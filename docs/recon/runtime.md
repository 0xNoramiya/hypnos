# Hermes runtime recon for Hypnos

Hermes Agent **v0.23.0** (`git describe` = `v0.23.0`, HEAD `2bd378ed`), read from `/home/kuda/hackathon/ref/hermes-agent`.
Abbreviations: `DG` = `website/docs/developer-guide`, `UG` = `website/docs/user-guide`, `PI` = `DG/plugins/index.md`.
Source refs are repo-relative. "Public" means listed in `hermes_cli/plugin_validate_private_imports.py:_PUBLIC`
(mirrored in `docs/recon/public-api-allowlist.txt`); importing anything else gives a `hermes plugins validate` **warning**, not a refusal.

---

## 0. Decisions this forces on Hypnos

1. **Teacher LLM calls (`ctx.llm`) only work inside a Hermes process with a loaded plugin context.** Make the nightly orchestrator a
   Hermes process (`hermes hypnos sleep`, i.e. `[sys.executable, "-m", "hermes_cli.main", "hypnos", "sleep"]`) that does all
   extraction, variant generation and judging, and writes results to the plugin DB. The torch/LoRA worker is a **separate sidecar
   interpreter** (`python_runtime: external`) that never calls the LLM and talks to the orchestrator via SQLite/files.
2. **Storage:** `from plugins.plugin_storage import plugin_data_dir, plugin_db`, which resolves to `<HERMES_HOME>/plugin-data/hypnos/`.
   `ctx.state` lives in a **different** dir (`plugin-data/agent-plugin-hypnos-f51bd9eb/state.json`), so keep sidecar-visible data out of it.
3. **Settings key `model` is reserved** (also `plugins`, `security`, `settings`). Use `student_model`/`base_model`. `ctx.get_config` does
   **not** apply `config_schema` defaults; pass `default=` every time.
4. **Scheduling:** there is no `ctx` API to create a cron job, and plugins must not write `cron/jobs.json`. Hermes offers three paths:
   a no-agent cron job created from an explicit user command, an Automation Blueprint, or a user-made cron job. Don't run an in-process timer
   (§5). There is no idle API, so Hypnos records its own activity timestamps from hooks.
5. **Sandbox:** run `docker run --rm` yourself with Hermes-like hardening. Don't push generated commands through `ctx.dispatch_tool("terminal")`:
   that targets the user's configured backend (possibly `local`), goes through approvals, and is denied under cron (`approvals.cron_mode: deny`).
6. **Redaction:** `agent.redact.redact_sensitive_text(text, force=True)` / `redact_for_egress(text)` exist, but `agent.redact` is **not** on the
   public list (validate warning). `ctx.register_redaction_patterns()` exists in source but is not in the website docs.
7. **Plugin host isolation (`plugins.isolation: host`)** skips `register_cli_command` and refuses `on_unload`, `spawn_task` and `post_message`.
   `ctx.llm` and `ctx.state` still work through remote facades. Every CLI feature therefore needs a slash-command equivalent.
8. **Hermes ships no RL/training tooling.** No Atropos/Tinker extra exists in v0.23.0. It ships only a documented ShareGPT trajectory format and the
   `batch_runner.py` / `trajectory_compressor.py` scripts. Reuse the format, not the code.

---

## 1. `ctx.llm` — Plugin LLM access

Doc: `DG/plugin-llm-access.md` (637 lines). Impl: `agent/plugin_llm.py` (600 lines). `ctx.llm` is a lazy property on `PluginContext`:

```python
# hermes_cli/plugins.py:376-384
@property
def llm(self) -> Any:
    if self._llm is None:
        from agent.plugin_llm import PluginLlm
        self._llm = PluginLlm(plugin_id=self.plugin_id)
    return self._llm
```

### Exact signatures (`agent/plugin_llm.py`)

```python
# :446
def complete(self, messages: list[dict[str, Any]], *, provider=None, model=None, temperature=None,
             max_tokens=None, timeout=None, agent_id=None, profile=None, purpose=None, task=None
            ) -> PluginLlmCompleteResult
# :460
def complete_structured(self, *, instructions: str, input: Sequence[PluginLlmInput], json_schema=None,
             json_mode: bool = False, schema_name=None, system_prompt=None, provider=None, model=None,
             temperature=None, max_tokens=None, timeout=None, agent_id=None, profile=None, purpose=None,
             task=None) -> PluginLlmStructuredResult
# :476 / :486
async def acomplete(...same as complete...) -> PluginLlmCompleteResult
async def acomplete_structured(...same as complete_structured...) -> PluginLlmStructuredResult
```

- `input` blocks: `{"type":"text","text":...}` or `{"type":"image","data":bytes|"url":...,"mime_type":...}`, or the dataclasses
  `PluginLlmTextInput` / `PluginLlmImageInput` (`:24-41`). Unknown shapes raise `ValueError` (`:239-262`).
- `complete_structured` raises `ValueError` on empty `instructions` or an empty `input` (`:419-422`).
- JSON lane: sends `extra_body.response_format` = `json_schema` (strict False) or `json_object` (`:403-411`), adds a system line
  "Respond with a single JSON object…" (`:285-287`), strips code fences (`:307-313`), then `json.loads`.
  **Schema validation only runs if `jsonschema` is importable** (`:326-333`). `jsonschema` is NOT a Hermes core dep, so validate yourself.
  A schema **violation raises `ValueError`**, while unparseable JSON returns `parsed=None, content_type="text"` (`:316-334`).

### Result objects (`agent/plugin_llm.py:48-86`)

```python
@dataclass class PluginLlmUsage: input_tokens=0; output_tokens=0; total_tokens=0;
                                 cache_read_tokens=0; cache_write_tokens=0; cost_usd: Optional[float]=None
@dataclass class PluginLlmCompleteResult: text: str; provider: str; model: str; agent_id: str;
                                          usage: PluginLlmUsage; audit: dict   # plugin_id, purpose, profile, task
@dataclass class PluginLlmStructuredResult: (same) + parsed: Optional[Any]=None; content_type: str="text"
                                            # audit also has schema_name
```

`cost_usd` is documented (`plugin-llm-access.md:389-391`), but `_extract_usage` (`:337-361`) never sets it, so it is always `None`.
Track spend from tokens.

### Budget / limits

- **There is no per-plugin token or cost budget** in `plugin_llm.py`. Doc: "Every call runs against the user's paid provider. Don't loop on
  `complete()`… without thinking about token spend" (`plugin-llm-access.md:518-520`). Hypnos must enforce its own nightly budget.
- "Bounded. Single sync or async call. No streaming, no tool loops" (`:88-91`).
- Concurrency cap exists only per **aux task**: `auxiliary.<task>.max_concurrency` (`agent/auxiliary_client.py:6324-6351`; `UG/configuration.md:1785`).
- Timeout: `timeout=` arg, else `auxiliary.<task>.timeout`, else the global aux default (`plugin-llm-access.md:496-497`).
- Fallback chain on 5xx/429 is applied by the host (`:493-495`). Errors otherwise propagate from `auxiliary_client.call_llm()` (`:513-517`).
- Audit: one INFO line per call in `agent.log`, `plugin_llm.<method> plugin=… provider=… model=… task=… purpose=… tokens=N` (`plugin_llm.py:532-543`).

### Trust gate and capability ids

- Default (no config): may call all four methods on the user's **active** provider/model and set request-shaping args
  (`plugin-llm-access.md:395-407`). `provider=`, `model=`, `agent_id=` and `profile=` raise `PluginLlmTrustError(PermissionError)` (`plugin_llm.py:144-181`).
- Policy is re-read from `plugins.entries.<id>.llm.*` on **every call** (`:121-141`): `allow_provider_override`, `allowed_providers`,
  `allow_model_override`, `allowed_models`, `allow_agent_id_override`, `allow_profile_override`, `allow_task_override`.
- Capability ids (`hermes_cli/plugin_capabilities.py:30-53`): `tools.override`, `llm.provider_override`, `llm.model_override`,
  `llm.agent_id_override`, `llm.profile_override`, `llm.task_override`, `llm.side_question`, `gateway.platform_actions`.
  A consent grant also writes the legacy `allow_*` key (`record_consent`, `:168`), and that key is what `plugin_llm` reads.
- **Hypnos needs no capability.** For a separately routable teacher model, register its own aux task, which needs no capability (own tasks always allowed, `plugin_llm.py:207-236`):

```python
ctx.register_auxiliary_task("hypnos_teacher", display_name="Hypnos teacher",
                            description="Habit extraction, variant generation, judging",
                            defaults={"timeout": 120})            # optional inherit_from="compression"
ctx.llm.complete_structured(..., task="hypnos_teacher", purpose="hypnos.extract")
```
  Signature: `register_auxiliary_task(self, key, *, display_name, description, defaults=None, inherit_from=None)` (`hermes_cli/plugins.py:855-876`).
  The user can then pin `auxiliary.hypnos_teacher.{provider,model,max_concurrency}`, and the task shows up in `hermes model` → auxiliary picker (`plugin-llm-access.md:319-330`).

### `ctx.side_question` (not useful for Hypnos)

`side_question(self, token, prompt, *, timeout=15.0) -> Optional[str]` (`hermes_cli/plugins_capability_ctx.py:27`). It needs the `llm.side_question`
capability and a `post_llm_call` token. "Cron runs and delegated subagent turns always get `None`" (`plugin-llm-access.md:570-573`). Token expires after 60 s,
prompt ≤ 4096 chars, timeout ≤ 60 s (`:574-584`). Hypnos's offline work never has a live token.

### Separate worker process?

- `PluginLlm` is "Constructed by `PluginContext` … plugins should not instantiate it directly" (`plugin_llm.py:427-434`). It needs the Hermes
  import tree (`agent.auxiliary_client`, `hermes_cli.config`) and the profile's `auth.json`/`.env`, so it is unusable from a torch sidecar venv.
- It **does work** in any Hermes process that ran plugin discovery, including `hermes <plugin-cli-cmd>`. `hermes_cli/main.py:3239-3266` calls
  `discover_plugins()` for any non-builtin first token (`_plugin_cli_discovery_needed`, `:2771-2779`), so `register(ctx)` runs and a CLI handler closing over
  `ctx` can call `ctx.llm`. The CLI loads the profile `.env` at import (`hermes_cli/env_loader.py:27,63`).
- Plugin host isolation: `llm` is in `HOST_REMOTE_FACADES` (`hermes_cli/plugin_isolation.py:55-57`), so calls are proxied to the Hermes side and still work.
- Cron/gateway: nothing in `plugin_llm` checks cron or gateway, so calls work in hooks and tools fired during a cron run. In async gateway code use
  `acomplete*`, because sync `complete` blocks the loop. Hooks are bounded by `plugins.hook_callback_timeout` (30 s default, `UG/features/hooks.md:396`);
  "post_llm_call is time-bounded: never wait for the model inside the hook" (`plugin-llm-access.md:546`).

---

## 2. Plugin storage

Docs: `PI:1153-1173` ("Store durable state"), `PI:957-988` ("Store settings and runtime state"). Impl: `plugins/plugin_storage.py` (public module).

```python
# plugins/plugin_storage.py:27,36
def plugin_data_dir(name: str) -> Path        # <get_hermes_home()>/plugin-data/<name>/, mkdir on call; name ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$, no ".."
def plugin_db(name: str, filename: str = "data.db") -> sqlite3.Connection
    # open_db(..., foreign_keys=True, row_factory=None, check_same_thread=False); WAL via apply_wal_with_fallback; busy_timeout 5000ms
```

- Import path: `from plugins.plugin_storage import plugin_data_dir, plugin_db` (`PI:1161`). "survives `hermes plugins update` and `remove` and follows the active profile" (`PI:52`).
- `plugin_data_dir` "resolves `get_hermes_home()` on every call so it follows the active profile — don't cache across profile switches" (`plugin_storage.py:28-29`).
- `open_db` (`hermes_cli/sqlite_util.py:18-58`): `busy_timeout_ms=5000`. WAL "network filesystems degrade to DELETE" (`plugin_storage.py:43-44`). The caller owns transactions.
- "Secrets don't belong here" (`PI:1171-1173`).
- **From a subprocess:** the helpers import `hermes_constants` and `hermes_cli.sqlite_util`, so they work only in a Hermes interpreter. A sidecar venv should open the
  same absolute path with stdlib `sqlite3` (WAL persists in the file; set `PRAGMA busy_timeout`). The **spawner must pass `HERMES_HOME`** (or the resolved
  db path) explicitly: "a subprocess that drops `HERMES_HOME` can fall back to the default profile … subprocess spawners should pass `HERMES_HOME` explicitly"
  (`DG/session-storage.md:36-41`). `get_hermes_home()` = context-local override → `HERMES_HOME` → `~/.hermes` (`hermes_constants.py:194-201`). Under a
  multiplexed gateway the override is context-local, so resolve it in the spawning thread.
- **`ctx.state`** (`hermes_cli/plugins.py:282-285`, `hermes_cli/plugins_state.py:134-220`): `get(key, default=None)` and `set(key, value)`; JSON, 10 MiB quota
  (`plugins_state.py:19`), file lock + atomic write, keys `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`. Dir = `plugin-data/<_portable_data_namespace(plugin_id)>`
  = `agent-plugin-hypnos-<sha256[:8]>` (`plugins_state.py:93-101`, `plugins_manifest.py:59-63`), which is **not** `plugin-data/hypnos/`. Use it for small cursors only.

---

## 3. Config

Manifest `config_schema` (`PI:622`, form table `PI:1000-1018`; impl `hermes_cli/plugins_settings.py:23-29`):

- Types: `str`/`string`, `int`/`integer`, `float`/`number`, `bool`/`boolean`, `list`/`array`, `dict`/`object`, `secret`. `choices:`/`enum:` turns `str` into a dropdown.
  Per-key `label`/`title`, `description`, `default`, `required`. Mismatches "log actionable warnings … never load failures" (`PI:622`).
- `secret`: `{type: secret, env: HYPNOS_FOO}`, stored in `.env` (default name `<PLUGIN_ID>_<KEY>` upper-snaked, `plugins_settings.py:70-77`). "Secrets never touch
  `config.yaml`"; read with `get_secret("HYPNOS_FOO")` from `agent.secret_scope` (`PI:1020-1024`).
- Desktop renders the form automatically and broadcasts `plugin.<id>.settings.changed` on save (`PI:1050-1055`).

```python
# hermes_cli/plugins.py:266,278
def get_config(self, key: str, default: Any = None) -> Any   # reads plugins.entries.<id>.settings.<dotted key>; legacy .config fallback
def set_config(self, key: str, value: Any) -> None           # atomic write into settings subtree
```
- Keys must be plugin-relative dotted segments `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`. **Reserved first segment: `model`, `plugins`, `security`, `settings`** →
  `ValueError` (`hermes_cli/plugins_state.py:16-17,24-38`).
- `get_config` returns the caller's `default` when unset. It does **not** merge schema defaults (`plugins.py:266-276`).
- Outside a ctx (e.g. dashboard API): `load_config_readonly()` from `hermes_cli.config` and read `plugins.entries.<id>.settings`, falling back to schema defaults yourself (`PI:1053-1055`).
- `config_keys:` is only for **core** config sections the plugin reads (`PI:623`). Not needed.

---

## 4. Slash commands, CLI commands, confirmation

### CLI (`PI:1718-1754`)
```python
# hermes_cli/plugins.py:635
def register_cli_command(self, name: str, help: str, setup_fn: Callable, handler_fn: Callable | None = None,
                         description: str = "") -> PluginRegistration
```
**argparse.** `setup_fn(subparser)` builds the tree, and `handler_fn` becomes `set_defaults(func=…)` (`hermes_cli/main.py:3235-3236`). The handler receives an argparse `Namespace`.
It runs in the user's terminal process. **Skipped under `plugins.isolation: host`** ("`hermes <command>` subcommands are wired into the local CLI parser",
`hermes_cli/plugin_isolation.py:50-52`).

### Slash (`PI:1756-1830`)
```python
# hermes_cli/plugins.py:649  (doc PI:1777 omits argument_mode)
def register_command(self, name: str, handler: Callable, description: str = "", args_hint: str = "",
                     argument_mode: str | None = None) -> Optional[PluginRegistration]
```
- Handler: `fn(raw_args: str) -> str | None`, sync or async. It may declare kwargs `session_id`, `session_key`, `platform`, `source` (`PI:1796-1805`).
  A name colliding with a built-in is silently rejected (`plugins.py:660-665`).
- **Runs in the gateway process**, even when typed in classic `hermes chat`. "Return the text to show. `print()` writes to the gateway's stdout",
  use `agent.runtime_cwd.resolve_agent_cwd()`, not `os.getcwd()` (`PI:1818-1830`). Sync handlers run in the gateway executor, so never block for minutes; spawn the worker.
- `ctx.dispatch_tool(name, args, *, parent_agent=None) -> str` (`PI:1886`; impl `plugins.py:676`) goes "through the normal approval, redaction, and budget pipelines" (`PI:955`).

### Asking for confirmation that fails closed
- Slash: `ctx.ask_choice(question, choices) -> Optional[str]` (`plugins.py:618`, `hermes_cli/plugin_choices.py:81-96`). It returns `None` "when nothing can ask: a hook, a cron
  job, an async handler" or on cancel/timeout (`agent.clarify_timeout`) (`PI:1842`). Pattern: `if ctx.ask_choice("Delete N samples?", ["yes","no"]) != "yes": return "Cancelled."`.
  Accept an explicit `--yes` arg as the non-interactive path.
- CLI: the public helper `hermes_cli.setup:prompt_yes_no(question, default=True, description=None) -> bool` (`hermes_cli/setup.py:287`). It returns `default` when
  `HERMES_NONINTERACTIVE=1` or on EOF/closed stdin (`:288-309`). **Pass `default=False`** for destructive ops and also check `sys.stdin.isatty()`.
- Catalog rule 12: "Anything that waits for a person (a prompt, an OAuth browser flow) fails cleanly or times out under cron, the messaging gateway and other unattended runs
  instead of hanging the agent" (`DG/plugins/catalog-submission.md:166-171`).

### Other user-facing surfaces
- `ctx.post_message(text) -> PluginMessage` / `.edit(text)` posts live progress into the **bound** conversation only. It never raises. `no_conversation` for timers/cron/threads
  started without `contextvars.copy_context()` (`UG/features/plugins.md:1001-1053`). Unsupported under host isolation.
- `ctx.inject_message(content, role="user", *, session_key=None, origin=None) -> bool` (`plugins.py:588`). Gateway injection needs `plugins.entries.<id>.allow_gateway_injection: true` (`plugins.py:624-632`).

---

## 5. Scheduling nightly work

### Facts
- Cron fires **only from the gateway**: "The gateway ticks the scheduler every 60 seconds" (`UG/features/cron.md:423`). "In CLI mode, cron jobs only fire when `hermes cron`
  commands are run or during active CLI sessions" (`DG/cron-internals.md:274`).
- Under systemd each due job runs in an external worker `python -m cron.scheduler` inside `systemd-run --user --scope`, so a gateway restart doesn't kill it
  (`cron.md:463-476`). The gateway unit uses `KillMode=mixed` (`hermes_cli/gateway.py:3276`), so **a child spawned directly from the gateway is killed on gateway restart.**
- Chronos (`cron.provider: chronos`) lets hosted gateways **scale to zero** and wake only on a cron fire (`DG/cron-internals.md:281-321`; `DG/chronos-managed-cron-contract.md`).
  An in-process plugin timer would not fire there. Plugins also load in every Hermes process (CLI, TUI, gateway, cron/kanban workers), so a timer would run N times. **Do not self-schedule.**
- `hermes pause` blocks every scheduled fire (`cron.md:322`).
- Cron sessions: fresh agent, MEMORY/USER load, "cron jobs cannot ask clarifying questions", `cronjob` toolset disabled (recursion guard) (`DG/cron-internals.md:323-332,438-443`).
  Dangerous commands follow `approvals.cron_mode` (default `deny`) (`UG/security.md:36,50`). `side_question` gets `None` and `ask_choice` returns `None`.
- Agent-job toolsets come from `hermes tools` → cron platform or the job's `enabled_toolsets` (`cron.md:1313-1334`). A plugin toolset must be enabled there for the cron agent to see a Hypnos tool.
- Per-tool-call deadline is `timeouts.tools.sequential_call`, default = `tools.concurrent_batch` = **420 s** (`agent/tool_executor.py:138,189-198,838-845`). Agent jobs also have an
  inactivity budget `HERMES_CRON_TIMEOUT` of 600 s idle (`DG/cron-internals.md:367`).
- **No-agent jobs** (`cron.md:979-1011`; `website/docs/guides/cron-script-only.md:140-196`): `--no-agent --script X`. The script must resolve inside `$HERMES_HOME/scripts/`, `.py` runs under
  `sys.executable` (Hermes venv) or `--interpreter <abs venv python>`, and the env is sanitized ("provider API credentials … are **not** inherited";
  `tools/environments/local.py:333`). Stdout is delivered verbatim, empty stdout is silent, non-zero exit or timeout raises an alert, and `{"wakeAgent": false}` on the last line is silent. Timeout
  `cron.script_timeout_seconds` / `HERMES_CRON_SCRIPT_TIMEOUT`, **default 3600 s** (`DG/cron-internals.md:360-367`). Zero tokens.
- `ctx.current_cron_execution() -> CronExecution | None` (`plugins.py:411`; `PI:1936-1958`). Fields `job_id, job_name, execution_id, source, scheduled_instant, started_at,
  profile` (`cron/execution_identity.py:18-25`). `scheduled_instant is None` means a manual run. It works in hooks and tool handlers during the run.
  Subagents and tool subprocesses see `None`.
- Blueprints: `register_automation_blueprint(key, *, title, description, schedule_template, prompt_template, category="general", slots=(), deliver_default="origin",
  skills=(), tags=())` (`hermes_cli/plugins_content.py:69`; `PI:1960-1991`). Key becomes `hypnos:<key>`. Slot types `time|enum|weekdays|text`. A blueprint creates an **agent**
  prompt job, with no `no_agent`/`script`/`enabled_toolsets` fields (`cron/blueprint_catalog.py:67-86`). "Jobs outlive the plugin" (`PI:1989`). No capability needed, and it works under host isolation.
- Programmatic creation: the `cronjob_manage` tool (`tools/cronjob_tools.py:1263`) via `ctx.dispatch_tool("cronjob_manage", {"action":"create", ...,"no_agent":True,"script":...})`
  from a slash command, or shell out to `hermes cron create`. `--interpreter` is **user-only** ("the agent's `cronjob` tool can't", `cron-script-only.md:184`).
  Never patch `jobs.json` (`cron.md:1460`, `UG/security.md:419`).

### Options for Hypnos (ranked)
1. **No-agent cron job, created only on explicit user action** (`hermes hypnos schedule` / `/hypnos schedule`, behind confirmation). It writes a tiny shim
   `$HERMES_HOME/scripts/hypnos-sleep.py` that execs `[sys.executable, "-m", "hermes_cli.main", "hypnos", "sleep", "--scheduled"]`, then
   `hermes cron create "0 3 * * *" --no-agent --script hypnos-sleep.py --name hypnos-nightly --deliver local`.
   - Pros: zero tokens, runs in the cron worker's systemd scope, the stdout summary becomes the "morning report" delivery, and failures alert. Respects `hermes pause` and Chronos.
   - Constraints: the whole run fits in 3600 s unless the user raises `cron.script_timeout_seconds`, so make phases checkpointed and resumable. The script dir is outside plugin data, so
     disclose it (catalog rule 13). The sanitized env should still carry `HERMES_HOME`: `build_subprocess_env` says "_sanitize_subprocess_env (profile home inherent)" (`tools/environments/local.py:381-389`), and the sanitizer prepends the Hermes bin dir so bare `hermes` resolves (`:324-330`). Verify that empirically. The `hermes` child re-loads the profile `.env` itself, so `ctx.llm` works.
2. **Automation Blueprint** `hypnos:nightly-sleep` whose prompt makes the agent call a `hypnos_sleep` tool. The tool checks `ctx.current_cron_execution()`, spawns the detached orchestrator
   and returns in under 420 s. This is the most plugin-native and discoverable path (`/blueprint`, Desktop, dashboard). Costs one agent turn per night and needs the Hypnos toolset enabled for the cron platform.
3. User-created cron (document the one-liner). Manual `hermes hypnos sleep --now` always available.

### Idle detection
No documented idle API (grep of plugins/cron/hooks docs). Build it from hooks:
- Stamp `last_activity` in plugin DB from `pre_llm_call`, `post_llm_call`, `post_tool_call`, `on_session_start`/`on_session_end`. Use cheap writes only, since hooks are bounded at 30 s.
- At sleep start and periodically during training, require `now - last_activity >= idle_minutes` and low host load (`psutil` is a Hermes core dep). If a user turn arrives
  mid-run, pause or yield the GPU job. The worker polls the DB flag that the hooks set.
- `/heartbeat` "idle" is per-session turn idleness (`UG/features/heartbeat.md:9,44`), so it doesn't fit.

---

## 6. Background processes and subprocesses

- No dedicated plugin API for long-running processes. What exists:
  - `ctx.spawn_task(coro, *, name=None) -> asyncio.Task` is a supervised task cancelled on unload. It needs a running loop and is **unsupported under host isolation** (`plugins.py:425-435`; `plugin_host.py:407`).
  - `ctx.on_unload(callback) -> PluginRegistration` (`plugins.py:416`), also unsupported under host isolation.
  - Background threads: `agent.memory_provider.spawn_context_thread(target, *, name, daemon=True, args=(), kwargs=None)` (`agent/memory_provider.py:29`), "never a bare
    `threading.Thread`", because profile/secret scope lives in contextvars (`DG/memory-provider-plugin.md:422`; `plugins/AGENTS.md:113-115`).
  - Plugin Doctor: "plugin code still executes in-process with the current user's permissions and can spawn subprocesses" (`PI:349-351`).
  - Plugin guard: plugins "are *expected* to … spawn subprocesses" (`tools/plugin_guard.py:4-5`).
- Spawning `hermes` safely: use `[sys.executable, "-m", "hermes_cli.main", ...]`. The module form beats PATH lookup because "a PATH-first lookup lets an attacker-planted `hermes`
  shadow the running install" (`hermes_cli/kanban_db_dispatch.py:2467-2471,2531-2545`). Pass `HERMES_HOME` (and `-p <profile>` if needed).
- Sidecar venv: declare `python_runtime: external` ("the plugin manages its own interpreter/venv (sidecar pattern); Hermes installs nothing", `PI:621,700`). Survival contract:
  `$HERMES_HOME/plugins/**` survives Hermes updates but **not** `hermes plugins update/remove` (`DG/memory-provider-plugin.md:554-561`). Put the venv and model weights under
  `plugin_data_dir("hypnos")`, which survives both.
- **Disclosure (catalog rule 13, `catalog-submission.md:172-177`):** "network calls to third-party services, reads outside the plugin's own data, shell commands, long-running
  background processes, stored credentials". Rule 17 (`:200-222`): a binary/weights "a user's own machine fetches at setup time is allowed only from the upstream tool's
  official source, pinned by checksum, and disclosed". Rule 3: no self-updating code. Scanner: `docker pull`, `pip install` without `==` and `requests.get("https://…")` score
  `medium` (informational, not caution) (`tools/skills_guard.py:420-428,953-956`).
- Local servers: no rule found beyond disclosure. Bind loopback only.

---

## 7. Secrets and redaction

- `agent.secret_scope.get_secret(name: str, default: Optional[str] = None) -> Optional[str]` (`agent/secret_scope.py:230`). It honors the profile scope and fails closed under multiplex.
  `get_secret_str(name, default="") -> str` is at `:272`. It is public, and catalog rule 11 makes it **mandatory** ("never reads `.env` files, Hermes's `auth.json`…", `catalog-submission.md:149-165`).
- `hermes_cli.plugin_provider_requests.credentialed_provider_request(provider, method, url, *, json=None, headers=None, timeout=30.0) -> ProviderResponse`
  (`hermes_cli/plugin_provider_requests.py:134`; `PI:479-506`). It is only for `requires_auth: [openai-codex]` or plugin-declared `oauth:` ids, and the caller must be a plugin frame on the stack.
  Hypnos doesn't need it, because `ctx.llm` covers teacher calls.
- `ctx.register_redaction_patterns(patterns) -> int` (`hermes_cli/plugins.py:878-890`) is additive. Each regex must compile and start with ≥ 2 literal chars, with no top-level alternation and no
  nested unbounded quantifiers (`agent/redact.py:1904-1935`). "Plugins can over-redact, never weaken built-ins; `security.redact_secrets: false` applies equally." It is **not in website docs.**
- Public-by-name scrubbers in `agent/redact.py` (module **not** on the `_PUBLIC` list, so validate warns):
  - `redact_sensitive_text(text, *, force=False, code_file=False, file_read=False, secret_file=False, redact_url_credentials=False) -> str` (`:1441`). Use
    `force=True`; otherwise the user's `security.redact_secrets: false` disables it (`:140-160`).
  - `redact_for_egress(text) -> str` (`:1747-1759`) is `force=True` plus a Bearer sweep. It **fails closed** (returns `REDACTION_UNAVAILABLE` if the redactor raises). This is the best fit for "scrub before persisting".
  - `mask_secret(value, *, head=4, tail=4, floor=12, …)` (`:904`), `is_secret_field_name(key)` (`:1136`), `redact_terminal_output(output, command=None, *, force=False)` (`:1762`).
  - Recommendation: call `redact_for_egress` behind a try-import, run Hypnos's own regex pass on top, and disclose the private import. Also strip values of known secret env names (via `get_secret`) as literals.
- Hook payloads: `pre/post_api_request` `request`/`response` are "sanitized, size-capped … (sensitive keys redacted)" (`PI:1558`). `post_tool_call.result` and `post_llm_call` text carry no such
  guarantee, so scrub them yourself.

---

## 8. Terminal / sandbox

- `DG/terminal-environment-plugin.md` covers **providing** a backend (`ctx.register_terminal_environment_provider(TerminalEnvironmentProvider)`, `create_environment()` → object with
  `execute(command, timeout=None, **kw) -> {"output", "exit_code"}`, `cleanup()`; `:38-135`). It has no API for **consuming** a sandbox. Terminal backends "run only in-process" and are refused
  under host isolation (`:238-243`).
- The only consumer path is `ctx.dispatch_tool("terminal", {"command": ...})`. It runs on the user's `terminal.backend`, which can be `local`, i.e. no sandbox. The default Docker backend is
  **one persistent shared container per profile** (`UG/configuration.md:389-397`). Calls go through approvals, `cron_mode: deny` in cron, and the 420 s tool deadline, so they are unsuitable for evaluating untrusted generated commands.
- `tools.environments.docker.DockerEnvironment` is not public (only `tools.environments.base:BaseEnvironment` as a base class).
- **Run `docker run` ourselves**, mirroring Hermes's hardening (`tools/environments/docker.py:322-334,400,822,838`): `--rm --network=none --cap-drop ALL
  --security-opt no-new-privileges --pids-limit <n> --memory/--cpus --read-only --tmpfs /tmp:rw,nosuid,size=…`, plus a non-root `--user`, a pinned image digest and no host mounts
  except a read-only fixture dir. Honor `HERMES_DOCKER_BINARY` (podman support, `UG/configuration.md:447`) if desired. Disclose the Docker use and any image pull (pin by digest, rule 17).
- Don't put Hypnos secrets in container env. Run sandboxed commands with an explicit minimal env.

---

## 9. RL / training tooling

- **No `rl-training.md` exists in v0.23.0 docs.** RL is mentioned only in `website/docs/getting-started/learning-path.md:113-122` (links out to Atropos) and `website/docs/index.mdx:153`
  ("RL training with Atropos"). `pyproject.toml` has **no** atropos/tinker/torch/trl extra (extras list `:225+`). A stale doctor warning remains: "Python 3.11+ recommended for RL Training tools
  (tinker requires >= 3.11)" (`hermes_cli/doctor_platform.py:438`). The `CONTRIBUTING.md:926` pin policy mentions atroposlib/tinker git URLs. **Nothing reusable for LoRA training.**
- `DG/trajectory-format.md` defines ShareGPT-compatible JSONL: `{"conversations":[{"from":"system|human|gpt|tool","value":...}], "timestamp", "model", "completed"}`.
  Batch variant adds `prompt_index, metadata, partial, api_calls, toolsets_used, tool_stats, tool_error_counts`. Normalization rules:
  - Every `gpt` turn has `<think>…</think>` (empty if none).
  - Tool calls become `<tool_call>\n{"name","arguments"}\n</tool_call>`.
  - Tool results are grouped into one `tool` turn of `<tool_response>{"tool_call_id","name","content"}</tool_response>`.
  - The system prompt is regenerated with a `<tools>` block (`:84-205`).
  Saving is library-only (`AIAgent(save_trajectories=True)`, `:216-229`). Writer `agent/trajectory.py:37 save_trajectory(trajectory, model, completed, filename=None)` is not public.
  "Batch runner and RL trajectories are NOT stored [in state.db]" (`DG/session-storage.md:164`).
- `batch_runner.py` (doc `UG/features/batch-processing.md`) runs full agents over a JSONL prompt set, and `trajectory_compressor.py` (CLI via `fire`) compresses trajectories. Both are top-level scripts, not plugin APIs.
- **Plan:** emit Hypnos training data in this ShareGPT format (HF `datasets` loads it, `trajectory-format.md:196-208`) with our own converter. Tool-call formatting then matches Hermes's
  function-calling template, so a fine-tuned student could later be served as a Hermes model.

---

## 10. Session data (read-only)

- Store: `get_hermes_home()/state.db`, WAL (`DG/session-storage.md:1-46,597-604`). "The profile's gateway is the **only writer of canonical sessions** … never by opening `state.db`
  for transcript writes themselves" (`:7-12`).
- `hermes_state` is a **public** module. Constructor: `SessionDB(db_path: Path | None = None, read_only: bool = False)` (`hermes_state.py:584`). The `read_only` flag is undocumented on the page,
  which shows only `SessionDB()` (`session-storage.md:390-397`). Use `read_only=True`.
- Read APIs: `get_session(session_id)` (`hermes_state_sessions.py:834`); `list_sessions_rich(source=None, …, limit=20, offset=0, include_children=False, …)` (`:1303`);
  `get_messages(session_id, include_inactive=False, include_compacted=False, limit=None, offset=0, latest=False, after_id=None, include_ancestors=False)` (`hermes_state_messages.py:1370`);
  `get_messages_as_conversation(session_id, include_ancestors=False, …)` (`:1517`); `search_messages(...)` (`hermes_state_search.py:1257`); `export_session(session_id, include_compacted=False, …)`
  (`hermes_state_portability.py:347`). Schema columns are at `session-storage.md:167-290` (messages: `role, content, tool_calls(JSON), tool_name, reasoning, timestamp, …`).
- Prefer **live capture via hooks** (documented): `post_llm_call(session_id, user_message, assistant_response, conversation_history, model, platform, **kw)` (successful turns only) and
  `post_tool_call(tool_name, args, result, task_id, duration_ms, **kw)` (`UG/features/hooks.md:627-650,768-790`). Use `SessionDB(read_only=True)` only for backfill.
- Honor deletions: `on_session_delete(session_id, reason, profile, deleted_ids?, sessions_dir?)` (`PI:1545`) means purge captured rows. Also `post_compaction` and `on_session_reset`.

---

## 11. Memory files

- Location: `get_hermes_home() / "memories" / {"MEMORY.md","USER.md"}` (`UG/features/memory.md:20`; `tools/memory_tool.py:38-40 get_memory_dir()`, not public). A named profile uses
  `~/.hermes/profiles/<name>/memories/` (`memory.md:82`). `DG/prompt-assembly.md:312` says `~/.hermes/MEMORY.md`, which is stale; trust source.
- Format: entries separated by `§` lines, multiline allowed; caps 2,200 / 1,375 chars (`memory.md:14-55`). Either store can be disabled (`memory_enabled` / user-profile toggle, `memory.md:305-309`).
  Files may be absent.
- **No plugin API documents reading them.** Plugins read them as plain files via `hermes_constants.get_hermes_home()` (public). Read-only is safe, and the doc warns only against two
  **writers** (`memory.md:22-24`). Built-in writes can be observed through a memory-provider `on_memory_write` (`DG/memory-provider-plugin.md:179`), but Hypnos isn't a memory provider.
  Disclose "reads outside the plugin's own data" (rule 13).

---

## 12. Python dependencies

- Declare in `pyproject.toml` `[project].dependencies` (wins) or `python_dependencies` in `plugin.yaml` (`PI:660-709`). Resolved by PM together with Hermes core, extras and every enabled plugin; installed
  only on install/enable **with consent**; atomic, so failure keeps the old env. `--yes-deps` exists for headless installs. "Hermes's exact pins cannot move" (rule 10). A conflicting pin fails install (`PI:711-722`).
- Version bounds: "Upper bounds on every dependency — `>=floor,<next_major` … `>=0.29,<0.32` for pre-1.0" (`PI:726-727`). Reviewers flag bare floors (`PI:737-739`).
- Runtime installs:
  - `pm.ensure_import("<extra>")` is **only for Hermes `pyproject.toml` extras**, "not an arbitrary package specification" (`PI:1337-1358`), and is gated by `security.allow_lazy_installs` (`PI:705`).
  - "Dependencies installed manually with pip are not durable PM declarations" (`PI:1377-1378`), so don't pip into Hermes's venv.
  - `python_runtime: external` makes Hermes install nothing and ignore the plugin's `pyproject.toml` (`PI:621,700`), so the Hermes-side half must use only stdlib plus Hermes core deps.
  - Core deps include `httpx, pydantic, psutil, requests, ruamel.yaml, jinja2, rich, croniter` (`pyproject.toml:19-224`). **Not** `jsonschema`, `numpy` or `torch`.
- Hypnos approach: Hermes-side code is stdlib plus core deps. The training sidecar venv lives in `plugin_data_dir`, gets created by an explicit `hermes hypnos setup` with consent, uses a pinned lock
  (`uv --exclude-newer`-style), and is disclosed. `register()` must stay light: per-plugin load deadline `plugins.load_timeout_seconds` is 10 s default (`hermes_cli/plugins_loader.py:40`), so never import torch at register time.

---

## 13. Plugin host isolation matrix (`plugins.isolation: host`, opt-in, default `in_process`)

From `hermes_cli/plugin_isolation.py:35-57` and `UG/features/plugins.md:894-937`:

| Surface | in_process | host |
|---|---|---|
| `ctx.llm`, `ctx.state` | yes | yes (remote facade) |
| `register_command`, `register_hook`, `register_tool`, blueprints | yes | yes |
| `register_cli_command` | yes | **skipped (warning)** |
| `on_unload`, `spawn_task` | yes | **refused** (`plugin_host.py:407`) |
| `post_message`/`apost_message` | yes | **refused** |
| terminal backend providers | yes | refused |

---

## 14. Manifest sketch implied by the above

```yaml
name: hypnos
version: 0.1.0
manifest_version: 2
requires_hermes: ">=0.23.0"        # also in catalog entry (rule 14)
python_runtime: external           # torch sidecar venv under plugin-data/hypnos/
provides_tools: [hypnos_sleep]     # only if using blueprint path
provides_hooks: [pre_llm_call, post_llm_call, post_tool_call, on_session_start, on_session_end, on_session_delete]
config_schema:
  student_model:   {type: str, default: "Qwen/Qwen2.5-0.5B-Instruct", label: "Student base model"}   # NOT `model` (reserved)
  capture_enabled: {type: bool, default: true}
  idle_minutes:    {type: int, default: 60}
  nightly_token_budget: {type: int, default: 200000}
  sandbox_image:   {type: str, default: "", description: "Pinned image digest for command eval"}
# no capabilities: needed (own aux task `hypnos_teacher` covers model routing)
```

## 15. Gaps and unknowns
- `register_redaction_patterns` and `SessionDB(read_only=True)` are source-only, not in the website docs.
- No documented idle signal, and no ctx API to create cron jobs.
- `PluginLlmUsage.cost_usd` is never populated.
- Unverified: whether a detached child started from a no-agent cron script outlives the transient systemd scope after the script exits (cron worker exit). Test before relying on detach-and-return in option 1.
