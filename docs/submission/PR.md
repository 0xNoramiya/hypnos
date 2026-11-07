# Add Hypnos to the plugin catalog

Adds `plugin-catalog/hypnos.yaml`, pinned to `<SHA>` of https://github.com/0xNoramiya/hypnos (MIT). I am the
repository owner.

## What it does

Hermes learns in its sleep. Hypnos captures what the user approves, denies and corrects. At night it replays
the day, practises variations of its mistakes in a Docker sandbox, and trains a small LoRA brain (SFT and
DPO). In the morning the brain takes a held-out exam and is promoted only if it beats the brain it replaces.
Hermes then routes the task families the brain has mastered to it, with the habits it consolidated pruned
from its prompt. Only procedural habits go into the weights; facts stay in Hermes's text memory. `/lethe`
forgets a session or a habit exactly by retraining from a clean ancestor.

## Hermes surfaces used

- Hooks: `pre_llm_call` (capture, correction detection, habit context), `post_tool_call`,
  `post_approval_response`, `post_llm_call`, `on_session_end`, `on_session_finalize`, `post_compaction`,
  `on_session_delete`, `transform_llm_output` (Desktop reply badge, Desktop only, setting to turn off).
- Middleware: `llm_execution` for routing. The user's model stays the provider; brain answers are proposed
  tool calls that go through Hermes approvals like any other.
- Slash commands `/hypnos`, `/sleep`, `/hypnogram`, `/brain`, `/lethe`; CLI `hermes hypnos ...`.
- `ctx.llm` with an own auxiliary task `hypnos_teacher`; `ctx.get_config` / `set_config`; `config_schema`.
- `plugins.plugin_storage`, `agent.secret_scope` (no secrets declared today), `hermes_cli.plugin_events`,
  `hermes_cli.config.load_config_readonly`, `hermes_cli.setup.prompt_yes_no`. All imports are on the public
  plugin API list.
- Dashboard backend routes (`dashboard/plugin_api.py`) and a Desktop plugin (`desktop/plugin.js`, SDK only,
  no build step): status bar, routes, sidebar nav, palette, session row, transcript directive, settings page,
  pet lines.

## Disclosures

- Stores scrubbed copies of turns, signals, habits, dreams, brains and reports under
  `plugin-data/hypnos/`. Secrets, emails and IPs are scrubbed before writing. Telemetry: none.
- Reads `MEMORY.md` and `USER.md` read only. Writes `scripts/hypnos-sleep.py` only on `hermes hypnos schedule`.
- Network: the dream teacher uses the user's model through `ctx.llm` with a per-night token budget (or never,
  with `teacher: scripted`); the trainer downloads the chosen base model from Hugging Face; `hf upload` only
  with `--push-to-hub`.
- Background processes: a training worker during nights (separate interpreter, minimal environment without
  credentials), and a brain server on `127.0.0.1:8765` after a promoted night (opt-out setting).
- Shell and Docker: runs `docker run` for REM dreams with no network, capabilities dropped, read-only root,
  tmpfs, non-root, limits and no host mounts; the image is `buildpack-deps:bookworm-scm` pinned by digest and
  is only pulled by the user. Runs `hermes` itself for background nights.
- `python_runtime: external`: Hermes installs nothing. Trainer extras in `pyproject.toml` have floors and
  upper bounds.
- Never approves or bypasses approvals. Scheduled nights never prompt and fail cleanly under cron.

## Checks

- `hermes plugins validate . --install-deps`: passes (security scan safe, desktop surface clean, no core
  override, declared hooks and middleware match).
- `hermes plugins doctor . --ci`: passes.
- Tests: 217 passing, including live runs against Hermes v0.23.0 with a scripted model, a real CPU training
  night, and a brain-proposed destructive command blocked by Hermes approvals.

## Screenshots

<!-- docs/screenshots/*.png at the pinned commit -->

🤖 Generated with [Claude Code](https://claude.com/claude-code)
