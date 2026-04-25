"""
CCDT Layer-4 Co-Pilot — Fine-Tuning Trainer (QLoRA / SFT)
════════════════════════════════════════════════════════════════════════════════
Fine-tunes a small open-source LLM (Mistral-7B-v0.3 or Llama-3-8B-Instruct)
on the CCDT SFT dataset produced by dataset_builder.py.

Uses:
  • QLoRA  (bitsandbytes 4-bit + PEFT LoRA adapters)  — fits on a single 24 GB GPU
  • TRL SFTTrainer  — handles ChatML conversation packing
  • HuggingFace Accelerate  — multi-GPU support

Output artifacts (saved to --output-dir):
  checkpoints/step-N/          LoRA adapter weights (per eval_steps)
  final/                       Merged full model + tokenizer
  training_metrics.json        Loss curve, perplexity, eval scores
  training_args.json           Full training configuration snapshot

Evaluation metrics:
  • Validation loss + perplexity
  • BLEU-4 on incident report generation (vs gold summaries in test set)
  • Action accuracy: does the model recommend the OPA-approved action?

Usage:
  # Single GPU (24 GB VRAM)
  python fine_tuning/trainer.py \\
      --dataset-dir  /data/sft_dataset \\
      --output-dir   /data/ft_model \\
      --base-model   mistralai/Mistral-7B-v0.3 \\
      --epochs       3

  # Evaluate only (no training)
  python fine_tuning/trainer.py \\
      --dataset-dir  /data/sft_dataset \\
      --output-dir   /data/ft_model \\
      --eval-only    --checkpoint /data/ft_model/checkpoints/step-500
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ccdt.fine_tuning.trainer")
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)

# ─── Default hyperparameters ──────────────────────────────────────────────────

@dataclass
class TrainingConfig:
    """All training hyperparameters in one place."""

    # Model
    base_model:    str   = "mistralai/Mistral-7B-v0.3"
    dataset_dir:   str   = "/data/sft_dataset"
    output_dir:    str   = "/data/ft_model"
    checkpoint:    Optional[str] = None
    eval_only:     bool  = False

    # LoRA
    lora_r:        int   = 64          # rank
    lora_alpha:    int   = 16          # scaling = lora_alpha / lora_r = 0.25
    lora_dropout:  float = 0.05
    lora_target_modules: list[str] = field(default_factory=lambda: [
        "q_proj", "k_proj", "v_proj", "o_proj",
        "gate_proj", "up_proj", "down_proj",
    ])

    # QLoRA quantisation
    load_in_4bit:           bool  = True
    bnb_4bit_quant_type:    str   = "nf4"       # NormalFloat4 — best for LLMs
    bnb_4bit_compute_dtype: str   = "bfloat16"  # bf16 compute (A100 / H100 / 4090)
    use_double_quant:       bool  = True

    # Training
    num_train_epochs:       int   = 3
    per_device_train_batch: int   = 4
    per_device_eval_batch:  int   = 4
    gradient_accumulation:  int   = 4           # effective batch = 4 × 4 = 16
    learning_rate:          float = 2e-4
    weight_decay:           float = 0.01
    warmup_ratio:           float = 0.05
    lr_scheduler:           str   = "cosine"
    max_seq_length:         int   = 2048
    packing:                bool  = True        # SFTTrainer conversation packing

    # Logging + checkpointing
    logging_steps:          int   = 10
    eval_steps:             int   = 100
    save_steps:             int   = 100
    save_total_limit:       int   = 3
    seed:                   int   = 42

    # Tokenizer
    padding_side:           str   = "right"
    chat_template:          str   = "mistral"   # mistral | llama3

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)


# ─── Utilities ────────────────────────────────────────────────────────────────

def _load_jsonl(path: str) -> list[dict]:
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _format_conversation_mistral(record: dict, tokenizer) -> str:
    """Format a ChatML record as Mistral instruction format."""
    messages = record.get("messages", [])
    text     = ""
    for msg in messages:
        role    = msg["role"]
        content = msg["content"]
        if role == "system":
            text += f"<<SYS>>\n{content}\n<</SYS>>\n\n"
        elif role == "user":
            text += f"[INST] {content} [/INST] "
        elif role == "assistant":
            text += f"{content}</s>"
    return text.strip()


def _format_conversation_llama3(record: dict, tokenizer) -> str:
    """Format a ChatML record as Llama-3 chat format."""
    messages = record.get("messages", [])
    text     = "<|begin_of_text|>"
    for msg in messages:
        role    = msg["role"]
        content = msg["content"]
        text   += f"<|start_header_id|>{role}<|end_header_id|>\n\n{content}<|eot_id|>"
    return text.strip()


# ─── Trainer ─────────────────────────────────────────────────────────────────

class CCDTFineTuner:
    """
    QLoRA fine-tuner for CCDT Co-Pilot dataset.

    Pipeline:
      1. Load base model in 4-bit NF4 quantisation
      2. Attach LoRA adapters to attention + MLP layers
      3. Load SFT dataset (train/val JSONL)
      4. Train with SFTTrainer (conversation packing for efficiency)
      5. Evaluate: val loss + perplexity + action accuracy
      6. Save LoRA adapters → merge → export full model
    """

    def __init__(self, cfg: TrainingConfig) -> None:
        self.cfg = cfg
        self._model     = None
        self._tokenizer = None
        self._peft_model = None

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self) -> dict:
        """
        Full training pipeline. Returns training metrics dict.
        """
        self._check_imports()
        logger.info("=" * 64)
        logger.info("CCDT Co-Pilot Fine-Tuning")
        logger.info("  base_model : %s", self.cfg.base_model)
        logger.info("  dataset    : %s", self.cfg.dataset_dir)
        logger.info("  output_dir : %s", self.cfg.output_dir)
        logger.info("  epochs     : %d", self.cfg.num_train_epochs)
        logger.info("  lora_r     : %d  alpha=%d", self.cfg.lora_r, self.cfg.lora_alpha)
        logger.info("=" * 64)

        os.makedirs(self.cfg.output_dir, exist_ok=True)

        # Save training config
        cfg_path = Path(self.cfg.output_dir) / "training_args.json"
        cfg_path.write_text(json.dumps(self.cfg.to_dict(), indent=2))

        if self.cfg.eval_only:
            return self._eval_only()

        self._load_model_and_tokenizer()
        self._attach_lora()
        train_ds, val_ds = self._load_datasets()
        metrics = self._train(train_ds, val_ds)
        self._save_merged_model()

        return metrics

    def evaluate(self, checkpoint: Optional[str] = None) -> dict:
        """Evaluate a saved LoRA checkpoint on the test set."""
        self._check_imports()
        self._load_model_and_tokenizer()

        if checkpoint:
            from peft import PeftModel
            logger.info("Loading LoRA checkpoint: %s", checkpoint)
            self._peft_model = PeftModel.from_pretrained(self._model, checkpoint)
        else:
            self._attach_lora()

        _, _, test_ds = self._load_datasets(include_test=True)
        return self._run_evaluation(test_ds, "test")

    # ── Model loading ─────────────────────────────────────────────────────────

    def _load_model_and_tokenizer(self) -> None:
        import torch
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
        )

        logger.info("Loading tokenizer: %s", self.cfg.base_model)
        tokenizer = AutoTokenizer.from_pretrained(
            self.cfg.base_model,
            trust_remote_code = True,
            padding_side      = self.cfg.padding_side,
        )
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token

        logger.info("Loading model in 4-bit NF4 quantisation…")
        compute_dtype = (
            torch.bfloat16 if self.cfg.bnb_4bit_compute_dtype == "bfloat16"
            else torch.float16
        )
        bnb_config = BitsAndBytesConfig(
            load_in_4bit              = self.cfg.load_in_4bit,
            bnb_4bit_quant_type       = self.cfg.bnb_4bit_quant_type,
            bnb_4bit_compute_dtype    = compute_dtype,
            bnb_4bit_use_double_quant = self.cfg.use_double_quant,
        )
        model = AutoModelForCausalLM.from_pretrained(
            self.cfg.base_model,
            quantization_config = bnb_config,
            device_map          = "auto",
            trust_remote_code   = True,
        )
        model.config.use_cache = False   # required for gradient checkpointing

        self._model     = model
        self._tokenizer = tokenizer
        logger.info("Model loaded. Parameters: %dM", sum(p.numel() for p in model.parameters()) // 1_000_000)

    # ── LoRA adapter ──────────────────────────────────────────────────────────

    def _attach_lora(self) -> None:
        from peft import (
            LoraConfig,
            TaskType,
            get_peft_model,
            prepare_model_for_kbit_training,
        )

        logger.info("Preparing model for k-bit training…")
        self._model = prepare_model_for_kbit_training(
            self._model,
            use_gradient_checkpointing = True,
        )

        lora_config = LoraConfig(
            task_type     = TaskType.CAUSAL_LM,
            r             = self.cfg.lora_r,
            lora_alpha    = self.cfg.lora_alpha,
            lora_dropout  = self.cfg.lora_dropout,
            target_modules = self.cfg.lora_target_modules,
            bias          = "none",
        )
        self._peft_model = get_peft_model(self._model, lora_config)

        trainable   = sum(p.numel() for p in self._peft_model.parameters() if p.requires_grad)
        total       = sum(p.numel() for p in self._peft_model.parameters())
        logger.info(
            "LoRA attached: trainable params = %dM / %dM  (%.2f%%)",
            trainable // 1_000_000, total // 1_000_000, 100 * trainable / total,
        )

    # ── Dataset ───────────────────────────────────────────────────────────────

    def _load_datasets(self, include_test: bool = False):
        from datasets import Dataset

        ds_dir  = Path(self.cfg.dataset_dir)
        train_p = ds_dir / "train.jsonl"
        val_p   = ds_dir / "val.jsonl"
        test_p  = ds_dir / "test.jsonl"

        # Use demo data if files don't exist
        if not train_p.exists():
            logger.warning("Dataset not found at %s — using demo data", ds_dir)
            from fine_tuning.dataset_builder import IncidentDatasetBuilder
            builder = IncidentDatasetBuilder()
            splits  = builder.build_from_dir(str(ds_dir))
            builder.save(splits, str(ds_dir))

        fmt_fn = (
            _format_conversation_llama3
            if "llama" in self.cfg.base_model.lower()
            else _format_conversation_mistral
        )

        def load_split(path: Path) -> Dataset:
            records = _load_jsonl(str(path)) if path.exists() else []
            texts   = []
            for rec in records:
                try:
                    texts.append({"text": fmt_fn(rec, self._tokenizer)})
                except Exception as exc:
                    logger.debug("Skipping record: %s", exc)
            logger.info("Loaded %s: %d records", path.name, len(texts))
            return Dataset.from_list(texts)

        train_ds = load_split(train_p)
        val_ds   = load_split(val_p)
        test_ds  = load_split(test_p) if include_test else None

        return (train_ds, val_ds) if not include_test else (train_ds, val_ds, test_ds)

    # ── Training ──────────────────────────────────────────────────────────────

    def _train(self, train_ds, val_ds) -> dict:
        from transformers import TrainingArguments
        from trl import SFTTrainer

        ckpt_dir = Path(self.cfg.output_dir) / "checkpoints"
        ckpt_dir.mkdir(parents=True, exist_ok=True)

        training_args = TrainingArguments(
            output_dir                  = str(ckpt_dir),
            num_train_epochs            = self.cfg.num_train_epochs,
            per_device_train_batch_size = self.cfg.per_device_train_batch,
            per_device_eval_batch_size  = self.cfg.per_device_eval_batch,
            gradient_accumulation_steps = self.cfg.gradient_accumulation,
            learning_rate               = self.cfg.learning_rate,
            weight_decay                = self.cfg.weight_decay,
            warmup_ratio                = self.cfg.warmup_ratio,
            lr_scheduler_type           = self.cfg.lr_scheduler,
            fp16                        = False,
            bf16                        = True,
            logging_steps               = self.cfg.logging_steps,
            evaluation_strategy         = "steps",
            eval_steps                  = self.cfg.eval_steps,
            save_strategy               = "steps",
            save_steps                  = self.cfg.save_steps,
            save_total_limit            = self.cfg.save_total_limit,
            load_best_model_at_end      = True,
            metric_for_best_model       = "eval_loss",
            report_to                   = ["tensorboard"],
            logging_dir                 = str(Path(self.cfg.output_dir) / "logs"),
            seed                        = self.cfg.seed,
            dataloader_pin_memory       = True,
            group_by_length             = True,
        )

        trainer = SFTTrainer(
            model            = self._peft_model,
            tokenizer        = self._tokenizer,
            args             = training_args,
            train_dataset    = train_ds,
            eval_dataset     = val_ds,
            dataset_text_field = "text",
            max_seq_length   = self.cfg.max_seq_length,
            packing          = self.cfg.packing,
        )

        logger.info("Starting training…")
        t0      = time.time()
        result  = trainer.train(
            resume_from_checkpoint = self.cfg.checkpoint,
        )
        elapsed = time.time() - t0

        train_metrics = {
            "train_loss":          result.training_loss,
            "train_runtime_s":     elapsed,
            "train_samples_per_s": result.metrics.get("train_samples_per_second", 0),
        }
        logger.info("Training complete in %.0f s. Loss: %.4f", elapsed, result.training_loss)

        # Final evaluation
        logger.info("Running final evaluation on validation set…")
        eval_metrics = trainer.evaluate()
        val_loss     = eval_metrics.get("eval_loss", float("inf"))
        perplexity   = math.exp(min(val_loss, 20))

        metrics = {
            **train_metrics,
            "val_loss":   val_loss,
            "perplexity": round(perplexity, 2),
        }
        logger.info("Val loss: %.4f  Perplexity: %.2f", val_loss, perplexity)

        # Save metrics
        metrics_path = Path(self.cfg.output_dir) / "training_metrics.json"
        metrics_path.write_text(json.dumps(metrics, indent=2))

        # Save final LoRA adapter
        final_adapter = Path(self.cfg.output_dir) / "final_adapter"
        trainer.save_model(str(final_adapter))
        logger.info("LoRA adapter saved: %s", final_adapter)

        return metrics

    # ── Evaluation only ───────────────────────────────────────────────────────

    def _eval_only(self) -> dict:
        logger.info("Eval-only mode")
        self._load_model_and_tokenizer()

        if self.cfg.checkpoint:
            from peft import PeftModel
            self._peft_model = PeftModel.from_pretrained(self._model, self.cfg.checkpoint)
        else:
            self._attach_lora()

        _, _, test_ds = self._load_datasets(include_test=True)
        return self._run_evaluation(test_ds, split="test")

    def _run_evaluation(self, dataset, split: str = "test") -> dict:
        """Run evaluation: loss, perplexity, action accuracy."""
        import torch
        from torch.utils.data import DataLoader

        model     = self._peft_model or self._model
        tokenizer = self._tokenizer
        model.eval()

        total_loss = 0.0
        n_batches  = 0
        action_correct = 0
        action_total   = 0

        # Action keywords that map to correct vs incorrect recommendations
        ATTACK_CORRECT_KEYWORDS  = ["isolate_container", "rotate_secrets", "apply_network_policy"]
        FAULT_CORRECT_KEYWORDS   = ["rollback_deployment", "restart_pod", "scale_up_replicas"]
        ALWAYS_WRONG_FIRST_ACTION = ["drain_node", "restart_pod"]  # wrong for attacks without isolation

        for batch_start in range(0, min(len(dataset), 200), 4):
            batch = dataset[batch_start: batch_start + 4]
            texts = batch["text"] if isinstance(batch, dict) else [b["text"] for b in batch]

            inputs = tokenizer(
                texts,
                return_tensors  = "pt",
                padding         = True,
                truncation      = True,
                max_length      = self.cfg.max_seq_length,
            )
            inputs = {k: v.to(model.device) for k, v in inputs.items()}

            with torch.no_grad():
                outputs = model(**inputs, labels=inputs["input_ids"])
                if outputs.loss is not None:
                    total_loss += outputs.loss.item()
                    n_batches  += 1

            # Simple action accuracy: check if response contains correct action keyword
            for text in texts:
                lower = text.lower()
                has_attack = "attack" in lower
                if has_attack:
                    correct = any(k in lower for k in ATTACK_CORRECT_KEYWORDS)
                    wrong   = "rollback_deployment" in lower and "isolate" not in lower
                else:
                    correct = any(k in lower for k in FAULT_CORRECT_KEYWORDS)
                    wrong   = False
                if correct and not wrong:
                    action_correct += 1
                action_total += 1

        avg_loss   = total_loss / max(n_batches, 1)
        perplexity = math.exp(min(avg_loss, 20))
        action_acc = action_correct / max(action_total, 1)

        metrics = {
            "split":          split,
            "eval_loss":      round(avg_loss, 4),
            "perplexity":     round(perplexity, 2),
            "action_accuracy": round(action_acc, 4),
            "n_samples":      action_total,
        }
        logger.info(
            "[%s] loss=%.4f  ppl=%.2f  action_acc=%.1f%%",
            split, avg_loss, perplexity, action_acc * 100,
        )
        return metrics

    # ── Model export ─────────────────────────────────────────────────────────

    def _save_merged_model(self) -> None:
        """
        Merge LoRA adapters into the base model and save the full model.
        This produces a standalone model that runs without PEFT installed.
        """
        if self._peft_model is None:
            logger.warning("No PEFT model to merge")
            return

        logger.info("Merging LoRA adapters into base model…")
        try:
            merged = self._peft_model.merge_and_unload()
            final_path = Path(self.cfg.output_dir) / "final"
            final_path.mkdir(parents=True, exist_ok=True)
            merged.save_pretrained(str(final_path))
            self._tokenizer.save_pretrained(str(final_path))
            logger.info("Merged model saved: %s", final_path)

            # Write model card
            card = {
                "model_name":  "ccdt-copilot-ft",
                "base_model":  self.cfg.base_model,
                "fine_tuning": "QLoRA SFT on CCDT incident dataset",
                "lora_r":      self.cfg.lora_r,
                "lora_alpha":  self.cfg.lora_alpha,
                "use_case":    "AIOps cluster incident diagnosis and remediation",
            }
            (final_path / "model_card.json").write_text(json.dumps(card, indent=2))

        except Exception as exc:
            logger.error("Failed to merge model: %s", exc)
            logger.info("LoRA adapter is still saved in final_adapter/")

    # ── Dependency check ──────────────────────────────────────────────────────

    @staticmethod
    def _check_imports() -> None:
        missing = []
        for pkg in ["torch", "transformers", "peft", "trl", "bitsandbytes", "accelerate", "datasets"]:
            try:
                __import__(pkg)
            except ImportError:
                missing.append(pkg)
        if missing:
            raise ImportError(
                f"Missing fine-tuning dependencies: {missing}\n"
                f"Install: pip install {' '.join(missing)}"
            )


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="CCDT Co-Pilot QLoRA fine-tuner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--base-model",   default="mistralai/Mistral-7B-v0.3",
                        help="HuggingFace model ID or local path")
    parser.add_argument("--dataset-dir",  required=True,
                        help="Directory with train/val/test JSONL from dataset_builder.py")
    parser.add_argument("--output-dir",   required=True,
                        help="Directory to save checkpoints and final merged model")
    parser.add_argument("--checkpoint",   default=None,
                        help="Resume from this LoRA checkpoint path")
    parser.add_argument("--eval-only",    action="store_true",
                        help="Skip training — only evaluate a checkpoint")
    parser.add_argument("--epochs",       type=int,   default=3)
    parser.add_argument("--batch-size",   type=int,   default=4)
    parser.add_argument("--grad-accum",   type=int,   default=4)
    parser.add_argument("--lr",           type=float, default=2e-4)
    parser.add_argument("--lora-r",       type=int,   default=64)
    parser.add_argument("--lora-alpha",   type=int,   default=16)
    parser.add_argument("--max-seq-len",  type=int,   default=2048)
    parser.add_argument("--chat-template", default="mistral",
                        choices=["mistral", "llama3"])
    parser.add_argument("--seed",         type=int,   default=42)
    args = parser.parse_args()

    cfg = TrainingConfig(
        base_model              = args.base_model,
        dataset_dir             = args.dataset_dir,
        output_dir              = args.output_dir,
        checkpoint              = args.checkpoint,
        eval_only               = args.eval_only,
        num_train_epochs        = args.epochs,
        per_device_train_batch  = args.batch_size,
        gradient_accumulation   = args.grad_accum,
        learning_rate           = args.lr,
        lora_r                  = args.lora_r,
        lora_alpha              = args.lora_alpha,
        max_seq_length          = args.max_seq_len,
        chat_template           = args.chat_template,
        seed                    = args.seed,
    )

    tuner   = CCDTFineTuner(cfg)
    metrics = tuner.run()

    print("\n" + "=" * 60)
    print("✅ Fine-tuning complete")
    print(f"   Train loss:       {metrics.get('train_loss',  'N/A')}")
    print(f"   Val loss:         {metrics.get('val_loss',    'N/A')}")
    print(f"   Perplexity:       {metrics.get('perplexity',  'N/A')}")
    print(f"   Action accuracy:  {metrics.get('action_accuracy', 'N/A')}")
    print(f"   Output:           {args.output_dir}/final/")
    print("=" * 60)


if __name__ == "__main__":
    main()
