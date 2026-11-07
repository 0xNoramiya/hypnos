# Disclosures

What Hypnos does that you would want to know before installing it. The same list is in the catalog pull
request.

## Data it stores

- Location: `~/.hermes/plugin-data/hypnos/` (per Hermes profile). It survives plugin updates and removal;
  delete the folder to remove everything.
- `data.db` (SQLite) holds scrubbed copies of your turns: the user message, the tool calls Hermes made and
  their (truncated) output, the final reply, approvals, signals, learning pairs, habits, probes, dreams,
  routing decisions, nights and reports.
- `brains/vN/` holds LoRA adapters. `nights/N/` holds the training and exam files for each night.
  `exports/` holds brains you exported.
- Before anything is written, a scrubber replaces API keys, tokens, private keys, credentials in URLs,
  emails, IP addresses and long high-entropy strings with placeholders. Each night re-checks everything
  captured since the last one.
- Turns on platforms listed in `exclude_platforms` (cron by default) and subagent turns are not captured.
  Deleting a Hermes session deletes its Hypnos data.

## Files it reads outside its own data

- `MEMORY.md` and `USER.md` in Hermes's memories folder, read only, to find preference lines that describe
  habits. Hypnos never writes these files.

## Files it writes outside its own data

- `~/.hermes/scripts/hypnos-sleep.py`, only when you run `hermes hypnos schedule`. Hermes's no-agent cron
  jobs must live in that folder. `hermes hypnos schedule --remove` deletes it.

## Network

- The dream teacher calls your own model through Hermes (`ctx.llm`, auxiliary task `hypnos_teacher`), at
  most `teacher_budget_tokens` per night (60,000 by default). Set `teacher: scripted` to never call a model.
  Hypnos never sees your credentials.
- The trainer downloads the base model you chose (default `Qwen/Qwen3-0.6B`) from Hugging Face once, through
  the standard Hugging Face cache.
- `docker pull` of the sandbox image only if you run it yourself. The image is the official
  `buildpack-deps:bookworm-scm`, pinned by digest.
- `/brain export --push-to-hub REPO` runs your own `hf upload` command, only with that flag.
- The brain server listens on `127.0.0.1` only. A `brain_endpoint` you configure is called with the brain's
  compact prompt.
- Telemetry: none.

## Processes it starts

- During a night, the training worker: a separate Python interpreter (the trainer environment you created)
  that runs LoRA training and generation. It gets a minimal environment with no Hermes or provider
  credentials.
- After a promoted night, the brain server: a long-running background process on `127.0.0.1:8765` that
  serves the current brain. Stop it with `hermes hypnos serve stop`, or turn off
  `auto_start_brain_server`. It never starts if you set `routing: off` or your own `brain_endpoint`.
- `/sleep now` and Desktop's "Sleep now" start the night as a background `hermes hypnos sleep` process.
- REM dreams run short Docker containers: no network, all capabilities dropped, read-only root filesystem,
  tmpfs work area, non-root user, CPU, memory, process and time limits, and no host folder mounted. The
  commands run there are the dream teacher's and the brain's proposals, never on your machine.

## Shell commands

Hypnos runs `docker` (sandbox), the trainer Python, `hermes` itself (background nights and Lethe), and
`hf upload` only on request. It never runs the commands it captures or proposes on your machine.

## Approvals

Hypnos never approves, denies or skips a Hermes approval. When the brain answers a turn, its tool call goes
through Hermes's approvals like any other.

## Python dependencies

The plugin itself uses only the standard library and Hermes. It declares `python_runtime: external`, so
Hermes installs nothing for it. The trainer extras in `pyproject.toml` (torch, transformers, peft,
accelerate, safetensors, huggingface-hub) have floors and upper bounds and go into the separate trainer
environment you create.
