"""
CCDT Layer-2 Cognitive Core — Training Module
──────────────────────────────────────────────
PyTorch Lightning module wrapping CausalGNN for distributed training.

Features:
  • AdamW optimiser with weight decay 1e-4
  • CosineAnnealingLR warmup schedule
  • Gradient clipping (max_norm=1.0)
  • Mixed-precision (bf16 on Ampere+, fp16 on older GPUs)
  • Per-epoch validation with F1 / AUC / accuracy metrics
  • Model checkpoint saving (best val_loss + every N epochs)
  • Weights & Biases / TensorBoard logging

Usage:
    python trainer.py [--epochs 100] [--batch-size 32] [--lr 3e-4] [--gpus 1]
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Any, Optional

import torch
import torch.nn.functional as F
from lightning import LightningDataModule, LightningModule, Trainer
from lightning.pytorch.callbacks import (
    EarlyStopping,
    LearningRateMonitor,
    ModelCheckpoint,
    RichProgressBar,
)
from lightning.pytorch.loggers import TensorBoardLogger
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts
from torch_geometric.loader import DataLoader
from torchmetrics import MetricCollection
from torchmetrics.classification import (
    MulticlassAccuracy,
    MulticlassAUROC,
    MulticlassF1Score,
)

from models.causal_gnn import CCDTCognitiveModel, CausalLoss
from training.dataset import CausalIncidentDataset

logger = logging.getLogger("ccdt.cognitive.trainer")

NUM_CLASSES = 3


# ─── Data Module ──────────────────────────────────────────────────────────────

class CCDTDataModule(LightningDataModule):
    """PyTorch Lightning DataModule wrapping CausalIncidentDataset."""

    def __init__(
        self,
        scenario_dir:  str   = "data",
        root:          str   = "/tmp/ccdt_dataset",
        num_samples:   int   = 4000,
        batch_size:    int   = 32,
        num_workers:   int   = 4,
        val_split:     float = 0.15,
        test_split:    float = 0.10,
        seed:          int   = 42,
    ) -> None:
        super().__init__()
        self.scenario_dir = scenario_dir
        self.root         = root
        self.num_samples  = num_samples
        self.batch_size   = batch_size
        self.num_workers  = num_workers
        self.val_split    = val_split
        self.test_split   = test_split
        self.seed         = seed

        self.train_ds: Optional[CausalIncidentDataset] = None
        self.val_ds:   Optional[CausalIncidentDataset] = None
        self.test_ds:  Optional[CausalIncidentDataset] = None

    def setup(self, stage: Optional[str] = None) -> None:
        common = dict(
            root=self.root,
            scenario_dir=self.scenario_dir,
            num_samples=self.num_samples,
            val_split=self.val_split,
            test_split=self.test_split,
            seed=self.seed,
        )
        if stage in ("fit", None):
            self.train_ds = CausalIncidentDataset(**common, split="train", augment=True)
            self.val_ds   = CausalIncidentDataset(**common, split="val",   augment=False)
            logger.info(
                "Dataset: train=%d val=%d",
                len(self.train_ds), len(self.val_ds),
            )
        if stage in ("test", None):
            self.test_ds = CausalIncidentDataset(**common, split="test", augment=False)

    def train_dataloader(self) -> DataLoader:
        return DataLoader(
            self.train_ds,
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=self.num_workers,
            pin_memory=True,
            drop_last=True,
        )

    def val_dataloader(self) -> DataLoader:
        return DataLoader(
            self.val_ds,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=self.num_workers,
            pin_memory=True,
        )

    def test_dataloader(self) -> DataLoader:
        return DataLoader(
            self.test_ds,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=self.num_workers,
        )

    def class_weights(self) -> torch.Tensor:
        return self.train_ds.class_weights() if self.train_ds else torch.ones(NUM_CLASSES)


# ─── Lightning Module ─────────────────────────────────────────────────────────

class CCDTLightningModule(LightningModule):
    """
    Lightning module for training/evaluating the CausalGNN.

    Logs per-batch and per-epoch metrics to TensorBoard.
    Supports mixed-precision and multi-GPU training via DDP.
    """

    def __init__(
        self,
        learning_rate:      float = 3e-4,
        weight_decay:       float = 1e-4,
        lambda_dag:         float = 0.1,
        lambda_contrastive: float = 0.05,
        warmup_steps:       int   = 200,
        t_max:              int   = 50,   # CosineAnnealing cycle length
        class_weights:      Optional[torch.Tensor] = None,
        # Model hyperparams
        hidden_dim:  int = 128,
        num_heads:   int = 8,
        num_layers:  int = 4,
        dropout:     float = 0.15,
    ) -> None:
        super().__init__()
        self.save_hyperparameters()

        # Model
        self.model = CCDTCognitiveModel(
            hidden_dim=hidden_dim,
            num_heads=num_heads,
            num_layers=num_layers,
            dropout=dropout,
        )

        # Loss
        cw = class_weights if class_weights is not None else torch.tensor([1.0, 2.0, 5.0])
        self.loss_fn = CausalLoss(
            lambda_dag=lambda_dag,
            lambda_contrastive=lambda_contrastive,
            node_class_weights=cw,
        )

        # Metrics
        metric_args = dict(num_classes=NUM_CLASSES, average="macro")
        self.train_metrics = MetricCollection({
            "acc": MulticlassAccuracy(**metric_args),
            "f1":  MulticlassF1Score(**metric_args),
        }, prefix="train/")

        self.val_metrics = MetricCollection({
            "acc":  MulticlassAccuracy(**metric_args),
            "f1":   MulticlassF1Score(**metric_args),
            "auroc": MulticlassAUROC(num_classes=NUM_CLASSES, average="macro"),
        }, prefix="val/")

        self.test_metrics = self.val_metrics.clone(prefix="test/")

    # ── Forward pass ────────────────────────────────────────────────────────

    def forward(self, batch) -> dict[str, torch.Tensor]:
        return self.model(
            batch.x,
            batch.edge_index,
            batch.edge_attr if hasattr(batch, "edge_attr") else None,
            batch.batch,
        )

    # ── Shared step ─────────────────────────────────────────────────────────

    def _shared_step(self, batch, stage: str) -> dict[str, torch.Tensor]:
        out   = self.forward(batch)
        loss_dict = self.loss_fn(
            out,
            node_labels=batch.y,
            graph_labels=batch.graph_y.view(-1),
        )

        node_probs = F.softmax(out["node_logits"], dim=-1)
        preds      = node_probs.argmax(dim=-1)

        loss = loss_dict["total"]
        self.log(f"{stage}/loss",       loss,                    prog_bar=True)
        self.log(f"{stage}/ce_node",    loss_dict["ce_node"])
        self.log(f"{stage}/ce_graph",   loss_dict["ce_graph"])
        self.log(f"{stage}/dag_pen",    loss_dict["dag_penalty"])
        self.log(f"{stage}/contrastive",loss_dict["contrastive"])

        return {
            "loss":   loss,
            "preds":  preds,
            "labels": batch.y,
            "probs":  node_probs,
        }

    # ── Training ────────────────────────────────────────────────────────────

    def training_step(self, batch, batch_idx: int) -> torch.Tensor:
        step = self._shared_step(batch, "train")
        metrics = self.train_metrics(step["preds"], step["labels"])
        self.log_dict(metrics, prog_bar=True, on_step=False, on_epoch=True)
        return step["loss"]

    def on_train_epoch_end(self) -> None:
        self.train_metrics.reset()

    # ── Validation ──────────────────────────────────────────────────────────

    def validation_step(self, batch, batch_idx: int) -> None:
        step = self._shared_step(batch, "val")
        self.val_metrics.update(step["preds"], step["labels"])
        # AUROC needs probabilities
        self.val_metrics["val/auroc"].update(step["probs"], step["labels"])

    def on_validation_epoch_end(self) -> None:
        metrics = self.val_metrics.compute()
        self.log_dict(metrics, prog_bar=True)
        self.val_metrics.reset()

    # ── Test ────────────────────────────────────────────────────────────────

    def test_step(self, batch, batch_idx: int) -> None:
        step = self._shared_step(batch, "test")
        self.test_metrics.update(step["preds"], step["labels"])

    def on_test_epoch_end(self) -> None:
        metrics = self.test_metrics.compute()
        self.log_dict(metrics)
        self.test_metrics.reset()

    # ── Optimiser + scheduler ────────────────────────────────────────────────

    def configure_optimizers(self) -> dict[str, Any]:
        # Weight decay applied only to non-bias, non-norm parameters
        decay_params    = []
        no_decay_params = []
        for name, param in self.model.named_parameters():
            if not param.requires_grad:
                continue
            if "bias" in name or "norm" in name or "batch_norm" in name:
                no_decay_params.append(param)
            else:
                decay_params.append(param)

        optimiser = AdamW([
            {"params": decay_params,    "weight_decay": self.hparams.weight_decay},
            {"params": no_decay_params, "weight_decay": 0.0},
        ], lr=self.hparams.learning_rate)

        scheduler = CosineAnnealingWarmRestarts(
            optimiser,
            T_0=self.hparams.t_max,
            T_mult=2,
            eta_min=1e-6,
        )

        return {
            "optimizer":  optimiser,
            "lr_scheduler": {
                "scheduler": scheduler,
                "interval":  "epoch",
                "frequency": 1,
                "monitor":   "val/loss",
            },
        }


# ─── Training entrypoint ──────────────────────────────────────────────────────

def train(args: argparse.Namespace) -> None:
    """Full training pipeline."""
    torch.set_float32_matmul_precision("high")

    # ── Data ────────────────────────────────────────────────────────────────
    dm = CCDTDataModule(
        scenario_dir=args.scenario_dir,
        num_samples=args.num_samples,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        val_split=0.15,
        test_split=0.10,
    )
    dm.setup("fit")
    class_weights = dm.class_weights()

    # ── Model ────────────────────────────────────────────────────────────────
    model = CCDTLightningModule(
        learning_rate=args.lr,
        lambda_dag=args.lambda_dag,
        lambda_contrastive=args.lambda_contrastive,
        class_weights=class_weights,
        hidden_dim=128,
        num_heads=8,
        num_layers=4,
        dropout=0.15,
    )

    # ── Callbacks ────────────────────────────────────────────────────────────
    ckpt_dir = Path(args.checkpoint_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    callbacks = [
        ModelCheckpoint(
            dirpath=str(ckpt_dir),
            filename="ccdt-gnn-{epoch:03d}-{val/f1:.4f}",
            monitor="val/f1",
            mode="max",
            save_top_k=3,
            save_last=True,
            verbose=True,
        ),
        EarlyStopping(
            monitor="val/f1",
            patience=15,
            mode="max",
            verbose=True,
            min_delta=0.001,
        ),
        LearningRateMonitor(logging_interval="epoch"),
        RichProgressBar(),
    ]

    # ── Logger ───────────────────────────────────────────────────────────────
    tb_logger = TensorBoardLogger(save_dir=args.log_dir, name="ccdt-gnn")

    # ── Trainer ──────────────────────────────────────────────────────────────
    precision = "bf16-mixed" if torch.cuda.is_bf16_supported() else "16-mixed"
    trainer = Trainer(
        max_epochs=args.epochs,
        accelerator="auto",
        devices=args.gpus if args.gpus > 0 else "auto",
        precision=precision if args.gpus > 0 else "32-true",
        callbacks=callbacks,
        logger=tb_logger,
        gradient_clip_val=1.0,
        log_every_n_steps=10,
        val_check_interval=1.0,
        enable_model_summary=True,
    )

    logger.info("Starting training: epochs=%d batch=%d lr=%g", args.epochs, args.batch_size, args.lr)
    trainer.fit(model, dm)

    # ── Test ─────────────────────────────────────────────────────────────────
    trainer.test(model, dm, ckpt_path="best")
    logger.info("Training complete. Best checkpoint: %s", callbacks[0].best_model_path)

    # ── Export to ONNX ───────────────────────────────────────────────────────
    if args.export_onnx:
        export_path = ckpt_dir / "ccdt_gnn.onnx"
        _export_onnx(model.model, export_path)


def _export_onnx(model, path: Path) -> None:
    """Export the GNN to ONNX for production inference."""
    import torch
    model.eval()
    dummy_x          = torch.randn(10, 17)
    dummy_edge_index = torch.randint(0, 10, (2, 20))
    dummy_edge_attr  = torch.randn(20, 4)

    try:
        torch.onnx.export(
            model.gnn,
            (dummy_x, dummy_edge_index, dummy_edge_attr),
            str(path),
            input_names=["x", "edge_index", "edge_attr"],
            output_names=["node_logits", "graph_logits"],
            opset_version=17,
            dynamic_axes={
                "x":          {0: "num_nodes"},
                "edge_index": {1: "num_edges"},
                "edge_attr":  {0: "num_edges"},
                "node_logits":{0: "num_nodes"},
            },
        )
        logger.info("ONNX model exported: %s", path)
    except Exception as exc:
        logger.warning("ONNX export failed: %s", exc)


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Train CCDT Causal GNN")
    parser.add_argument("--epochs",          type=int,   default=100)
    parser.add_argument("--batch-size",      type=int,   default=32)
    parser.add_argument("--lr",              type=float, default=3e-4)
    parser.add_argument("--lambda-dag",      type=float, default=0.1)
    parser.add_argument("--lambda-contrastive", type=float, default=0.05)
    parser.add_argument("--num-samples",     type=int,   default=4000)
    parser.add_argument("--num-workers",     type=int,   default=4)
    parser.add_argument("--gpus",            type=int,   default=0)
    parser.add_argument("--scenario-dir",    type=str,   default="data")
    parser.add_argument("--checkpoint-dir",  type=str,   default="checkpoints")
    parser.add_argument("--log-dir",         type=str,   default="logs")
    parser.add_argument("--export-onnx",     action="store_true")

    args = parser.parse_args()
    train(args)
