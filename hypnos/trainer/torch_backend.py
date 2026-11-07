"""LoRA SFT, DPO and generation with transformers + peft on CUDA or CPU.

TRL is not required: the loops below are small, and owning them keeps one code path for CPU and GPU.
DPO uses reference log-probs computed once, from the starting adapter, before any update, so only one
model is held in memory.
"""

from __future__ import annotations

import math
import os
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import torch
import torch.nn.functional as F
from peft import LoraConfig, PeftModel, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer

from .render import render_completion, render_prompt

Emit = Callable[[dict], None]


@dataclass
class Encoded:
    id: str
    input_ids: list[int]
    labels: list[int]


def pick_device() -> tuple[str, torch.dtype]:
    if torch.cuda.is_available():
        return "cuda", torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps", torch.float32
    return "cpu", torch.float32


class TorchBackend:
    def __init__(self, base_model: str, adapter: str | None = None, *, trainable: bool = False,
                 lora: dict | None = None, seed: int = 0, max_len: int = 1024, checkpointing: bool | None = None):
        torch.manual_seed(seed)
        random.seed(seed)
        self.device, self.dtype = pick_device()
        if self.device == "cpu":
            torch.set_num_threads(max(1, os.cpu_count() or 1))
        self.max_len = max_len
        self.tokenizer = AutoTokenizer.from_pretrained(base_model)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        base = AutoModelForCausalLM.from_pretrained(base_model, dtype=self.dtype)
        base.to(self.device)
        if adapter and not trainable:
            # Inference only: fold the LoRA into the weights. Generation through the PEFT wrapper is about
            # 10x slower on CPU.
            self.model = PeftModel.from_pretrained(base, adapter).merge_and_unload()
        elif adapter:
            self.model = PeftModel.from_pretrained(base, adapter, is_trainable=trainable)
        elif trainable:
            cfg = lora or {}
            config = LoraConfig(r=cfg.get("r", 16), lora_alpha=cfg.get("alpha", 32), lora_dropout=cfg.get("dropout", 0.05),
                                target_modules=cfg.get("target_modules", "all-linear"), task_type="CAUSAL_LM")
            self.model = get_peft_model(base, config)
        else:
            self.model = base
        if trainable and hasattr(self.model, "enable_input_require_grads"):
            self.model.enable_input_require_grads()
        if trainable and (checkpointing if checkpointing is not None else self.device == "cpu"):
            # Keep only layer inputs for backward and recompute the rest: a CPU night's peak memory drops by
            # gigabytes for about 25% more time.
            try:
                self.model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
            except (TypeError, ValueError, AttributeError):
                pass

    # --- encoding ------------------------------------------------------------------------------------

    def encode(self, example: dict, target: dict | None = None) -> Encoded | None:
        messages, tools = example["messages"], example.get("tools") or []
        target = target if target is not None else example["target"]
        prompt = render_prompt(self.tokenizer, messages, tools)
        completion = render_completion(self.tokenizer, messages, tools, target)
        p_ids = self.tokenizer(prompt, add_special_tokens=False)["input_ids"]
        c_ids = self.tokenizer(completion, add_special_tokens=False)["input_ids"]
        if not c_ids:
            return None
        overflow = len(p_ids) + len(c_ids) - self.max_len
        if overflow > 0:
            if overflow >= len(p_ids) - 16:
                return None
            # Keep the system prompt head and the most recent context.
            head = p_ids[:64]
            p_ids = head + p_ids[64 + overflow:]
        return Encoded(example.get("id", ""), p_ids + c_ids, [-100] * len(p_ids) + c_ids)

    def _batch(self, items: list[Encoded]) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        width = max(len(e.input_ids) for e in items)
        pad = self.tokenizer.pad_token_id
        ids = torch.full((len(items), width), pad, dtype=torch.long)
        labels = torch.full((len(items), width), -100, dtype=torch.long)
        mask = torch.zeros((len(items), width), dtype=torch.long)
        for i, e in enumerate(items):
            n = len(e.input_ids)
            ids[i, :n] = torch.tensor(e.input_ids)
            labels[i, :n] = torch.tensor(e.labels)
            mask[i, :n] = 1
        return ids.to(self.device), labels.to(self.device), mask.to(self.device)

    def _decoder_and_head(self):
        base = self.model.get_base_model() if hasattr(self.model, "get_base_model") else self.model
        decoder = getattr(base, "model", None)
        head = getattr(base, "lm_head", None)
        return (decoder, head) if decoder is not None and head is not None else (None, None)

    def completion_logprobs(self, items: list[Encoded]) -> tuple[torch.Tensor, torch.Tensor, int]:
        """Log-probs of completion tokens only, with their sequence index.

        The LM head runs only on completion positions. Projecting every position onto the full vocabulary
        (batch x length x vocab in fp32) is what made CPU nights need gigabytes.
        """
        ids, labels, mask = self._batch(items)
        targets = labels[:, 1:]
        valid = targets != -100
        decoder, head = self._decoder_and_head()
        if decoder is not None:
            hidden = decoder(input_ids=ids, attention_mask=mask).last_hidden_state[:, :-1, :]
            logits = head(hidden[valid]).float()
        else:
            logits = self.model(input_ids=ids, attention_mask=mask).logits[:, :-1, :][valid].float()
        lp = torch.log_softmax(logits, dim=-1).gather(-1, targets[valid].unsqueeze(-1)).squeeze(-1)
        return lp, valid.nonzero(as_tuple=True)[0], ids.size(0)

    def sequence_logprobs(self, items: list[Encoded]) -> torch.Tensor:
        """Sum of completion-token log-probs per sequence."""
        lp, seq, n = self.completion_logprobs(items)
        return torch.zeros(n, device=lp.device, dtype=lp.dtype).index_add(0, seq, lp)

    # --- training ------------------------------------------------------------------------------------

    def _optimizer(self, lr: float):
        params = [p for p in self.model.parameters() if p.requires_grad]
        return torch.optim.AdamW(params, lr=lr, weight_decay=0.0)

    def train_phases(self, phases: list[dict], sft: list[dict], dpo: list[dict], *, emit: Emit,
                     lr_sft: float = 2e-4, lr_dpo: float = 5e-5, beta: float = 0.1, batch_size: int = 1,
                     grad_accum: int = 4, deadline: float | None = None) -> dict:
        """Run interleaved N3 (SFT) and REM (DPO) phases. Returns training stats."""
        self.model.train()
        sft_enc = [e for e in (self.encode(x) for x in sft) if e is not None]
        dpo_enc = []
        for x in dpo:
            c, r = self.encode(x, x["chosen"]), self.encode(x, x["rejected"])
            if c and r:
                dpo_enc.append((x.get("id", ""), c, r))
        ref = {}
        if dpo_enc:
            self.model.eval()
            with torch.no_grad():
                for pid, c, r in dpo_enc:
                    ref[pid] = (self.sequence_logprobs([c]).item(), self.sequence_logprobs([r]).item())
            self.model.train()
        opt_sft, opt_dpo = self._optimizer(lr_sft), self._optimizer(lr_dpo)
        stats = {"sft_steps": 0, "dpo_steps": 0, "sft_loss": [], "dpo_loss": [], "dpo_acc": [], "truncated": False,
                 "sft_examples": len(sft_enc), "dpo_pairs": len(dpo_enc)}
        rng = random.Random(0)
        for phase in phases:
            kind, steps = phase["kind"], int(phase["steps"])
            if steps <= 0 or (kind == "sft" and not sft_enc) or (kind == "dpo" and not dpo_enc):
                emit({"event": "phase_skipped", **phase})
                continue
            emit({"event": "phase_start", **phase})
            pool = list(sft_enc if kind == "sft" else dpo_enc)
            rng.shuffle(pool)
            cursor = 0
            losses = []
            for step in range(steps):
                if deadline and time.time() > deadline:
                    stats["truncated"] = True
                    break
                opt = opt_sft if kind == "sft" else opt_dpo
                opt.zero_grad(set_to_none=True)
                total = 0.0
                for _ in range(grad_accum):
                    chunk = []
                    for _ in range(batch_size):
                        if cursor >= len(pool):
                            rng.shuffle(pool)
                            cursor = 0
                        chunk.append(pool[cursor])
                        cursor += 1
                    if kind == "sft":
                        lp, _, _ = self.completion_logprobs(chunk)
                        loss = -lp.mean()
                    else:
                        loss, acc = self._dpo_loss(chunk, ref, beta)
                        stats["dpo_acc"].append(acc)
                    (loss / grad_accum).backward()
                    total += loss.item() / grad_accum
                torch.nn.utils.clip_grad_norm_([p for p in self.model.parameters() if p.requires_grad], 1.0)
                opt.step()
                losses.append(total)
                stats[f"{kind}_steps"] += 1
                stats[f"{kind}_loss"].append(round(total, 4))
                if step % max(1, steps // 10) == 0 or step == steps - 1:
                    emit({"event": "progress", "stage": phase["stage"], "cycle": phase.get("cycle"), "kind": kind,
                          "step": step + 1, "steps": steps, "loss": round(total, 4)})
            emit({"event": "phase_end", **phase, "mean_loss": round(sum(losses) / len(losses), 4) if losses else None})
            if stats["truncated"]:
                break
        self.model.eval()
        return stats

    def _dpo_loss(self, chunk: list, ref: dict, beta: float) -> tuple[torch.Tensor, float]:
        chosen = self.sequence_logprobs([c for _, c, _ in chunk])
        rejected = self.sequence_logprobs([r for _, _, r in chunk])
        ref_c = torch.tensor([ref[pid][0] for pid, _, _ in chunk], device=chosen.device)
        ref_r = torch.tensor([ref[pid][1] for pid, _, _ in chunk], device=chosen.device)
        margin = beta * ((chosen - ref_c) - (rejected - ref_r))
        loss = -F.logsigmoid(margin).mean()
        return loss, float((margin > 0).float().mean().item())

    # --- inference -----------------------------------------------------------------------------------

    @torch.no_grad()
    def generate(self, prompts: Iterable[dict], *, max_new_tokens: int = 96, batch_size: int = 8,
                 adapter_enabled: bool = True) -> list[dict]:
        """Greedy generation. Returns text and mean token log-prob for each prompt."""
        self.model.eval()
        tok = self.tokenizer
        tok.padding_side = "left"
        items = list(prompts)
        out: list[dict] = []
        ctx = self.model.disable_adapter() if (not adapter_enabled and hasattr(self.model, "disable_adapter")) else None
        merged = False
        if ctx is not None:
            ctx.__enter__()
        elif hasattr(self.model, "merge_adapter"):
            self.model.merge_adapter()  # temporary, undone below so training continues on the LoRA weights
            merged = True
        try:
            for start in range(0, len(items), batch_size):
                chunk = items[start:start + batch_size]
                texts = [render_prompt(tok, x["messages"], x.get("tools") or []) for x in chunk]
                enc = tok(texts, return_tensors="pt", padding=True, add_special_tokens=False).to(self.device)
                kwargs = {"max_new_tokens": max_new_tokens, "do_sample": False, "output_scores": True,
                          "use_cache": True, "return_dict_in_generate": True, "pad_token_id": tok.pad_token_id}
                try:
                    # A tool call is complete at </tool_call>; stopping there saves most of the budget.
                    gen = self.model.generate(**enc, **kwargs, stop_strings=["</tool_call>"], tokenizer=tok)
                except (TypeError, ValueError):
                    gen = self.model.generate(**enc, **kwargs)
                seqs = gen.sequences[:, enc["input_ids"].shape[1]:]
                scores = torch.stack(gen.scores, dim=1).float().log_softmax(-1)
                for i, x in enumerate(chunk):
                    toks = seqs[i]
                    stop = len(toks)
                    for j, t in enumerate(toks.tolist()):
                        if t in (tok.eos_token_id, tok.pad_token_id) or (tok.convert_ids_to_tokens(t) in ("<|im_end|>",)):
                            stop = j
                            break
                    lp = scores[i, :stop].gather(-1, toks[:stop].unsqueeze(-1)).squeeze(-1) if stop else torch.tensor([])
                    mean_lp = float(lp.mean().item()) if stop else -99.0
                    out.append({"id": x.get("id", ""), "text": tok.decode(toks[:stop], skip_special_tokens=False),
                                "mean_logprob": round(mean_lp, 4) if math.isfinite(mean_lp) else -99.0,
                                "tokens": stop})
        finally:
            if ctx is not None:
                ctx.__exit__(None, None, None)
            if merged:
                self.model.unmerge_adapter()
            tok.padding_side = "right"
        return out

    def save(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)
        self.model.save_pretrained(path)

    def prompt_tokens(self, example: dict) -> int:
        return len(self.tokenizer(render_prompt(self.tokenizer, example["messages"], example.get("tools") or []),
                                  add_special_tokens=False)["input_ids"])
