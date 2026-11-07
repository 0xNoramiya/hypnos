# Hermes v0.23.0 hook recon for Hypnos

Source: `/home/kuda/hackathon/ref/hermes-agent` at tag `v0.23.0` (commit `2bd378ed`). All paths are relative to that repo.
Docs: `website/docs/user-guide/features/hooks.md` (HK), `website/docs/developer-guide/observer-hooks.md` (OH), `website/docs/developer-guide/plugins/index.md` (PI).
Every kwarg list below comes from the call site in the source code. Where the docs disagree, the source wins and the disagreement is flagged.

---

## 0. Key conclusions

1. All six hooks Hypnos plans to use exist. `post_approval_response` is real, observer-only, and carries `choice` plus `tool_call_id` and `turn_id`. It carries **no free-text reason**.
2. `post_llm_call` fires **once per user turn**, after the tool loop, and only on successful, non-interrupted turns. It does not fire per LLM API call; the per-call hook is `post_api_request`. Its `conversation_history` holds the turn's assistant `tool_calls` and the tool results.
3. `on_session_end` fires **once per turn** (canonical site `agent/turn_finalizer.py:886`), despite its name. The real teardown hook is `on_session_finalize`, and it does not fire on a crash, for cron runs, or on idle eviction.
4. **`pre_tool_call` fails closed.** If the callback raises or times out, the user's tool call is **blocked**, and a timeout puts every later call into a 60 s block window. Hypnos does not need `pre_tool_call` at all (see §9).
5. Terminal results contain no separate stdout/stderr. `post_tool_call.result` is a JSON string `{"output": <stdout+stderr combined, ANSI-stripped, redacted, head/tail-truncated>, "exit_code": int, "error": str|null, ...}`. A denied command gives `exit_code: -1`, `status: "blocked"`, and an `error` that starts with `"BLOCKED: "`. The hook kwarg `status` for a denial is `"error"`, not `"blocked"`.
6. A deny reason exists only on messaging-gateway `/deny <reason>` (at most 280 chars). It never reaches approval hooks. It is spliced into the tool result `error` text as ` Reason given by the user: "<reason>".` The CLI, TUI, buttons, and API server have no reason channel.
7. After a denial the model is told "Do NOT retry ... Stop ... wait for the user". So "deny, then corrected command" normally spans **two turns**, and the correction is the next `pre_llm_call.user_message`. No id links the denied call to the later corrected call; Hypnos must pair them heuristically.
8. **In v0.23 the classic CLI runs every turn inside the local gateway process** (`GatewayAgent`). Plugin hooks fire there, in one long-lived, multi-session, possibly multi-profile process, with parallel tool threads. Hypnos must be thread-safe and must key all state by `session_id`.
9. Bounded hooks (`post_tool_call`, `pre_llm_call`, `post_llm_call`, `on_session_end`, ...) run on a **new daemon thread per invocation** while the agent thread waits. Approval hooks and `on_session_finalize`/`on_session_reset` run synchronously on the caller thread with **no** timeout.
10. Hermes provides no turn index. Count `pre_llm_call` per `session_id`, or count user messages in `conversation_history`.

---

## 1. Hook inventory and contracts

### 1.1 All plugin hook names (46)

`VALID_HOOKS` is defined at `hermes_cli/plugins.py:109-217`; verified via AST, `len == 46`:

```
agent_loop_stopped, api_request_error, gateway_platform_event, kanban_task_blocked, kanban_task_claimed,
kanban_task_completed, on_human_input_request, on_human_input_resolved, on_interim_message,
on_kanban_dispatch_tick, on_kanban_task_updated, on_kanban_worker_exited, on_kanban_worker_spawned,
on_kanban_worker_stale_claim, on_room_member_activity, on_session_delete, on_session_end,
on_session_finalize, on_session_reset, on_session_start, on_skill_lifecycle, on_stream_delta,
on_stream_end, on_stream_start, post_api_request, post_approval_response, post_auxiliary_call,
post_compaction, post_gateway_admission, post_llm_call, post_tool_call, pre_api_request,
pre_approval_request, pre_auxiliary_call, pre_command, pre_gateway_dispatch, pre_llm_call,
pre_tool_call, pre_transcription, pre_verify, subagent_start, subagent_stop,
transform_api_error_classification, transform_llm_output, transform_terminal_output, transform_tool_result
```

- Registration: `ctx.register_hook(hook_name, callback, *, hold_stream=False)` (`hermes_cli/plugins.py:935`). An unknown name **warns but is still stored** (`plugins.py:951-954`).
- Middleware is a separate surface: `ctx.register_middleware(kind, cb)` with kinds `tool_request`, `llm_request`, `tool_execution`, and `llm_execution` (PI:1680-1716; `hermes_cli/middleware.py:24`).
- Every payload also carries `telemetry_schema_version="hermes.observer.v1"` (`plugins_dispatch.py:291-292`; `middleware.py:16`).

### 1.2 Dispatch mechanics (all hooks)

| Aspect | Behaviour | Ref |
|---|---|---|
| Entry point | `hermes_cli.lifecycle.invoke_hook` calls first-party observers (`_observe`), then plugins | `hermes_cli/lifecycle.py:30-37` |
| Kwarg narrowing | A callback with `**kwargs` gets the full payload. A narrow signature gets only the names it declares, so a missing field is not fatal | `plugins_dispatch.py:250-263` |
| Sync or async | Sync is the intended form. An `async def` result is resolved via `asyncio.run` or a helper thread with a 30 s cap. Use sync | `plugins_dispatch.py:214-245, 266-273` |
| Bounded hooks | `post_tool_call, transform_terminal_output, transform_tool_result, transform_llm_output, pre_llm_call, post_llm_call, pre_api_request, post_api_request, api_request_error, pre_auxiliary_call, post_auxiliary_call, pre_verify, on_session_start, on_session_end, post_gateway_admission, pre_command, post_compaction`, plus fail-closed `pre_tool_call` | `plugins_dispatch.py:42-52, 60` |
| Bounded execution | **One new `threading.Thread(daemon=True)` per callback per invocation**, run under `contextvars.copy_context()`. The caller blocks on `done.wait(timeout)`; on timeout the worker is abandoned, never joined | `plugins_dispatch.py:388-433` |
| Timeout | `plugins.hook_callback_timeout`: default 30 s, `0` disables the threaded path, maximum 600 | `plugins_dispatch.py:183-184`; `plugins.py:1049-1074` |
| After a timeout | The callback is suppressed for 60 s. At most 3 abandoned workers per callback, after which it is skipped outright | `plugins_dispatch.py:64-66, 363-386` |
| Unbounded hooks | Run inline on the caller thread with no timeout: `on_session_finalize/reset` ("last-chance flush"), `subagent_start`, `pre_gateway_dispatch`, `pre/post_approval_*`, `on_human_input_*`, `kanban_*`. `subagent_stop` is always on the caller thread | `plugins_dispatch.py:26-41, 62` |
| Exceptions | Caught per callback and logged as a WARNING once per distinct (hook, callback, error); repeats go to DEBUG | `plugins_dispatch.py:318-348` |
| Safe mode | A safe-mode worker policy turns every hook off (`invoke_hook` returns `[]`, `has_hook` returns False) | `plugins_dispatch.py:285-288, 559-565` |

### 1.3 Hooks Hypnos needs, plus close substitutes

`ids` = `task_id, session_id, tool_call_id, turn_id, api_request_id`. Each is `""` when unknown: `_CallIds.hook_kwargs` (`model_tools.py:655-666`) and `tool_hook_ids` (`agent/inline_tool_executors.py:20-28`).

#### `pre_llm_call` (directive/control, bounded, fail-open)

- **Fires** once per user turn in `_collect_pre_llm_call_context`, `agent/turn_context.py:849-910`, called at `turn_context.py:1236`. That call comes after the user message is appended to `messages` (`:1181`) and before turn-start compaction (`:1245`).
- **Skipped entirely** when `agent._persist_disabled` is set (`:858`). This covers background-review forks (`agent/background_review.py:1016`).
- **kwargs** (`turn_context.py:862-874`, verbatim):
  ```
  session_id=agent.session_id, task_id=effective_task_id, turn_id=turn_id,
  user_message=original_user_message, conversation_history=list(messages),
  is_first_turn=(not bool(conversation_history)), model=agent.model,
  platform=getattr(agent,"platform",None) or "", parent_session_id=... or "", sender_id=... or ""
  ```
  - `user_message` is `persist_user_message` if set, else the user message (`:1191`). It is a `str`, or a `list` of content parts for multimodal turns (HK:692).
  - `conversation_history` is a shallow `list()` copy whose last element is the current user message, so the dicts are shared and must not be mutated.
  - `parent_session_id` is non-empty for delegated subagent turns.
- **Return**: a `str` or `{"context": str}` is injected into the user message. `{"action": "block", "message": str}` vetoes the turn before inference. `None` means pure observer (`turn_context.py:886-907`; HK:700-706).
- **Errors**: a raise or timeout is logged and skipped, and the turn proceeds (HK:706; PI:1586).

#### `post_llm_call` (observer, bounded)

- **Fires** in `_apply_output_hooks`, `agent/turn_finalizer.py:470-506`. The guard is `if final_response and not interrupted` (`:756`) plus `not _persist_disabled` (`:485`). It runs after `transform_llm_output`, so `assistant_response` is the post-transform text.
- **kwargs** (`turn_finalizer.py:494-505`):
  ```
  session_id, task_id, turn_id, user_message=original_user_message, assistant_response=final_response,
  conversation_history=list(messages), model, platform, side_question_token
  ```
- **Return**: ignored. It does **not** fire on interrupt, on an empty final response, or when the iteration budget runs out with no text (HK:789).

#### `pre_tool_call` (directive/control, bounded, **fail-closed**)

- **Agent loop** (the normal path): `_pre_tool_block`, `agent/tool_executor.py:661-675`, calls `hermes_cli/plugins.py:2019` `_dispatch_pre_tool_call_hooks`, which calls `:1907-1927`.
- **Other path**: `model_tools.py:779-801`. This path is skipped when the executor already fired the hook (`tool_executor.py:1722`, `skip_pre_tool_call_hook=True`).
- **kwargs** (`plugins.py:1923-1927`):
  ```
  tool_name, args (dict; {} if not a dict), task_id, session_id, tool_call_id, turn_id, api_request_id, middleware_trace (list)
  ```
- **Return**:
  - `{"action":"block","message":str}` vetoes the call.
  - `{"action":"approve","message"?,"rule_key"?}` escalates to the human gate.
  - `{"action":"modify","args":{...}}` is shallow-merged into the args.
  - Precedence is block > approve > none (`plugins.py:1928-1959`). Anything else is ignored.
- **Failure semantics** (`plugins_dispatch.py:303-323`):
  - A raise produces `{"action":"block","message":"pre_tool_call plugin callback <name> raised <Exc>: ..."}` (`:89-95`).
  - A timeout or still-running callback produces a block with `"pre_tool_call plugin callback timed out or is still running"` (`:67, 311-312`).
  - A suppressed callback (60 s after a timeout) is "skipped", and each skip also returns a block (`:363-370, 311-312`). HK:592 confirms this.
- **Ordering in the agent loop** (`tool_executor.py:689-748`): scope check → `pre_tool_call` → pruned-arg check → guardrails → terminal approval prep (`prepare_current_terminal`, `:741`) → execute. Inside execute, the terminal handler runs the approval gate.

#### `post_tool_call` (observer, bounded)

- **Emitter**: `model_tools._emit_post_tool_call_hook` (`model_tools.py:687-711`). It only emits when `has_hook("post_tool_call")` is true (`:700`).
- **Agent loop**: one terminal emit per `tool_call_id` via `_ToolCallRef.emit_post` (`tool_executor.py:294-307`) → `inline_tool_executors.py:31-60`. The inner registry emit is suppressed (`tool_executor.py:1708-1712`).
- **kwargs** (`model_tools.py:704-709`):
  ```
  tool_name, args, result, task_id, session_id, tool_call_id, turn_id, api_request_id,
  duration_ms, status, error_type, error_message, middleware_trace
  ```
  `args` is the post-`modify` dict and the same object the agent holds, so do not mutate it.
- **`status`, `error_type`, `error_message` sources**:

  | Case | `status` | `error_type` | `error_message` | Ref |
  |---|---|---|---|---|
  | Caller gave none, `result` JSON has a truthy `"error"` | `"error"` | `"tool_error"` | `str(error)` | `model_tools.py:669-674` |
  | Caller gave none, terminal with non-zero `exit_code` (via `agent.display._detect_tool_failure`) | `"error"` | `"tool_error"` | e.g. `"exit 1"` | `model_tools.py:677-681`; `agent/display.py:1046-1054` |
  | Plugin block | `"blocked"` | `"plugin_block"` | block message | `model_tools.py:801, 957`; `tool_executor.py:703` |
  | Guardrail block | `"blocked"` | `"guardrail_block"` | guardrail message | `tool_executor.py:653-657` |
  | Ctrl-C mid-tool | `"cancelled"` | `"keyboard_interrupt"` | cancel message | `tool_executor.py:309-317` |
  | Handler raised | `"error"` | `type(e).__name__` | `str(e)` | `model_tools.py:975-979` |

- **Doc bug**: HK:646 says it "does not fire if the tool raised". The source does fire, with `status="error"`.
- **Return**: ignored.

#### `pre_approval_request` / `post_approval_response` (observers, **unbounded**, caller thread)

- **Dispatcher**: `tools/approval_context.py:54-73` `_fire_approval_hook`. It never raises. It `setdefault`s `turn_id` and `tool_call_id` from contextvars, and `session_id` when bound. `session_id` is an undocumented extra.
- **Context binding**: the contextvars are bound around tool execution by `model_tools._approval_observability` (`model_tools.py:817-833, 854`) and by terminal batch prep (`agent/terminal_approval_batch.py:54-67`).
- **Base payload**: `command, description, pattern_key, pattern_keys, session_key, surface`. `post_approval_response` adds `choice`.

| Path | Call sites | Surface-specific fields |
|---|---|---|
| CLI in-process prompt | `tools/approval.py:991-996` | `surface="cli"`. `command` is raw for terminal and redacted for `execute_code` (`redact_cli`, `approval.py:792-808, 988-990`) |
| Gateway-routed: messaging, TUI, Desktop, and the **classic CLI in v0.23** | `tools/approval_gateway_wait.py:159-165` (payload), `:236` (pre), `:93-98` (post via `_finish`) | `surface` is `"cli"` for a classic CLI session, otherwise `"gateway"` (`approval_context.py:76-80`). `command` is display-redacted (`approval.py:935, 942`). Post adds `cancelled=<cause>` when the prompt is withdrawn. Coalesced followers add `coalesced=True` (`:117, :137`). Notify failure posts `choice="notify_failed"` (`:244`) |
| Smart (aux LLM) | `tools/approval_smart.py:156-169` | `surface="smart"`. `command` and `description` are **force-redacted**. Post has `choice="smart_approve"\|"smart_deny"` and `decided_by="aux_llm"`. An "escalate" verdict sends no smart post; a human prompt follows |
| Plugin approval transport | `tools/approval_prompt.py:237-252` | `surface=f"transport:{name}"`, plus `request_id` and `request_digest`. `choice` may be `transport_<failure>` |
| Protected instruction-file write | `tools/file_tools_write_guards.py:333-338` | `pattern_key="protected_instruction_file"` |
| MCP elicitation consent | `tools/approval_prompt.py:362-375` | `surface="mcp-elicitation/<server>"` etc. |

- **`choice` values** (HK:1505): `once | session | always | deny | timeout | cancelled | notify_failed | smart_approve | smart_deny`.
- **Return**: ignored. These hooks cannot veto (HK:1459; OH:226-228). They are deliberately unbounded because the approval UX has its own timeout (`plugins_dispatch.py:36-38`). Any time Hypnos spends here delays the prompt or the tool.

#### `on_session_end` (observer, bounded). Fires **per turn**

| Site | Ref | kwargs |
|---|---|---|
| Canonical, end of every `run_conversation` unless `_persist_disabled` | `agent/turn_finalizer.py:885-897` | `session_id, task_id, turn_id, completed, failed, interrupted, turn_exit_reason, model, platform` |
| CLI exit while mid-turn | `hermes_cli/cli_shutdown.py:195-203` | `session_id, completed=False, interrupted=True, model, platform, reason, **extra`. No `turn_id`/`task_id` |
| TUI close or reaper | `tui_gateway/session_lifecycle.py:464-467` | `completed=False, interrupted=True, session_id, model, platform` |

- `turn_exit_reason` examples: `"blocked_by_plugin_pre_llm_call"`, `"budget_exhausted"`, `"guardrail_halt"`, `"interpreter_shutdown"`, ... (assigned in `agent/*`).

#### `on_session_finalize` (observer, **unbounded**; may return a user notice)

- **Core**: `hermes_cli/lifecycle.py:81-97` `finalize_session(**kwargs)`, which fires `_observe`, then Relay close, then the plugin hooks.
- **CLI `/new` and quit**: `cli_session_mixin.py:503-511` passes `{session_id, platform, reason:"session_boundary"}`. Also `cli_shutdown.py:185`.
- **TUI**: `tui_gateway/session_lifecycle.py:54, 476`.
- **Gateway**: `gateway/run_shutdown_session_end.py:56-68` passes `session_id, platform, reason, **extra` (`old_session_id`/`new_session_id` on reset). It runs off-loop with a 10 s cap (`gateway/run.py:3739`).
- **Return**: a `str` or `{"message": str}` is shown as an out-of-band notice (`lifecycle.py:68-78`; HK:1040-1051).
- **Gaps**: not delivered for cron ("cron jobs ... don't finalize sessions", HK:1051) or idle cache eviction (HK:1025), and nothing fires on a crash.

#### `on_session_reset` (observer, unbounded)

- CLI: `cli_session_mixin.py:503-513`, `{session_id, platform, reason:"new_session"}`.
- Gateway: `gateway/slash_commands_session.py:258-260`, `session_id=_new_sid, reason="new_session", platform, old_session_id, new_session_id`.
- TUI: `tui_gateway/session_registry.py:146`.
- Gateway order: create replacement → `on_session_finalize(old)` → `on_session_reset(new)` → `on_session_start(new)` on the first turn (HK:1085).

#### `on_session_start` (observer, bounded)

- `agent/conversation_loop.py:930-938`: `session_id, model, platform`. It fires on the first turn of a new session (no prior history) and is skipped for `_persist_disabled`.

#### Substitutes and complements

| Hook | Use for Hypnos | kwargs (source) | Notes |
|---|---|---|---|
| `on_human_input_request` / `on_human_input_resolved` | Approval wait timing; pairs on `request_id` | `kind, request_id, session_id, session_key, platform, prompt` (+ `outcome`) at `tools/human_input_hooks.py:61-72` | `prompt` is force-redacted. Unbounded. Not fired for smart approvals or coalesced followers (HK:1532). Approval outcomes match `choice` (HK:1560). No `tool_call_id` |
| `transform_terminal_output` | Raw exit code and output of commands that actually ran | `command, output, returncode, task_id, env_type, tool_call_id` at `tools/terminal_tool_result.py:144-146`. `tool_call_id` is undocumented | Bounded. Return `None` to observe only. **`output` is pre-redaction** (HK:1715). Fires for background `process_manage` output with `returncode=None` |
| `transform_tool_result` | Same data as `post_tool_call` | post kwargs minus `middleware_trace` (`model_tools.py:873-875`) | Runs after `post_tool_call`. Return `None` |
| `post_api_request` | Per-LLM-call assistant message and tool calls | HK:461 list; `assistant_message`, sanitized `response`, `assistant_tool_call_count` | One per provider attempt |
| `on_interim_message` | Mid-loop assistant text | `text, already_streamed, turn_id, iteration, session_id, model, provider, surface` | Queued off the hot path (HK:500-544) |
| `post_compaction` | Track `session_id` rotation | `session_id, old_session_id, kind, reason, before_tokens, after_tokens, messages_removed` | `old_session_id=""` when compacted in place |
| `subagent_start` / `subagent_stop` | Link child sessions | HK:478-479 | `subagent_stop` stays on the caller thread |
| `agent_loop_stopped` | Gateway/TUI interrupt | `session_key, platform, reason, invalidation_reason` | Not fired in the plain CLI |
| `pre_command` | Slash-command usage | `surface, command, alias_used, args_raw, session_key, platform` | **Not** fired for `/approve`, `/deny`, `/stop` (HK:483) |

---

## 2. Approval flow

### 2.1 Mechanics

- **Terminal path**: `tools/terminal_tool.py:1114-1150` `_run_approval_guards` → `_check_all_guards` → `tools/approval.py:1177` `check_dangerous_command` → `_run_approval_gate` (`:1048`) → `_human_decision` (`:876-1010`).
- **Gate order**: optional smart (aux-LLM) gate first (`:889-893`), then the selected plugin transport (`:915-928`), then the gateway async wait (`:932-971`) **or** the CLI in-process prompt (`:986-1010`).
- **CLI choices**: the prompt reads a single key, and **any unrecognised input means `deny`** (`tools/approval_prompt.py:162-175`). There is no free-text field.
- **Gateway** `/deny [all] <reason>`: `gateway/slash_commands.py:1225-1243` takes `reason = ...[:280]` and calls `resolve_gateway_approval(..., reason=reason)` (`tools/approval.py:144-178`, which sets `entry.reason`).
  - Buttons and cards use `resolve_gateway_approval_card` with no reason. The API server path has none either (`gateway/platforms/api_server_runs.py:1396`).
- **Model-facing denial text**: `approval.py:781-786, 804`:
  `"BLOCKED: User denied this command. The user has NOT consented to this action. Do NOT retry this command, do NOT rephrase it, and do NOT attempt the same outcome via a different command. Stop the current workflow and wait for the user to respond ..."`
  - Gateway variant (`:968-970`): `"BLOCKED: Command denied by user. Reason given by the user: \"<reason>\"." + _STOP_COMMAND`.
- **Denial dict**: `approval.py:576-582`, `{"approved": False, "message", "pattern_key", "description", "outcome": "denied"|"timeout"|"cancelled"|"notify_failed", "user_consent": False, "user_summary", ["deny_reason"]}`. `deny_reason` is gateway-only (`:901-905`).
- **Silent approvals** (no approval hooks fire):
  - session or always grants already in place
  - `command_allowlist`
  - yolo
  - smart-approve (which fires only `surface="smart"` hooks)

  The terminal result still gets an `approval` note for pre-approved, allowlisted, and smart cases (`terminal_tool.py:1139-1149`; `tools/approval_provenance.py:39-49`).

### 2.2 What a denial looks like to hooks

**`post_approval_response`**: `choice="deny"`, plus `command, description, pattern_key, pattern_keys, session_key, surface, turn_id, tool_call_id, [session_id]`. There is no reason field. In the gateway `_finish` (`approval_gateway_wait.py:93-98`) the reason goes only into the return dict, not the hook.

**`post_tool_call`** for the denied terminal call:

```
tool_name="terminal", args={"command": "...", ...}, tool_call_id=C1, turn_id=T1,
status="error", error_type="tool_error", error_message="BLOCKED: User denied this command. ...",
result='{"output": "", "exit_code": -1, "error": "BLOCKED: ...", "status": "blocked", "user_summary": "<i18n sentence>"}'
```

- The result shape comes from `terminal_tool.py:1137-1138` and `_error_json` at `:1064-1070`.
- `status` is `"error"` because the JSON has a truthy `error` (`model_tools.py:673-674`). The kwarg `status="blocked"` is reserved for plugin and guardrail blocks.
- The `"status":"blocked"` **inside `result`** is what marks a denial.
- A pending gateway ask-mode decision instead returns `"status":"pending_approval"` with `approval_pending: true` (`terminal_tool.py:1122-1131`).

**Approved run**: the result JSON contains `"approval": "Command required approval (<desc>) and was approved by the user."` (`terminal_tool.py:1140-1144`; `terminal_tool_result.py:269`).

### 2.3 Hook stream: deny, then the user corrects, then a corrected command is approved

This is the typical path for a CLI or gateway session in v0.23. Events are listed in firing order.

```
TURN N (turn_id=T1)
 pre_llm_call        session_id=S turn_id=T1 task_id=K1 user_message="clean the build" conversation_history=[..., user]
 [on_stream_*/post_api_request ...]
 pre_tool_call       tool_name=terminal args={command:"rm -rf build"} tool_call_id=C1 turn_id=T1
 on_human_input_request  kind=approval request_id=H1 prompt=<redacted cmd>     # gateway: fires BEFORE pre_approval (approval_gateway_wait.py:234-236)
 pre_approval_request    command description pattern_key pattern_keys session_key surface turn_id=T1 tool_call_id=C1 [session_id]
 post_approval_response  ... choice="deny"                                      # gateway: inside the human_input block (:264)
 on_human_input_resolved request_id=H1 outcome="deny"
 post_tool_call      tool_call_id=C1 status="error" error_message="BLOCKED: ..." result={"exit_code":-1,"status":"blocked",...}
 transform_tool_result tool_call_id=C1 ...
 post_llm_call       turn_id=T1 assistant_response="Understood, I won't ..." conversation_history=[...assistant(tool_calls C1), tool(C1), assistant]
 on_session_end      turn_id=T1 completed=True interrupted=False turn_exit_reason=...
TURN N+1 (turn_id=T2)
 pre_llm_call        turn_id=T2 user_message="only delete build/*.o"   <-- correction text
 pre_tool_call       C2 args={command:"rm build/*.o"}
 [on_human_input_request] pre_approval_request(C2) post_approval_response(C2, choice="once") [on_human_input_resolved]
 transform_terminal_output command=... output=... returncode=0 tool_call_id=C2
 post_tool_call      C2 status="ok" result={"output":..., "exit_code":0, "error":null, "approval":"Command required approval ..."}
 post_llm_call(T2); on_session_end(T2)
```

- **CLI in-process ordering** (`approval.py:993-996` wrapping `approval_prompt.py:48-54`): `pre_approval_request` → `on_human_input_request` → `on_human_input_resolved` → `post_approval_response`.
- **Single-turn variant**: possible only if the model ignores the stop instruction. It would show C1 denied and C2 approved under the same `turn_id`.
- **Gateway `/deny <reason>` variant**: the reason shows up only in C1's `post_tool_call.result["error"]` and `error_message`. Regex: `Reason given by the user: "(.*)"\.`

### 2.4 Correlation keys

| Key | Scope | Present on | Notes |
|---|---|---|---|
| `tool_call_id` | One tool call | `pre_tool_call`, approval hooks (via contextvar), `post_tool_call`, `transform_tool_result`, `transform_terminal_output` | **Primary join for approval to tool call.** Connector batch entries get the suffix `"#<n>"` (`tools/connectors/dispatch.py:48`). Hermes's own Relay metrics join approvals this way (`hermes_cli/observability/relay_shared_metrics.py:486-508`). Do not join on `command` text: approval `command` may be redacted or have a trailing `&` stripped (`terminal_approval_batch.py:57-61`) |
| `turn_id` | One `run_conversation` | All turn, tool, API, and approval hooks; `on_session_end` (canonical only) | Built as `f"{session_id}:{task_id}:{uuid8}"` (`turn_context.py:592-594`), but treat it as opaque (HK:398) |
| `task_id` | Per turn unless the caller pins it (`uuid4()` at `turn_context.py:589`) | Turn and tool hooks | Isolates terminal and VM state |
| `session_id` | Conversation | Most hooks. Approval hooks only when the contextvar is bound | Rotates on `/new` and on compaction-with-rotation (`post_compaction.old_session_id`) |
| `session_key` | Gateway routing key | Approval, `on_human_input_*`, `agent_loop_stopped`, `pre_command` | Not equal to `session_id` |
| `request_id` | One human prompt | `on_human_input_*`; transport approvals | `uuid4().hex` (`human_input_hooks.py:64`) |
| `api_request_id` | Provider attempt | API and tool hooks | Opaque |

No field links the denied C1 to the corrected C2. Pair them as: same `session_id`, a deny on C1, then the next **approved** call with the same `tool_name`/`pattern_key` within the next k turns. The intervening `pre_llm_call.user_message` is the candidate correction.

---

## 3. Next user message, tool calls, and final text

- **Next user message**: `pre_llm_call.user_message` (`str|list`). `pre_llm_call.conversation_history[-1]` is the same user message, and the earlier elements are the full prior transcript, including the previous turn's final assistant message. Correction detection can run inside `pre_llm_call` with no Hypnos-side buffering, but **defer the classification off-thread** to stay under 5 ms.
- **Per-turn assistant tool calls and final text**: `post_llm_call.assistant_response` (the final text) and `post_llm_call.conversation_history` (the full list after the turn).
  - Assistant messages are `{"role":"assistant","content","reasoning",["reasoning_content"],["tool_calls"]}` (`agent/chat_completion_helpers.py:1638-1667`).
  - Tool results are `{"role":"tool","name","tool_name","content","tool_call_id",[...]}` (`agent/tool_dispatch_helpers.py:464-470`). High-risk tool content is wrapped in untrusted-data delimiters.
  - Slice from the last `role=="user"` message to get this turn.
- **Failed or interrupted turns**: there is no `post_llm_call`. Use `post_tool_call` events keyed by `turn_id`, plus `on_session_end(completed/interrupted/failed/turn_exit_reason)`.
- **Per API call**: `post_api_request.assistant_message` (raw) or `.response` (sanitized).

---

## 4. Session lifecycle and identifiers

- **Per turn**: `pre_llm_call` (open) → tools → `post_llm_call` (success only) → `on_session_end` (always, canonical).
- **Session open**: `on_session_start` on the first turn only (`is_first_turn=True` in `pre_llm_call`).
- **Session close**: `on_session_finalize` on CLI `/new`, quit, or one-shot end; TUI close; gateway reset or shutdown. It does **not** fire for cron, idle eviction, or a crash. `on_session_reset` fires at the boundary with the **new** id.
- **Deletion**: `on_session_delete(session_id, reason, profile, [deleted_ids], [sessions_dir])` (HK:1093-1117). Use it to purge Hypnos data for deleted sessions.
- **Session id stability**: `session_id` is stable across turns but **rotates** on `/new`, on `/reset` (gateway), and on compaction with rotation. Follow `post_compaction(session_id, old_session_id)` and `on_session_reset(old_session_id, new_session_id)` (gateway only) to chain ids.
- **Turn index**: none is exposed. Agent-internal `_user_turn_count` (`turn_context.py:1185`) is not passed to hooks. Count `pre_llm_call` per `session_id` yourself.
- **Forks and subagents**:
  - Background-review forks set `_persist_disabled`, so they fire **no** `pre_llm_call`, `post_llm_call`, or `on_session_*`.
  - Their tool hooks are not gated, so they **can** fire `pre_tool_call`/`post_tool_call` under the parent's `session_id` with a `turn_id` never opened by `pre_llm_call`. Drop those.
  - Subagents fire `pre_llm_call` with a non-empty `parent_session_id`.
  - Cron runs are detectable via `ctx.current_cron_execution()` (PI:1936-1958; `plugins.py:411`). Cron has no human, so it produces no corrections and no prompted approvals.

---

## 5. Observer hooks (`observer-hooks.md`)

- **What they are**: not a separate mechanism. "Observer hooks" are the same `ctx.register_hook` plugin hooks, documented as a read-only telemetry contract (OH:8-24, 28-36). The schema marker is `telemetry_schema_version="hermes.observer.v1"`, injected into every payload (OH:48-52; `plugins_dispatch.py:292`). HK:447 notes that the marker "does not mean all hook payloads share one semantic schema".
- **Families**: session lifecycle, turn-scoped LLM (`pre/post_llm_call`), request-scoped API (`pre/post_api_request`, `api_request_error`, `pre/post_auxiliary_call`), tool lifecycle (`pre/post_tool_call`, `transform_tool_result`), human input, approval, and subagent (OH:90-251).
- **Correlation ids**: `session_id, task_id, turn_id, api_request_id, api_call_count, tool_call_id, parent/child_session_id, parent/child_subagent_id, parent_turn_id` (OH:70-88).
- **Payload safety**: prefer the sanitized `request`/`response`/`error` fields. `conversation_history`, `request_messages`, and `assistant_message` are raw legacy fields (OH:253-269).
- **Performance**: payload construction is gated by `has_hook`. Register only the hooks you consume, do not deep-copy, and offload I/O (OH:271-283).
- **Fit for Hypnos**: the same API, so no "better" alternative exists. Two other surfaces could serve passive capture:
  - **Shell hooks**: a subprocess per event, which costs far more than 5 ms.
  - **Outbound webhooks**, `hooks.outbound` (HK:2174-2259): fire-and-forget on a background thread with zero hot-path cost, but best-effort and notify-only. A queue overflow drops events, and only one retry is made. Payloads carry `extra` with all kwargs. This is a viable zero-latency fallback that ships events to a local HTTP sink.

---

## 6. Rules

- **`**kwargs`**: HK:394 and PI:1554 say "Always accept `**kwargs`". At runtime, narrow signatures still work because the dispatcher filters to the declared names (`plugins_dispatch.py:250-263`). `hermes plugins doctor [path] [--ci]` reports an **error** for any registered hook callback without `VAR_KEYWORD`: `"hook callback 'X' for 'Y' must accept **kwargs for forward compatibility"` (`hermes_cli/plugin_dev.py:472-480, 332-337`).
- **Fail-open vs fail-closed**:
  - Observers fail open: a raise or timeout is logged and skipped (HK:395-396; OH:54-55).
  - **`pre_tool_call` fails closed** on a raise, a timeout, or the 60 s suppression window (HK:396, 592; `plugins_dispatch.py:303-323`).
  - `pre_llm_call` fails open (HK:706).
  - Unbounded hooks (approval, finalize, reset) have **no timeout**: a slow callback stalls the approval or the teardown. The gateway caps finalize and reset at 10 s via its executor (`gateway/run.py:3739`).
- **Slow bounded hooks**: the agent thread waits up to `hook_callback_timeout` (30 s) **per callback per event**.
- **Manifest**:
  - Hooks are **not** required to be declared for loading. The registered set is whatever `register(ctx)` adds (PI:368-376).
  - `provides_hooks` (alias `hooks:`, `hermes_cli/plugins_manifest.py:575-577`) drives checks only:
    - `doctor` warns on declared/registered drift and errors on unknown names (`plugin_dev.py:457-491`).
    - `hermes plugins validate` **fails** "declared hooks" when undeclared hooks are registered (`hermes_cli/plugin_validate.py:436-458`), which blocks catalog admission.
  - Recommendation: declare exactly the hooks you register.
- **Enablement**: user plugins load only when listed in `plugins.enabled` (HK:1911). Hooks of a late-loaded plugin activate immediately; tools and prompt sections defer to the next session (PI:2104-2128).
- **Storage**:
  - Use `plugins.plugin_storage.plugin_data_dir("hypnos")` or `plugin_db("hypnos")`, which resolve to `<HERMES_HOME>/plugin-data/hypnos/` and a WAL SQLite file (PI:1153-1169; `plugins/plugin_storage.py:27-46`).
  - `HERMES_HOME` is a **contextvar override** (`hermes_constants.py:194-199`). Bounded hook threads copy context, but a Hypnos background writer thread will not. Resolve the path inside the hook, or start the writer with `contextvars.copy_context()`; otherwise a multi-profile gateway writes to the wrong profile.
  - `plugin_data_dir` runs `mkdir` on every call. Cache the result per profile.

---

## 7. Tool result payloads (terminal)

`post_tool_call.result` for `tool_name == "terminal"` is a **JSON string** (`tools/terminal_tool_result.py:259-278`):

```
{"output": str,        # stdout+stderr COMBINED (no separate stderr), sudo-annotated, transform_terminal_output-applied,
                       # head/tail-truncated, ANSI-stripped, secret-redacted (:227-243)
 "exit_code": int,     # process return code; -1 for guard/deny/error envelopes (terminal_tool.py:1064-1070)
 "error": str | null,
 # optional, only when non-null, in this order:
 "cwd", "environment_recreated", "output_total_chars", "full_output_path", "truncation_note",
 "verification_evidence" {status,kind,scope,canonical_command}, "approval", "exit_code_meaning",
 "hint", "sudo_auth_failed", "sudo_cache_cleared"}
```

- **Error, denial, and pending envelopes**: `{"output":"","exit_code":-1,"error":<msg>,"status":"blocked"|"pending_approval"|...,["user_summary"]}`.
- **Background commands** (`args.background=true`): the post result has no final exit code. Final status arrives through later `process_manage` calls and notifications, which pass through `transform_terminal_output` with `returncode=None` while the process runs (HK:1715).
- **Raw, pre-redaction output and the true return code** for executed foreground commands: `transform_terminal_output(command, output, returncode, task_id, env_type, tool_call_id)`. It fires before truncation and redaction, which is a privacy risk for training data, and it never fires for denied or blocked commands.
- **Terminal args schema keys**: `command, background, timeout, workdir, pty, notify, heartbeat, persist_on_release` (`tools/terminal_tool.py` schema). `force` is an internal parameter and is not in the schema.
- **Type of `result`**: HK:642 says it is always a JSON string, but multimodal tool results can be dicts (`agent/display.py:1034, 1070`). Handle non-`str`.

---

## 8. Conflicts with Hypnos assumptions

| Assumption | Reality in v0.23.0 |
|---|---|
| `post_llm_call` fires after each LLM call | Once per **turn**, success only (`turn_finalizer.py:756`). Per call is `post_api_request` |
| `on_session_end` marks the session end | Fires **every turn**. The real end is `on_session_finalize`, which is not guaranteed (cron, idle, crash). Flush per turn or per event |
| Hooks are fail-open | **Not `pre_tool_call`**: a raise or timeout blocks the user's tool call, and after one timeout every call is blocked for 60 s. Approval, finalize, and reset hooks are unbounded |
| The approval response includes a reason or correction | No reason in hook kwargs. The gateway-only `/deny <reason>` appears in the tool result `error` text. CLI, TUI, and buttons have no reason channel |
| Deny then retry happens within one exchange | The model is instructed to stop and wait. The correction is normally the next turn's `pre_llm_call.user_message`, with no linking id |
| Denials show as `status="blocked"` | The hook kwarg is `status="error"`. `"status":"blocked"` lives inside the `result` JSON. `status="blocked"` is only for plugin and guardrail blocks |
| `post_tool_call` exposes exit code, stdout, and stderr | Exit code yes (`exit_code` in the result JSON). stdout and stderr are **combined** into `output`, redacted and truncated |
| The command string is the join key | Approval `command` may be redacted (gateway display, smart, transport) or normalised. Join on `tool_call_id` |
| Hermes provides a stable session id and turn index | `session_id` rotates on compaction and `/new`. No turn index exists |
| Plugins run in the CLI process | The classic CLI uses `GatewayAgent`, so turns and hooks run in the **gateway** process (`hermes_cli/gateway_agent.py:1-8`; `cli_agent_setup_mixin.py:650-657`). It is multi-session, multi-profile, and multi-threaded |
| Hooks cost nearly nothing | Each bounded hook spawns a thread per callback per event, plus a config stat (`plugins.py:1053-1054`). That is framework overhead before Hypnos code runs. Keep the Hypnos body to an O(1) enqueue |
| Every approval fires an approval hook | Session, always, allowlist, and yolo grants approve silently. Only the result `approval` note reveals some of them |

---

## 9. Recommended hook set for Hypnos

The `provides_hooks` list (all `def cb(**kwargs)`, sync, `try/except Exception: return None` around the whole body, O(1) enqueue to a `queue.SimpleQueue`, and a writer thread started with `copy_context()`):

```
pre_llm_call            # next user message → correction candidate; open turn (session_id, turn_id, is_first_turn, parent_session_id)
post_tool_call          # tool_name/args/result/status/error_*/duration_ms keyed by tool_call_id; parse terminal exit_code/BLOCKED/approval
post_approval_response  # choice + tool_call_id/turn_id/surface/pattern_key(s)/command (redaction-dependent)
post_llm_call           # assistant_response + slice of conversation_history for this turn
on_session_end          # per-turn close + completed/failed/interrupted/turn_exit_reason → flush
on_session_finalize     # session close (best-effort), return None
post_compaction         # session_id rotation chain (optional)
on_session_delete       # purge (optional)
```

- **Skip `pre_tool_call`**. It is fail-closed and adds nothing that `post_tool_call` plus approval hooks lack.
- **Skip `pre_approval_request`** unless prompt latency is needed. If it is, `on_human_input_*` gives the wait window.
- **Optional**: `transform_terminal_output` (return `None`) for the unredacted return code and output of executed commands. Gate it on a privacy setting.
