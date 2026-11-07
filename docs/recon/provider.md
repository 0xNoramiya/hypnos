# Recon: model-provider plugin vs middleware (Hermes Agent v0.23.0, 2bd378ed)

Paths are relative to `/home/kuda/hackathon/ref/hermes-agent` unless they start with `DSDK/`, which means
`/home/kuda/hackathon/ref/hermes-official-plugins/claude-subscription-directsdk/`. "doc:" means
`website/docs/`. Public import allowlist: `docs/recon/public-api-allowlist.txt`, from
`hermes_cli/plugin_validate_private_imports.py:27`.

## TL;DR

- A model-provider plugin is a **declarative `ProviderProfile`**, not a request handler. The only seam that sees
  requests is `ProviderProfile.create_client(**client_kwargs)`, which returns an object shaped like the OpenAI
  client (`.chat.completions.create(**kw)`). (base.py:7-9, 489-512)
- **No documented way exists for a provider to call another configured provider (the frontier) with
  Hermes-held credentials.** `ctx.llm` is not available to model-provider plugins and has no tools or
  streaming. `credentialed_provider_request` covers `openai-codex` only. `get_secret` is for the plugin's
  *own* secrets. Catalog rule 11 forbids working around this. The only routes are private internals
  (`agent.auxiliary_client.resolve_provider_client`, `hermes_cli.runtime_provider.resolve_runtime_provider`)
  or the fallback-chain trick (§3).
- **Middleware (`llm_request` + `llm_execution`, registered from a normal `register(ctx)` plugin) fits
  Hypnos's routing, shadow mode and pruning better.** The user's frontier stays `model.provider`, and Hermes
  keeps owning its credentials, streaming, prompt caching, retries and fallback. `llm_execution` can
  short-circuit with a brain response, or call `next_call(request)` to pass through. It fails open to the
  frontier. Tool calls in the returned response still go through Hermes approvals.
- Costs of the middleware route: the response shape depends on `api_mode`. A short-circuited brain answer is
  not streamed. Middleware code runs outside Hermes' interrupt and stale-call machinery. Usage is billed to
  the frontier model.

---

## 1. Declaration and registration

### Layout and discovery
doc:developer-guide/model-provider-plugin.md:28-37
```
plugins/model-providers/my-provider/
├── __init__.py       # Calls register_provider(profile) at module-level
├── plugin.yaml       # kind: model-provider + metadata (optional but recommended)
└── README.md         # Setup instructions (optional)
```
"The only required file is `__init__.py`." (:37)

Discovery order (:15-24): (1) bundled `<repo>/plugins/model-providers/<name>/`; (2) user
`$HERMES_HOME/plugins/model-providers/<name>/`, picked up on the next lookup with no restart; (3) installed
`$HERMES_HOME/plugins/<name>/`, imported only when `plugin.yaml` declares `kind: model-provider`;
(4) legacy `providers/<name>.py`. These are per profile home. `register_provider()` is last-writer-wins
(:26), so a user plugin overrides a bundled one with the same name. Pip packages use the entry-point group
`hermes_agent.plugins`, require opt-in through `plugins.enabled`, and have the lowest precedence (:726-770).

### plugin.yaml
doc:model-provider-plugin.md:66-73
```yaml
name: acme-inference
kind: model-provider
version: 1.0.0
description: Acme Inference — OpenAI-compatible direct API
author: Your Name
```
- Valid kinds: `{"standalone", "backend", "exclusive", "platform", "model-provider"}`
  (`hermes_cli/plugins_manifest.py:27`). Manifest v2 fields such as `manifest_version`, `requires_hermes`,
  `config_schema`, `requires_auth` and `python_dependencies` are optional
  (doc:developer-guide/plugins/index.md:607-658).
- A model-provider manifest is **never `register(ctx)`-loaded**. `hermes_cli/plugins_discovery.py:309-312`
  says: "Model providers load via providers/__init__.py; a second import here would create two
  ProviderProfile instances…". The general loader only records the manifest
  (doc:model-provider-plugin.md:722-724). **A `kind: model-provider` plugin therefore cannot register hooks,
  middleware, tools or commands** (:552: "model-provider manifests are skipped by the generic command-plugin
  loader, so `register(ctx)` is not the way to add commands").
- Model-provider kind is on by default with no `hermes plugins enable` step
  (`hermes_cli/plugins_cmd.py:854`). A standalone or middleware plugin must be enabled
  (`plugins_discovery.py:318-322`; doc:middleware.md:122-128).
- Real example: DSDK/plugin.yaml uses `name: claude-subscription-directsdk-experimental`,
  `kind: model-provider`, `manifest_version: 2`, `requires_hermes: ">=0.21.4"`, `optional_env`,
  `external_dependencies` and `config_schema`. Settings are read from
  `plugins.entries.<manifest name>.settings.<key>` via `hermes_cli.config.load_config_readonly()`
  (DSDK/directsdk_setup.py:56-107). `hermes_cli.config` is on the public allowlist.

### Registration API (no ABC with abstract methods; a dataclass base)
- `providers/__init__.py:140`: `def register_provider(profile: ProviderProfile) -> None:`
- `providers/base.py:141-148`: `@dataclass class ProviderProfile:` with `name: str` as the only required
  field. Docstring (base.py:7-9): "Provider profiles are DECLARATIVE — they describe the provider's
  behavior. They do NOT own client construction, credential rotation, or streaming. Those stay on AIAgent."
- Fields that matter for Hypnos: `api_mode="chat_completions"`, `display_name`, `description`, `env_vars`,
  `base_url`, `auth_type` (`api_key|oauth_device_code|oauth_external|external_process|aws_sdk|plugin-defined`),
  `supports_streaming: bool | None` (kw_only), `supports_health_check`, `supports_model_listing`,
  `fallback_models`, `model_aliases`, `model_capabilities`, `classify_api_error`, `hostname`/`hostnames`,
  `default_aux_model`. The full table is at doc:model-provider-plugin.md:220-337.
- Optional overridable methods, verbatim from base.py. All have defaults; none are abstract.
  ```python
  def create_client(self, **client_kwargs: Any) -> Any | None:                       # :489
  def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:  # :324
  def build_extra_body(self, *, session_id: str | None = None, **context: Any) -> dict[str, Any]:  # :332
  def build_api_kwargs_extras(self, *, reasoning_config: dict | None = None, **context: Any
                              ) -> tuple[dict[str, Any], dict[str, Any]]:              # :351
  def get_model_context_length(self, model: str) -> int | None:                         # :392
  def get_usage_cost(self, model: str, usage: Any) -> Any | None:                       # :396
  def fetch_models(self, *, api_key: str | None = None, base_url: str | None = None,
                   timeout: float = 8.0) -> list[str] | None:                           # :524
  def resolve_api_key_secret(self) -> tuple[str, str] | None:                           # :626
  def setup_status(self, **kwargs) / def discover_models(self, **kwargs)                # :514 / :519
  classify_api_error: Callable[..., Any] | None = None   # field; called (error, *, status_code, error_code, message, body, model)
  ```
- The `create_client` contract (base.py:493-505): "`client_kwargs` is the same mapping the core would have
  passed to `openai.OpenAI` (`api_key`, `base_url`, `command`, `args`, timeouts, headers…). Unknown keys must
  be tolerated…". A raise is logged and falls back to the standard client
  (`agent/agent_runtime_helpers.py:1875-1880`). It is called only on the OpenAI-wire client path
  (`agent_runtime_helpers.py:1986-1995`, `create_openai_client`), so the profile must use
  `api_mode="chat_completions"`.
- Under `plugins.isolation: host`, a profile that overrides `create_client` is imported **in-process** with
  a warning (`providers/__init__.py:623-655`; doc:user-guide/features/plugins.md:933-937).
- Reference implementation, DSDK/__init__.py:
  `class ClaudeOAuthDirectSDKProfile(ProviderProfile)` overrides `get_model_context_length` (:84),
  `get_usage_cost` (:92), `create_client` (:112, `return Client(**client_kwargs)`),
  `build_api_kwargs_extras` (:139, marks aux calls `{'hermes_auxiliary': True}`), and `fetch_account_usage`
  (:147). The registration at :171-197 uses `auth_type='external_process'`,
  `base_url='process://claude-subscription-directsdk-experimental'`, `classify_api_error=...`,
  `profile.supports_streaming = True`, then `register_provider(profile)`.

### Auth for a local-only provider (Hypnos has no key)
- An `api_key` profile with empty `env_vars` is **not mirrored** into the auth registry
  (doc:model-provider-plugin.md:541). Options: declare `env_vars=("HYPNOS_API_KEY","HYPNOS_BASE_URL")` and
  override `resolve_api_key_secret()` to return a placeholder `("local", "hypnos")`. That method is checked
  first in `hermes_cli/auth.py:375-377`. Alternatively use `auth_type="external_process"` with a
  `process://` marker base_url, as DSDK does. That needs `process_command` to resolve on PATH for the row to
  count as signed in (doc:model-provider-plugin.md:431-433).

---

## 2. What a provider receives per call and what it returns

A provider only sees calls through its `create_client` object: `client.chat.completions.create(**api_kwargs)`.

### Input (chat_completions wire)
- `api_kwargs` comes from `agent/transports/chat_completions.py:388-408` (`_base_kwargs`):
  `{"model", "messages"[, "temperature"][, "timeout"][, "tools"]}`, then max_tokens or
  `max_completion_tokens`, `extra_body` (from `build_extra_body` + `build_api_kwargs_extras`), optional
  `reasoning_effort`, `prompt_cache_key` (only if `supports_prompt_cache_key`), and `extra_headers`
  (Relay `traceparent`).
  - `model`: the hypnos model id as configured (`model.default`).
  - `messages`: OpenAI chat format. **The system prompt is `messages[0]` with `role: "system"`**, or
    `developer` after the developer-role swap (base.py:324-330). It is assembled in the layers described at
    doc:developer-guide/prompt-assembly.md:27-136: SOUL, tool guidance, memory, `## Skills (mandatory)`,
    `# Project Context`, timestamp and platform hint. These headings are usable as pruning anchors.
  - `tools`: OpenAI function schemas `[{"type":"function","function":{name,description,parameters}}]`.
- Streaming: Hermes prefers streaming (`agent/turn_api_call.py:46-71`, `_should_stream`). The stream call is
  `self._open_chat_stream({**next_api_kwargs, "stream": True, "timeout": timeout})`, where `timeout` is an
  `httpx.Timeout` (`agent/chat_completion_helpers.py:3062`). An `external_process` profile streams only if
  `supports_streaming=True`. If `create(stream=True)` returns a completed object instead of an iterator,
  Hermes "switch[es] the session to non-streaming and replay[s] its content as deltas"
  (`chat_completion_helpers.py:3226-3249`).
- DSDK whitelists the kwargs it accepts (DSDK/directsdk.py:259-263): `model, messages, tools, stream,
  stream_options, max_tokens, max_completion_tokens, temperature, top_p, stop, extra_body, extra_headers,
  timeout, tool_choice, parallel_tool_calls, n, response_format, hermes_auxiliary`.
- Auxiliary calls (compression, titles, vision on `provider: auto`) also reach the main provider's client
  (doc:user-guide/features/fallback-providers.md:230-235; `agent/auxiliary_client.py:5149-5160`).

### Output
- Non-streaming: an object shaped like an OpenAI ChatCompletion, read by
  `ChatCompletionsTransport.normalize_response` (`agent/transports/chat_completions.py:704-756`):
  `response.choices[0].message.{content, tool_calls[].{id,function.{name,arguments(JSON str)}}, reasoning,
  reasoning_content, reasoning_details, refusal}`, `choices[0].finish_reason`
  (`stop|tool_calls|length|content_filter`), `response.usage.{prompt_tokens, completion_tokens,
  total_tokens, prompt_tokens_details.cached_tokens}`, and `response.model`.
  Minimal real example (`agent/acp_client.py:375-391`):
  ```python
  message = SimpleNamespace(content=cleaned_text, tool_calls=tool_calls, reasoning=reasoning or None,
                            reasoning_content=reasoning or None, reasoning_details=None)
  completion = SimpleNamespace(
      choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if tool_calls else "stop")],
      usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=0)),
      model=model or self.placeholder_model)
  return _completion_to_stream_chunks(completion) if stream else completion
  ```
- Streaming: an iterator of chunks with `chunk.choices[0].delta.{role, content, tool_calls[].{index, id,
  type, function.{name, arguments}}, reasoning_content, reasoning, reasoning_details}`,
  `choices[0].finish_reason`, plus a trailing usage chunk `SimpleNamespace(choices=[], model=…, usage=…)`.
  See `agent/acp_openai_bridge.py:49-77` (`completion_to_stream_chunks`) and DSDK/directsdk.py:1101-1103
  (`_chunk`). Close the stream with `.close()` and implement `__enter__`/`__exit__` (DSDK `Stream`, :540-565).
- Client object conventions:
  - `HERMES_SKIP_TRANSPORT_WRAP = True` and `HERMES_SKIP_ASYNC_WRAP = True` class attributes
    (doc:model-provider-plugin.md:393).
  - `close()`, `is_closed`, and `api_key`/`base_url` attributes.
  - Optional `cancel(self)`. It is called from the interrupting thread and must return promptly
    (doc:model-provider-plugin.md:444; DSDK/directsdk.py:701-706).
  - Clients are cached and shared, so they must be thread-safe (`agent/acp_client.py:342-345`).
- Normalized internal types (`agent/transports/types.py`): `ToolCall(id, name, arguments: str,
  provider_data)`, `Usage(prompt_tokens, completion_tokens, total_tokens, cached_tokens)`, and
  `NormalizedResponse(content, tool_calls, finish_reason, reasoning, usage, provider_data)`.
- Usage and cost: `agent/usage_pricing.py:640-693` (`normalize_usage` → `CanonicalUsage(input_tokens,
  output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, raw_usage)`).
  `estimate_usage_cost` calls `profile.get_usage_cost(model_name, usage)` first (`usage_pricing.py:703-708`).
  Session rows record `model=agent.model, billing_provider=agent.provider` (`agent/turn_usage.py:264-280`).

---

## 3. Can a provider delegate to or wrap the user's frontier without reading credentials?

**Not through any documented surface.** Each candidate:

| Route | What it does | Usable by a Hypnos provider? |
|---|---|---|
| `ProviderProfile.create_client(**client_kwargs)` | `client_kwargs` holds **Hypnos's own** resolved runtime (api_key/base_url for `hypnos`), not the frontier's (agent_runtime_helpers.py:1853-1880) | No frontier access |
| `ctx.llm.complete(...)` (`agent.plugin_llm.PluginLlm`) | "Host-owned credentials … The plugin never sees a token" (doc:plugin-llm-access.md:85-89), but "**No streaming, no tool loops**" (:89-91). It defaults to "the user's active provider", which would be `hypnos` itself (recursion). `provider=` is gated by `allow_provider_override` (:404-446) | No. Model-provider plugins get no `ctx`, and there are no tools or streaming |
| `ctx.side_question(token, prompt)` | Returns `None` for "an agent-as-provider runtime (ACP, external-process providers…)" (:588-596); no tools | No |
| `hermes_cli.plugin_provider_requests.credentialed_provider_request(provider, method, url, *, json=None, headers=None, timeout=30.0) -> ProviderResponse` (:134-147) | `_PROVIDERS` contains only `"openai-codex"` (:70-72) or a plugin's own `oauth:` id; origin allowlist; non-streaming, full body | No for Anthropic, OpenRouter or Nous |
| `agent.secret_scope.get_secret(name, default=None)` (:230) | Reads env-var secrets in the profile scope. It is documented for the plugin's **own** `requires_env`/`config_schema` secrets (catalog rule 11, doc:developer-guide/plugins/catalog-submission.md:149-165) and does not cover OAuth (Claude Code creds, Nous OAuth in auth.json) | Technically possible for API-key frontiers, but this is exactly the credential read the brief forbids |
| `agent.auxiliary_client.resolve_provider_client(provider, model=None, async_mode=False, raw_codex=False, explicit_base_url=None, explicit_api_key=None, api_mode=None, main_runtime=None, is_vision=False, task=None) -> (client, model)` (:5311-5322) | Returns a client with `.chat.completions.create()` for any configured provider, with Hermes-managed auth (Anthropic is wrapped by `AnthropicAuxiliaryClient`, :1809-1822). This is what fallback itself uses | Works, but it is **private**: not on the public allowlist, so `hermes plugins validate` warns. "Internal import paths were never part of the plugin contract" (doc:plugins/index.md:303-313). Aux semantics (SDK retries, no main-loop streaming or caching decorations) |
| `hermes_cli.runtime_provider.resolve_runtime_provider(*, requested=None, explicit_api_key=None, explicit_base_url=None, target_model=None) -> dict` (:1044-1045) | Returns `{provider, api_mode, base_url, api_key, source…}` (doc:provider-runtime.md:91-100). **This hands the plugin the raw api_key** | Private, and it reads credentials |
| Fallback chain: `model.provider: hypnos` with `fallback_providers: [{provider: anthropic, model: …}]` | Hypnos raises, and `classify_api_error` returns `{"reason": "model_not_found", "retryable": False, "should_fallback": True}`. Hermes then builds the frontier client with its own creds via `resolve_provider_client` (doc:provider-runtime.md:193-201) | The **only documented-surface way** to "delegate". Limits are covered in §4 |

The closest documented approach is **not a provider**. It is a standalone plugin that registers
`llm_execution` (and optionally `llm_request`) middleware while the user's frontier stays `model.provider`.
Hermes then owns every frontier credential and the plugin never sees one (details in §6).

What is explicitly disallowed (catalog-submission.md):
- Rule 9 (:124-136): "must not replace, wrap or rebind core functions … (`AIAgent.<method> = ...`,
  `setattr(...)`, `sys.modules[...]`)". Admission refuses this ("no core override" check).
- Rule 11 (:149-165): "A plugin never reads `.env` files, Hermes's `auth.json`, another tool's login … If
  Hermes has no route for a credential a plugin needs, ask for one … the plugin does not work around it."
- doc:plugins/index.md:270-279 makes the same point: runtime wrapping of core is "not a supported extension
  point".

---

## 4. Fallback providers and whether a provider can signal "fall back"

- Config is a top-level `fallback_providers:` list of `{provider, model[, base_url, key_env]}`
  (doc:fallback-providers.md:32-40, 109-119).
- Triggers (:121-129): 429 and 5xx after retries; 401, 403 and 404 immediately; invalid or empty responses
  repeatedly. Internals: `try_activate_fallback()` is called from `agent/turn_api_error.py`,
  `turn_response_check.py` and `turn_recovery.py`. It swaps `self.model/provider/base_url/api_mode/client`
  in place (doc:provider-runtime.md:184-201; code `agent/chat_completion_helpers.py:2053-2096`).
- Turn scope (:147-153): "each new user message starts with the primary model restored … Within a single
  turn, fallback activates at most once".
- **A provider can signal fall-back in the same turn**: raise from `create()` and classify the error. From
  doc:model-provider-plugin.md:674: return `{"reason": <FailoverReason name>}` plus optional
  `retryable`/`should_compress`/`should_rotate_credential`/`should_fallback`/`error_context`. "for terminal
  reasons … `should_fallback: True` implies `retryable: False` unless you set it, because the fallback chain
  only runs for non-retryable verdicts". The reasons are in `agent/error_classifier.py:34-67`. Pick a
  non-rate-limit reason such as `model_not_found`: only `_RATE_LIMIT_FAILOVER_REASONS` arm the primary
  cooldown (`agent/fallback_cooldown.py:56-57`).
- Downsides of the fallback trick for Hypnos:
  - The user sees a notice every time: `"⚠️ Model fallback: {old_model} via {old_provider} unavailable
    ({reason}); using {fb_model} via {fb_provider}."` (chat_completion_helpers.py:2086-2092).
  - `rewrite_prompt_model_identity()` edits the system prompt (:2084).
  - It fires `api_request_error` hooks and counts as an error.
  - It works once per turn, with no way back to the brain mid-turn.
  - The frontier path never passes through Hypnos, so **shadow mode is impossible**.
  - Aux tasks and usage stay attributed to `hypnos`.
- Middleware equivalent: inside `llm_execution`, "fall back" is just `return next_call(request)`, with no
  error and no notice. An exception raised before `next_call` also fails open to the frontier
  (`hermes_cli/middleware.py:205-213`).

---

## 5. Per-turn metadata the Desktop UI or session can show

There is **no documented "answered-by" or custom-metadata field** that a provider or middleware can attach to
a turn for display. What exists:
- `response.model`: the transport reads it, and the `post_api_request` hook gets `response_model`, `usage`
  and a sanitized `response` (doc:plugins/index.md:1536-1537). These are observer channels only, not UI.
- Request middleware may return `source`/`reason`/`name` strings. They become `middleware_trace` in later
  observer hook payloads (doc:middleware.md:57-68; `hermes_cli/middleware.py:77-82`). Again observer-only.
  Execution middleware cannot add a trace entry.
- Usage rows are keyed by `model=agent.model, billing_provider=agent.provider` (turn_usage.py:264-280).
  With middleware they are attributed to the frontier; with a `hypnos` provider, to `hypnos` (priced by
  `get_usage_cost`).
- Desktop plugin, which is the real UI route:
  - The Python side calls `hermes_cli.plugin_events.broadcast_plugin_event(plugin_id, event, payload)`.
    The desktop half receives it with `host.onEvent('plugin.<id>.<event>', fn)`
    (doc:developer-guide/desktop-plugin-sdk.md:2277-2310). It reaches connected Desktop windows; it is a
    no-op for `hermes chat` and cron.
  - Show it in a status bar item (`STATUSBAR_AREAS.right`, :323-341).
  - A model-pill label provider (`COMPOSER_AREAS.modelPill`, :741-770) exists, but it is re-evaluated only
    when the registry, model, effort or compact flag changes.
- `transform_llm_output` can append a footer (doc:user-guide/features/hooks.md:1756-1803). It is **persisted
  and replayed to the model** ("the replacement is what the session stores … what the next turn replays"),
  so it is a poor fit for metadata.

---

## 6. Middleware

### What it is and how to register it
doc:developer-guide/middleware.md:8-26: "Middleware can change what happens by rewriting a request before
execution or by wrapping the execution callback itself … for … adaptive routing …".
```python
def register(ctx):
    ctx.register_middleware("llm_request", on_llm_request)
    ctx.register_middleware("llm_execution", on_llm_execution)
```
- `hermes_cli/plugins.py:939`: `def register_middleware(self, kind: str, callback: Callable) -> PluginRegistration:`
- `hermes_cli/middleware.py:24-26`: `VALID_MIDDLEWARE = {tool_request, tool_execution, llm_request, llm_execution}`

| Kind | Payload | Return | doc:middleware.md:50-55 |
|---|---|---|---|
| `llm_request` | `request`, `original_request` | `{"request": {...}}` (complete replacement) | "Replace effective provider kwargs before provider execution." |
| `llm_execution` | `request`, `original_request`, `next_call` | **"Any provider response"** | "Wrap or replace the actual provider call." |

Context kwargs, verbatim from the call sites:
- `agent/turn_api_request.py:158-162`: `task_id, turn_id, api_request_id, session_id, platform, model,
  provider, base_url, api_mode, api_call_count`.
- `agent/turn_api_call.py:139-145` adds `original_request` and `middleware_trace`.
- Both add `telemetry_schema_version="hermes.observer.v1"` and
  `middleware_schema_version="hermes.middleware.v1"`.
- **The `agent` object is not passed.** Accept `**kwargs`.

### Order and semantics
- LLM order (doc:middleware.md:92-98): build kwargs → `llm_request` → `pre_api_request` hooks →
  `llm_execution` → `post_api_request` / `api_request_error`.
- `next_call` is single-use. Calling it twice raises (`middleware.py:180-190`). Skipping it short-circuits
  ("Call `next_call(payload)` exactly once … (or skip it to short-circuit)", doc:plugins/index.md:1706-1707).
- Fail-open: a callback that raises before `next_call` is logged, and the chain continues to base
  execution. If it raises after a successful `next_call`, the downstream result is kept
  (`middleware.py:201-213`; doc:middleware.md:256-277).
- `llm_execution` runs once per API attempt. That means several runs per turn when tools are used, plus
  retries. Key per-turn decisions by `turn_id`.

### Can it rewrite the system prompt or messages? Yes.
"Request middleware sees the full provider kwargs, including `messages` or Responses API `input`, model
settings, tool definitions, stream options…" (doc:middleware.md:100-103). Because the shape follows
`api_mode`:
- `chat_completions` (OpenRouter, Nous, custom): `messages[0]` is the system prompt and `tools` are OpenAI
  function schemas.
- `anthropic_messages` (native Anthropic): `{"model", "messages", "max_tokens", "system", "tools"…}`
  (`agent/anthropic_adapter.py:677-681`). `system` is a block list with `cache_control`, messages use
  `tool_use`/`tool_result` blocks, and tools use `input_schema`.
- `codex_responses`: `instructions` + `input`. Codex preflight is re-applied after middleware
  (`turn_api_call.py:93-99`).

Contrast `pre_llm_call`, which only appends to the user message or blocks the turn: "Injected context is
appended to the **user message**, not the system prompt … The system prompt is Hermes's territory"
(doc:plugins/index.md:1602-1608).

### Can it choose the model per turn?
- `llm_request` can rewrite `request["model"]`, but only on the **same** client and provider: the client,
  base_url and creds are fixed on `agent.client`.
- Sending the turn to a **different endpoint** (the brain) means `llm_execution` does not call `next_call`
  and returns its own response.
- What `next_call` returns: the non-streaming path returns the raw SDK response, and the streaming path
  returns "a SimpleNamespace in the non-streaming response shape" (`chat_completion_helpers.py:3977-3990`).
  `stream: True` is added *below* middleware (:3062), so `request` never carries `stream`.
- **A short-circuited response must match `api_mode`.**
  - chat_completions: ChatCompletion shape (§2). The `openai` SDK's non-stream `ChatCompletion` from the
    local server works as-is.
  - anthropic_messages: an object with `.content` blocks (`type` text / thinking / tool_use with
    `id, name, input`), `.stop_reason` and `.usage`, as read by
    `agent/transports/anthropic.py:65-105`.
  - doc:middleware.md:228-230: "Return the same response shape Hermes expects from the provider adapter."

### Does it fit Hypnos better than a provider? Yes (my assessment)

| Need | Middleware (frontier stays `model.provider`) | `hypnos` provider (`create_client`) |
|---|---|---|
| Call frontier without touching creds | `next_call(request)`, so Hermes owns auth, pools, OAuth refresh and retries | Only via private internals or the fallback trick (§3) |
| Frontier streaming, prompt caching, Anthropic thinking signatures, reasoning config, fallback chain | Unchanged | Hypnos must re-implement them or lose them |
| Route to brain | Short-circuit with an `api_mode`-shaped response | Natural: the request is always chat_completions-shaped |
| Shadow mode | Run the brain on a context-copied thread, then `return next_call(request)` | Hypnos must own both calls |
| Pruned system prompt for the brain only | Build the brain request from a copy of `request` inside `llm_execution`. **Do not prune in `llm_request`**, because that also changes what the frontier sees and breaks its prompt cache | Same, inside the client |
| Brain answer streamed to UI | **No.** It arrives whole; the final text is rendered at turn end when not previewed (`hermes_cli/cli_chat_turn_mixin.py:707`; `agent/turn_finalizer.py:798`) | Yes: return a chunk iterator |
| Interrupt / stale timeout on brain call | **No.** Middleware runs on the agent thread outside `_interruptible_api_call` (`turn_api_call.py:93-145`), so Hypnos needs its own short timeout | Yes: `cancel()` plus the Hermes stale detector |
| Usage/cost attribution | Billed as `agent.model` (the frontier) at frontier prices. Return real counts deliberately, or zeros | Attributed to `hypnos`; `get_usage_cost` prices it |
| Aux tasks (compression, titles) | Untouched (they never hit `llm_execution`) | Routed to `hypnos` on `provider: auto` |
| Plugin kinds needed | One standalone plugin, with `hermes plugins enable` | Provider dir, plus a second standalone plugin for any hooks or middleware |
| `plugins.isolation: host` | Not documented for middleware; assume in_process (the default) | Forced in-process |

Threads: plain threads lose the profile/secret contextvars. Use `agent.memory_provider.spawn_context_thread`
or `contextvars.copy_context()` (doc:developer-guide/memory-provider-plugin.md:422).

---

## 7. A local OpenAI-compatible endpoint (custom base_url) and localhost rules

- Standard pattern: `base_url` is the default endpoint, and a trailing `*_BASE_URL` in `env_vars` is the user
  override (doc:model-provider-plugin.md:103-104). Hermes then builds a stock `openai.OpenAI` client with no
  `create_client` needed (:75-88).
- **Do not put a loopback URL in `profile.base_url` or `hostname`.**
  - Profile hostnames auto-extend the URL→provider reverse map (`agent/model_metadata.py:461-467`).
  - From `plugins/model-providers/lmstudio/__init__.py:8-11`: "a loopback `base_url` would map every
    `127.0.0.1` / `localhost` endpoint to LM Studio in URL->provider inference and disable local-server
    detection for Ollama, llama.cpp and vLLM."
  - Use a marker scheme (`process://hypnos`, as DSDK does) or an empty base_url, and keep the brain URL in
    plugin settings.
  - `_validate_base_url` only checks http(s) ports and passes other schemes
    (`agent/auxiliary_client.py:2829-2843`).
- Managed llama.cpp: Hermes probes **:8080** by default for a llama-server
  (doc:user-guide/local-models.md:135-146, 148-178; `local_runtime.detect_ports`). Run the brain on another
  port so it is not adopted as `provider: llamacpp`.
- In-plugin localhost servers have precedent. DSDK runs a request-scoped relay bound to `('127.0.0.1', 0)`
  (DSDK/admission.py:699-701). No core rule forbids a plugin-owned local server.
- Catalog rule 13 (catalog-submission.md:172-177): disclose "long-running background processes" and network
  calls.
- Rule 12 (:166-171): never route around approvals, and fail cleanly instead of hanging under cron or the
  gateway.
- Local-endpoint timeouts: a silent prefill on localhost is bounded only by `timeout`
  (doc:user-guide/configuration.md:1183). `providers.<id>.stale_timeout_seconds` exists.
- For comparison: `fallback_providers` and `auxiliary.*` accept `provider: custom` with
  `base_url: http://localhost:8000/v1` (doc:fallback-providers.md:109-119, 318-330).

---

## 8. Conflicts with the Hypnos design

1. **"Provider plugin named `hypnos` that wraps the user's provider" has no documented mechanism.**
   - A profile is declarative (base.py:7-9).
   - `create_client` gets Hypnos's own runtime, not the frontier's.
   - Calling the frontier means private `agent.auxiliary_client` / `hermes_cli.runtime_provider` (validate
     warnings, breakage risk), reading creds (violates rule 11, and fails for OAuth), or the noisy
     once-per-turn fallback trick with no shadow mode.
2. **A `kind: model-provider` plugin cannot `register(ctx)`.** Middleware, hooks and the Desktop event push
   need a second, standalone plugin directory, enabled with `hermes plugins enable`.
3. **Making `hypnos` the main provider hijacks the rest of Hermes:**
   - aux tasks on `auto`
   - usage/cost attribution
   - the context-window lookup (Hypnos must answer `get_model_context_length` with the frontier's window)
   - prompt-cache decoration and Anthropic-native features (Hypnos would get chat_completions-shaped
     requests even when the frontier is Anthropic)
   - Nous rate-guard and credential-pool behavior
4. **Middleware path caveats:**
   - The short-circuit response must match `api_mode` (chat_completions vs anthropic_messages; bail to
     `next_call` on `codex_responses`).
   - The brain answer is not streamed.
   - The brain call is outside interrupt and stale handling.
   - Brain tokens are billed as the frontier model.
   - Pruning must happen on the brain copy only, never through `llm_request`.
5. **Tool approvals are safe in both designs**, as long as Hypnos only *returns* `tool_calls`. Returned calls
   go through `tool_request` middleware → `pre_tool_call` hooks/guardrails → approvals → `tool_execution`
   (doc:middleware.md:105-120). Hypnos must not:
   - execute tools itself (no ACP-style in-client fs handling, `agent/acp_client.py:241-268`)
   - auto-approve anything
   - register `tool_request` middleware that rewrites args ("the mutated values are what guardrails and
     approvals evaluate", :273-275)
   Validate brain tool calls against `request["tools"]`, which is OpenAI `function.parameters` on
   chat_completions and Anthropic `input_schema` on anthropic_messages.
6. **No UI-visible "answered by" field.** A Desktop plugin with a status bar fed by `broadcast_plugin_event`
   is needed.
7. **Local brain:** keep loopback out of the profile base_url, avoid port 8080, and disclose the background
   server.

Suggested shape:
- Implement Hypnos as a standalone plugin with `llm_execution` (+ optional `llm_request` for tagging only)
  and a desktop half for the route indicator. Use `next_call` for the frontier and its fail-open; build
  brain requests from a pruned copy; give the brain call a hard timeout.
- If a `hypnos` row in the model picker is a requirement, ship it as a separate thin profile. Accept the §3
  limits, or use the fallback chain (`model.provider: hypnos`, `fallback_providers: [frontier]`).
