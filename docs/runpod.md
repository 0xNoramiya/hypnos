# Running Hypnos on a Runpod GPU

CPU nights work but are slow. For real nights and the demo recording, run Hermes and Hypnos on a GPU pod.
Your captured data then lives on the pod, so use the synthetic demo profile, or a pod you trust.

The README's GPU results came from this setup: one A40 on Secure Cloud and the official "Runpod Pytorch
2.8.0" template. The five-day timelapse took about 15 minutes, and the pod ran about 36 minutes in all,
setup included.

## 1. Pod

- Template: the official Runpod PyTorch template with CUDA 12.8
  (`runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404`). It already has torch with CUDA.
- GPU: one A40, RTX 4090, L4 or A10G is plenty for Qwen3-0.6B or Qwen3-1.7B LoRA.
- Disk: 40 GB or more of container disk.
- Ports: only `22/tcp` for SSH. You reach Hermes through an SSH tunnel, never a public port.

Some hosts have no public IP. Their pods offer only the Runpod SSH proxy (`ssh <pod>@ssh.runpod.io`): an
interactive shell without port forwarding or `scp`. Pick a pod with direct SSH if you want the Desktop
tunnel in step 5.

## 2. Hermes

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh \
  | bash -s -- --channel stable --non-interactive --skip-browser --skip-computer-use
exec $SHELL -l
hermes --version   # v0.23.0 or newer
```

Configure your model with `hermes model` (or set a provider key the usual way). Hypnos never reads it. The
demo timelapse does not need one: its frontier model is scripted.

## 3. Hypnos

```bash
hermes profile create hypnos-demo
hermes -p hypnos-demo plugins install https://github.com/0xNoramiya/hypnos
hermes -p hypnos-demo plugins enable hypnos
```

The template's Python already has torch, so the trainer only needs the training libraries, and Hypnos can
use that interpreter:

```bash
python3 -m pip install "transformers>=4.51,<6" "peft>=0.13,<1" "safetensors>=0.4,<1" "accelerate>=1.0,<2" \
  "huggingface-hub>=0.25,<2"
hermes -p hypnos-demo config set plugins.entries.hypnos.settings.trainer_python "$(command -v python3)"
hermes -p hypnos-demo hypnos doctor   # trainer interpreter: ok, torch ... True
```

On another image, use the venv command `doctor` prints (`--extra cuda --torch-backend auto`).

Docker is not available inside a Runpod container, so dreams run in lucid mode there: pattern checks only,
no execution. The report says so.

## 4. A real demo run

```bash
HF_HUB_DISABLE_XET=1 hermes -p hypnos-demo hypnos demo timelapse --days 5
```

Without `--tiny` this trains Qwen3-0.6B; each night took 1.3 to 2.4 minutes on the A40. For the larger
brain, set it first:

```bash
hermes -p hypnos-demo config set plugins.entries.hypnos.settings.base_model Qwen/Qwen3-1.7B
```

To keep the results after the pod is gone, copy the profile's `plugin-data/hypnos/` folder (the database,
`nights/` and the `brains/` adapters, about 40 MB per brain) into the same place in a local `hypnos-demo`
profile, then point the adapter paths at their new home:

```bash
python3 - <<'EOF'
import os, sqlite3
data = os.path.expanduser("~/.hermes/profiles/hypnos-demo/plugin-data/hypnos")
db = sqlite3.connect(f"{data}/data.db")
db.execute("UPDATE brains SET adapter_path = ? || substr(adapter_path, instr(adapter_path, '/brains/'))", (data,))
db.commit()
EOF
```

Then terminate the pod. A stopped pod keeps billing for its disk.

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
