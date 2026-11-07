# Running Hypnos on a Runpod GPU

CPU nights work but are slow. For real nights and the demo recording, run Hermes and Hypnos on a GPU pod.
Your captured data then lives on the pod, so use the synthetic demo profile, or a pod you trust.

## 1. Pod

- Template: any Runpod PyTorch template with CUDA 12 (Ubuntu 22.04 or 24.04).
- GPU: one RTX 4090, L4 or A10G is plenty for Qwen3-0.6B or Qwen3-1.7B LoRA.
- Disk: 40 GB container disk.
- Expose nothing publicly. You will reach Hermes through an SSH tunnel.

## 2. Hermes

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh \
  | bash -s -- --channel stable --non-interactive --skip-browser --skip-computer-use
exec $SHELL -l
hermes --version   # v0.23.0 or newer
```

Configure your model with `hermes model` (or set a provider key the usual way). Hypnos never reads it.

## 3. Hypnos

```bash
hermes profile create hypnos-demo
hermes -p hypnos-demo plugins install https://github.com/0xNoramiya/hypnos
hermes -p hypnos-demo plugins enable hypnos
hermes -p hypnos-demo hypnos doctor
```

`doctor` prints the trainer command. On the pod it is the CUDA extra:

```bash
uv venv --python 3.12 ~/.hermes/profiles/hypnos-demo/plugin-data/hypnos/trainer
uv pip install --python ~/.hermes/profiles/hypnos-demo/plugin-data/hypnos/trainer/bin/python \
  -r ~/.hermes/profiles/hypnos-demo/plugins/hypnos/pyproject.toml --extra cuda
```

Use the exact paths `doctor` prints; they depend on where the profile lives. Docker is usually not
available inside a Runpod container, so dreams run in lucid mode there. The report says so.

## 4. A real demo run

```bash
hermes -p hypnos-demo hypnos demo timelapse --days 5
```

Without `--tiny` this trains Qwen3-0.6B. For the larger brain, set it first:

```bash
hermes -p hypnos-demo config set plugins.entries.hypnos.settings.base_model Qwen/Qwen3-1.7B
```

## 5. Desktop in your browser

```bash
hermes -p hypnos-demo config set webapp.enabled true
hermes -p hypnos-demo gateway restart
hermes -p hypnos-demo webapp --no-open      # prints a one-use sign-in link for 127.0.0.1:9120
```

On your laptop, tunnel the port and open the link:

```bash
ssh -N -L 9120:127.0.0.1:9120 root@<pod-ip> -p <ssh-port> -i ~/.ssh/id_ed25519
```

Turn on Hypnos under Capabilities, then Plugins. The moon, the hypnogram, the `/hypnos` report page and the
reply badges appear there. Record the video from this browser window, following `docs/demo-script.md`.
