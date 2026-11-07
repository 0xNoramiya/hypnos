# Hypnos: verified Hermes API notes

Verified against **Hermes Agent v0.23.0** (stable, git install, commit `2bd378ed1b8d`, released 2026-11-05),
source and docs read from that tag on 2026-11-07. Docs base URL: `https://hermes-agent.nousresearch.com/docs/`.

Detailed findings with verbatim signatures and `file:line` references live in `docs/recon/`:

| File | Covers |
|---|---|
| `recon/provider.md` | model-provider plugins, provider runtime, fallback, middleware |
| `recon/hooks.md` | all 46 hook names, kwargs, approval flow, fail-open rules |
| `recon/desktop.md` | Desktop plugin SDK, packaging, REST + events, surfaces, starter skeleton |
| `recon/runtime.md` | `ctx.llm`, storage, config, commands, cron, redaction, sandbox, deps |
| `recon/public-api-allowlist.txt` | the import allowlist `hermes plugins validate` checks |

## 1. Environment

| Item | Result |
|---|---|
| Hermes | v0.23.0 installed with the official installer (`scripts/install.sh --channel stable`). PyPI only has 0.19.0 and pip installs are no longer supported. |
| `hermes plugins doctor . --ci` | present |
| `hermes plugins validate . --install-deps` | present (catalog CI gate) |
| Catalog name `hypnos` | free. Related listed entries: `icarus`, `hermes-dreaming`. |
| CPU | AMD Ryzen 5 6600H (6C/12T), WSL2 exposes 8 logical CPUs |
| RAM | 9.7 GB (WSL2 limit), 24 GB swap |
| GPU | Integrated Radeon 660M only. No NVIDIA, no CUDA, no Apple Silicon (no MLX). |
| Docker | 29.4.3, daemon up. `--network none --read-only --tmpfs` sandbox verified (no network leak). |
| Python | 3.13 system, Hermes runs on its own 3.14 venv (`~/.hermes/hermes-agent`) |
| HF downloads | The Xet transport stalls on this network. `HF_HUB_DISABLE_XET=1` fixes it (27 s for SmolLM2-135M). |

**CPU benchmark** (torch 2.15 CPU, transformers 5.22, peft 0.21, LoRA r=16 on q/k/v/o, seq 512, batch 1):

| Model | LoRA step | Greedy generation |
|---|---|---|
| SmolLM2-135M-Instruct | 3.9 s | 11 tok/s |
| Qwen3-0.6B | not run; about 4-5x the 135M cost by parameter count | |

Conclusion: CPU is fine for unit tests and a tiny smoke night, too slow for real nights. Real nights and the
demo recording run on a CUDA GPU (the user's Runpod pods).

## 2. Extension points Hypnos uses (verified)

### 2.1 Manifest (`plugin.yaml`)

Standalone plugin (no `kind: model-provider`, see conflict C3). Fields used:
`name, version, description, author, license, requires_hermes, provides_hooks, provides_middleware,
provides_tools: [], python_dependencies (optional extras only), config_schema`.

- `requires_hermes` is a SemVer floor, declared in `plugin.yaml` **and** the catalog entry. We use `">=0.23.0"`.
- `provides_hooks` / `provides_middleware` must exactly match what `register()` registers, or validate fails.
- Unknown manifest fields are ignored by older Hermes.
- `config_schema` types: `str, int, float, bool, list, dict, secret`; choices supported. Setting names
  `model`, `plugins`, `security`, `settings` are reserved. Doc: `developer-guide/plugins#settings-form-in-the-desktop`.

### 2.2 Registration (`register(ctx)`), `hermes_cli/plugins.py`

| API | Signature | Doc |
|---|---|---|
| hooks | `ctx.register_hook(name, callback)`; callbacks are keyword-only, must accept `**kwargs` (doctor errors otherwise) | `user-guide/features/hooks` |
| middleware | `ctx.register_middleware(kind, callback)`, kinds `tool_request, tool_execution, llm_request, llm_execution` (`hermes_cli/middleware.py:24`) | `developer-guide/middleware` |
| slash command | `ctx.register_command(name, handler, description="", args_hint="", ...)`; handler `(raw_args: str) -> str` runs in the gateway process, must return fast | `developer-guide/plugins#register-slash-commands` |
| CLI command | `ctx.register_cli_command(name, help, setup_fn, handler_fn=None, ...)`; argparse (`setup_fn(subparser)`) | `developer-guide/plugins#register-cli-commands` |
| config | `ctx.get_config(key, default=None)` reads `plugins.entries.hypnos.settings.<key>`; does **not** apply schema defaults | `developer-guide/plugins#store-settings-and-runtime-state` |
| choice prompt | `ctx.ask_choice(...)` returns `None` under cron, hooks and async handlers | `developer-guide/plugins#ask-the-user-to-pick-an-option` |
| teacher LLM | `ctx.llm.complete(messages, *, provider=None, model=None, temperature=None, ...)`, `ctx.llm.complete_structured(*, instructions, input, json_schema=None, schema_name=..., purpose=..., temperature=..., max_tokens=...)`, async variants | `developer-guide/plugin-llm-access` |

`register()` has a 10 s load deadline: no torch, transformers or other heavy imports at load.

### 2.3 Storage, `plugins.plugin_storage` (public)

```python
from plugins.plugin_storage import plugin_data_dir, plugin_db
plugin_data_dir(name: str) -> Path                       # <HERMES_HOME>/plugin-data/<name>/, created on call
plugin_db(name: str, filename: str = "data.db") -> sqlite3.Connection   # WAL mode
```
The worker subprocess opens the same file with plain `sqlite3` (absolute path passed by the parent).

### 2.4 Hooks we register (all fail-open, all `**kwargs`)

| Hook | When it fires | Key kwargs | Hypnos use |
|---|---|---|---|
| `pre_llm_call` | once per turn, before the loop | `session_id, user_message, conversation_history, platform, ...` | correction detector on the new user message; turn counter; habit block injection (appended to the user message) |
| `post_tool_call` | after each tool | `tool_name, args, result (JSON str: output, exit_code, error, status), tool_call_id, session_id, ...` | exit codes, test-runner parsing, denied-call detection (`exit_code: -1`, `status: blocked`) |
| `post_approval_response` | after the user answers an approval | `command, description, pattern_key(s), session_key, surface, choice, turn_id, tool_call_id` | approve/deny signals. `choice` in `once, session, always, deny, timeout, cancelled, notify_failed, smart_approve, smart_deny` |
| `post_llm_call` | once per turn after the tool loop, success only | `session_id, task_id, turn_id, assistant_response, model, platform` | close the episode, write per-turn route record |
| `on_session_end` | end of **every turn** | `session_id, ...` | flush |
| `on_session_finalize` | real session end (not cron, idle eviction or crash) | `session_id, ...` | best-effort final flush |
| `transform_llm_output` | final text, first non-empty return wins | `response_text, session_id, platform` | Desktop reply badge directive (desktop platform only, opt-out setting) |
| `post_compaction` (optional) | context compaction | old and new session ids | keep episodes linked across id change |

Join tool and approval events on `tool_call_id` (approval `command` may be redacted). Hermes gives no turn
index; Hypnos counts `pre_llm_call` per session. Hooks run in the long-lived gateway process across sessions
and profiles: hooks only enqueue; a background writer started with `contextvars.copy_context()` persists.

### 2.5 Routing middleware: `llm_execution`

```python
def on_llm_execution(*, request, original_request, next_call, task_id, turn_id, session_id,
                     platform, model, provider, base_url, api_mode, api_call_count, **kwargs):
    ...  # return next_call(request) to pass through, or return a provider-shaped response
```
- Runs once per API attempt (several per turn when tools are used). `next_call` is single-use.
- Raising before `next_call` fails open to the frontier.
- A short-circuited response must match `api_mode`: ChatCompletion shape for `chat_completions`,
  Anthropic Messages shape (`content` blocks, `stop_reason`, `usage`) for `anthropic_messages`.
  `codex_responses` always passes through.
- Tool calls returned this way still go through `tool_request` middleware, `pre_tool_call` guards and
  Hermes approvals. Hypnos never executes tools and never rewrites args.

### 2.6 Desktop

- Unified package: `desktop/plugin.js` in the plugin repo. Desktop copies it to `desktop-plugins/hypnos/`;
  it is off until the user enables it in Capabilities > Plugins. Folder, `plugin.yaml` name and desktop `id`
  are all `hypnos`. Imports: `@hermes/plugin-sdk`, `react`, `react/jsx-runtime` only; `jsx()` calls, no JSX.
- Python routes: `dashboard/manifest.json` + `dashboard/plugin_api.py` serve `/api/plugins/hypnos/...`;
  Desktop calls `ctx.rest('/path')`.
- Push: `from hermes_cli.plugin_events import broadcast_plugin_event`;
  `broadcast_plugin_event("hypnos", "night.stage", payload)`; Desktop listens with
  `ctx.onEvent('plugin.hypnos.night.stage', fn)`. Only delivered from the gateway process.
- Surfaces: `STATUSBAR_AREAS.right`, `ROUTES_AREA` (`/hypnos`), `SIDEBAR_NAV_AREA`, `PALETTE_AREA`,
  `TRANSCRIPT_DIRECTIVE_AREA` (`::hypnos{t="..."}`), `SESSION_ROW_AREAS`, `host.openWorkspace(...)`,
  `ctx.pet` (speech bubble only), auto-rendered `config_schema` settings form.
- No chart components: the hypnogram is hand-drawn SVG using `--ui-*` theme tokens.
- Doc: `developer-guide/desktop-plugin-sdk`.

### 2.7 Credentials, approvals, scheduling

- Secrets: `from agent.secret_scope import get_secret` for our own `config_schema` secrets only
  (Tinker key, optional HF token). Never read `.env`, `auth.json` or vendor tokens.
- Cron: plugins get no API to create cron jobs and must not edit `jobs.json`. Cron fires only from the gateway.
- Past sessions: `hermes_state.SessionDB` is public, but live hook capture is the documented route.
- `MEMORY.md` / `USER.md`: `<HERMES_HOME>/memories/`. Reading them is undocumented, so it is disclosed.

## 3. Conflicts between the spec and reality

| # | Spec says | Reality (v0.23.0) | Resolution |
|---|---|---|---|
| C1 | Only import modules in `hermes_cli/data/plugin_api.json` | No such file. The allowlist is the `_PUBLIC` set in `hermes_cli/plugin_validate_private_imports.py`; a non-public import is a validate **warning**, not a refusal | Import only from that set (copied to `recon/public-api-allowlist.txt`). A unit test AST-scans our code against it. |
| C2 | `hermes --version`, install if missing | PyPI is frozen at 0.19.0; pip is unsupported | Installed v0.23.0 via the official installer. `requires_hermes: ">=0.23.0"` (the only version we can test). |
| C3 | Model provider plugin `hypnos` wraps the frontier and does routing, shadow, fallback, pruning | A provider profile is declarative; `create_client` only gets Hypnos's own credentials; no documented route calls another provider (`credentialed_provider_request` covers `openai-codex` only; rule 11 forbids reading creds). A `kind: model-provider` plugin never gets `register(ctx)` | **Routing lives in `llm_execution` middleware.** The user's frontier stays `model.provider`; Hermes keeps its credentials, streaming, prompt caching, retries and fallback. Hypnos passes through with `next_call`, short-circuits with a validated brain answer, or runs the brain in a context-copied thread for shadow mode. Same-turn fallback is just `next_call`. No `hypnos` row in the model picker. |
| C4 | Middleware route side effects | Brain answers arrive whole (not streamed); the brain call is outside Hermes interrupt handling; usage is billed as the frontier model | Hard timeout on every brain call (default 20 s); brain usage reported in our own `routes` table; documented in README limitations. |
| C5 | `post_llm_call` per LLM call; `on_session_end` at session end | `post_llm_call` is once per turn (success only); `on_session_end` fires every turn; `on_session_finalize` skips cron, idle eviction and crashes | Per-call data comes from the middleware (sees request + response). Flush every turn; finalize is best effort. |
| C6 | Hook `pre_tool_call` for capture | `pre_tool_call` is **fail-closed**: an exception or timeout blocks the user's tool call, and one timeout blocks all tool calls for 60 s | Not registered. `post_tool_call` + `post_approval_response` cover capture. |
| C7 | Denial then corrected approved action forms a gold pair | No id links the denial to the later correction; deny reasons only exist for gateway `/deny <reason>` and arrive inside the tool result `error` text; after a denial the model is told to wait, so the correction comes in the next user message | Pair matcher: same session, denied call, then within K turns an approved or successful call of the same tool, with the intervening user message as correction context. Parse `Reason given by the user: "..."` when present. Each pair gets a confidence; only high-confidence pairs are gold. |
| C8 | Use a Hermes redaction utility if public | `agent.redact` works but is not public; hook payloads are not guaranteed scrubbed | Own scrubber with thorough tests. |
| C9 | `ctx.llm` for teacher work, budget-capped, from the night worker | `ctx.llm` only works inside a Hermes process; no budget enforcement; `cost_usd` is never filled | `hermes hypnos sleep` is the orchestrator and runs inside Hermes (CLI command): all teacher calls happen there. Training runs in a separate worker subprocess with its own Python env (torch never enters Hermes). Budget enforced by Hypnos from token counts and configured $/Mtok. |
| C10 | Nightly schedule from the plugin | No plugin cron API; `jobs.json` is off limits; cron only fires from the gateway | Manual `/sleep` and `hermes hypnos sleep`. Opt-in nightly: `hermes hypnos schedule` prints the exact `hermes cron` command for a no-agent job running `hermes hypnos sleep --unattended --if-idle`, which never prompts and exits cleanly. Nights checkpoint per stage (script timeout default 3600 s) and resume. Idle = our own activity timestamps plus host load. |
| C11 | Per-reply badge "brain v7 · 1.2k tokens saved" | No message footer slot. Only a transcript directive, appended to the reply text by `transform_llm_output`; it persists in the transcript and is replayed to the model (a few tokens); first transform wins across plugins | Directive `::hypnos{t="<turn>"}` on desktop only, badge fetches details by turn id; setting `desktop_reply_badges` to turn it off. Directives are stripped from captured data and brain prompts. Status-bar "last reply" chip as a fallback. |
| C12 | Hypnogram pane | Closing a plugin's only pane disables the whole plugin | Hypnogram opens as a workspace tab (`host.openWorkspace`) and is embedded in the report page. |
| C13 | Live hypnogram updates | `broadcast_plugin_event` only reaches Desktop from the gateway process | Events when the night is started from the gateway; always poll `ctx.rest('/night')` every 2 s while a night runs. |
| C14 | Pet drowsy/asleep/awake | `ctx.pet` only shows speech-bubble text (120 chars, 30 s, rate limited); no animation states | Bubble lines ("getting sleepy", "zzz", "learned 3 habits overnight"). Pet stays first in the cut list. |
| C15 | Desktop `desktop/plugin.js` only | Data needs Python REST routes | Add `dashboard/manifest.json` + `dashboard/plugin_api.py`. |
| C16 | `cuda` backend uses TRL + PEFT; `mlx` backend | No CUDA or MLX on this machine. TRL is now 1.x and transformers 5.x (APIs moved) | One `torch` backend (cuda when present, else cpu) with our own LoRA SFT and DPO loops over transformers + peft; DPO loss unit-tested on a tiny model. TRL not required. `mlx` backend written against mlx-lm but marked untested (no Mac here). `tinker` remote, opt-in, disclosed. |
| C17 | Default base Qwen3-1.7B or Qwen3.5-0.8B | Qwen3.5-0.8B is a multimodal hybrid linear-attention model whose fast kernels are CUDA only; 1.7B does not train on 9.7 GB CPU RAM | Default `Qwen/Qwen3-0.6B` (Apache-2.0, `<tool_call>` JSON template); `Qwen/Qwen3-1.7B` recommended on GPU; tests use `HuggingFaceTB/SmolLM2-135M-Instruct` (Apache-2.0) with our own tool template, since its template ignores tools. |
| C18 | Brain endpoint (mlx_lm.server, llama.cpp, Ollama, Tinker) | Hermes probes `:8080` for its own managed llama.cpp server and would adopt ours | `hermes hypnos serve` (transformers + peft, OpenAI-compatible, returns logprobs) on `127.0.0.1:8765` by default, or a user-configured endpoint. Disclosed background process. |
| C19 | Brain confidence: logprob or 2-sample agreement | External endpoints may not return logprobs | Primary: mean token logprob over the tool-call tokens. Fallback when logprobs are missing: agreement of 2 samples. |
| C20 | Slash commands trigger training and ask for confirmation | Handlers run in the gateway and must return fast; prompts return `None` under cron and async | Training is always spawned detached and the command returns at once. Confirmation is a two-step token: `/lethe habit 12` shows the plan and a code, `/lethe confirm <code>` runs it. CLI uses `--yes`. |
| C21 | Catalog `capabilities` must match "what we register" | The catalog block only lists `provides_tools, provides_hooks, provides_middleware, requires_env` | Declare hooks and `llm_execution` exactly; no tools. |
| C22 | Hermes RL tooling reuse | v0.23.0 ships no RL or training tooling (no rl-training doc page) | Own trainer; datasets exported in Hermes's ShareGPT trajectory format. |
| C23 | Brain prompt = full context minus consolidated habits | Hermes's system prompt is long (tool schemas, memory). A 0.6B brain on CPU cannot prefill 10k+ tokens per turn quickly | Brain prompt = compact template: Hypnos persona line, only the tools offered this turn, unconsolidated habits, recent turns. Training uses the exact same template, so train and serve match. Tokens saved are measured against the frontier request. |
| C24 | Habits pruned from the prompt | Habit text only exists in the prompt if something puts it there | Hypnos injects a short "habits" block through `pre_llm_call` for every turn (frontier too). Consolidated habits are dropped from the brain copy, along with matching preference lines from MEMORY.md / USER.md. |

## 4. Milestone plan

Deadline: 2026-11-11 17:00 EST (2026-11-12 06:00 WITA). Each milestone ends with tests green,
`hermes plugins doctor . --ci` clean and a commit.

| M | Scope | Target |
|---|---|---|
| M0 | Recon, this file | Nov 7 |
| M1 | Scaffold, SQLite + migrations, capture hooks (enqueue + writer thread), signals, correction detector (EN + ID), scrubber, sleep pressure, `/hypnos status` | Nov 8 |
| M2 | Habit ledger, deterministic probes (regex/AST over shell commands), judged-probe fallback, `/hypnos habits` | Nov 8 |
| M3 | `torch` trainer (cpu/cuda), worker subprocess protocol, N3 SFT, brains with lineage + manifest hash, `hermes hypnos serve` | Nov 9 |
| M4 | Waking exam (held-out next-action, probes, dreams, regression), promotion rule, rollback, morning report (CLI text) | Nov 9 |
| M5 | `llm_execution` routing, task families, shadow mode, validation fallback, habit pruning, token accounting. Demoable end to end. | Nov 10 |
| M6 | REM: teacher variants + verifiers, Docker sandbox and lucid mode, DPO, nightmares | Nov 10 |
| M7 | Desktop: moon status bar, hypnogram workspace, report route, reply badge, pet lines, settings | Nov 11 |
| M8 | `/brain diff`, `/brain export`, `/lethe` | Nov 11 |
| M9 | `demo timelapse`, README, DISCLOSURES, catalog entry + PR draft, demo script, screenshots (GPU night on Runpod) | Nov 11 |

Cut order if forced: Tinker backend, pet, `/brain export`, `mlx` backend.

## 5. Open items

- Runpod usage mode for real nights and the demo: run Hermes + Hypnos on the pod (Desktop connects remotely),
  or keep Hermes local and run only the trainer on the pod (data leaves the machine, opt-in and disclosed like
  Tinker). Needed by M3 for a real GPU night; CPU covers everything before that.
- Hermes Desktop for screenshots and the video: Desktop runs on the Windows side and connects to this WSL
  Hermes, or runs on the pod.

## 6. Where the build differs from the spec

| Spec | Built | Why |
|---|---|---|
| Model provider `hypnos` | `llm_execution` middleware | C3: a provider cannot reach the user's model without its credentials |
| `mlx`, `cuda` (TRL), `tinker` backends | One `torch` backend for CUDA and CPU; MLX and Tinker not written | No Mac or Tinker key to test with; Tinker was first in the cut order |
| Resumable nights | An interrupted night is marked `interrupted` and the next one starts fresh | Training is short; mid-night resume added risk for little gain |
| Optional `ctx.llm` correction classifier | Deterministic EN and ID patterns only | Patterns covered the persona and test set; a classifier costs a model call per turn |
| Pet drowsy, asleep, awake | Speech-bubble lines | C14 |
| Per-reply badge | Transcript directive, Desktop only, behind a setting | C11 |
| Hypnogram pane | Workspace tab plus the report page | C12 |
| Nightly schedule | `hermes hypnos schedule` writes the cron script and prints the `hermes cron create` command | C10 |
| `pre_tool_call` capture | Not registered | C6: it fails closed |

Measured on this machine: capture hooks p95 under 5 ms and passthrough routing p95 under 10 ms (tests);
LoRA generation through the PEFT wrapper is about 12x slower than with the adapter merged, so inference
always merges.
