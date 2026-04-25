"""
CCDT Layer-2 Cognitive Core — Model Evaluator
───────────────────────────────────────────────
Evaluates a trained CausalGNN checkpoint on a test dataset.
Produces per-class and macro-averaged metrics plus a confusion matrix.

Usage:
    python evaluator.py --checkpoint checkpoints/ccdt-gnn-best.ckpt \
                        --scenario-dir data \
                        --output-dir reports/

Outputs:
  - metrics.json         → machine-readable metric dict
  - confusion_matrix.png → (optional, requires matplotlib)
  - report.txt           → human-readable markdown report
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import torch.nn.functional as F
from torch_geometric.loader import DataLoader
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
    roc_auc_score,
)

logger = logging.getLogger("ccdt.cognitive.evaluator")

CLASS_NAMES = ["healthy", "fault", "attack"]
NUM_CLASSES = 3


# ─── compute_metrics ─────────────────────────────────────────────────────────

def compute_metrics(
    all_preds:  np.ndarray,   # (N,) predicted class indices
    all_labels: np.ndarray,   # (N,) ground-truth class indices
    all_probs:  np.ndarray,   # (N, 3) softmax probabilities
) -> dict:
    """
    Compute full evaluation metrics from flattened prediction arrays.

    Returns:
        dict with keys:
          accuracy, macro_f1, weighted_f1
          per_class: {class_name: {precision, recall, f1, support}}
          auroc_macro, auroc_per_class
          confusion_matrix (list of lists)
    """
    accuracy = float(accuracy_score(all_labels, all_preds))
    macro_f1 = float(f1_score(all_labels, all_preds,
                     average="macro",    zero_division=0))
    weighted_f1 = float(f1_score(all_labels, all_preds,
                        average="weighted", zero_division=0))

    # Per-class metrics from sklearn
    report = classification_report(
        all_labels, all_preds,
        target_names=CLASS_NAMES,
        output_dict=True,
        zero_division=0,
    )

    per_class = {
        cls: {
            "precision": round(float(report[cls]["precision"]), 4),
            "recall":    round(float(report[cls]["recall"]),    4),
            "f1":        round(float(report[cls]["f1-score"]),  4),
            "support":   int(report[cls]["support"]),
        }
        for cls in CLASS_NAMES
        if cls in report
    }

    # AUROC (needs probabilities)
    try:
        auroc_macro = float(roc_auc_score(
            all_labels, all_probs,
            multi_class="ovr", average="macro",
        ))
        auroc_per_class = {}
        for i, cls in enumerate(CLASS_NAMES):
            binary_labels = (all_labels == i).astype(int)
            try:
                auroc_per_class[cls] = round(
                    float(roc_auc_score(binary_labels, all_probs[:, i])), 4)
            except Exception:
                auroc_per_class[cls] = 0.0
    except Exception as exc:
        logger.warning("AUROC computation failed: %s", exc)
        auroc_macro = 0.0
        auroc_per_class = {cls: 0.0 for cls in CLASS_NAMES}

    cm = confusion_matrix(all_labels, all_preds, labels=[0, 1, 2]).tolist()

    return {
        "accuracy":       round(accuracy, 4),
        "macro_f1":       round(macro_f1, 4),
        "weighted_f1":    round(weighted_f1, 4),
        "auroc_macro":    round(auroc_macro, 4),
        "auroc_per_class": auroc_per_class,
        "per_class":      per_class,
        "confusion_matrix": cm,
        "num_samples":    len(all_labels),
    }


# ─── Evaluator class ──────────────────────────────────────────────────────────

class ModelEvaluator:
    """
    Evaluates a CCDTCognitiveModel (or CCDTLightningModule checkpoint)
    on a CausalIncidentDataset test split.
    """

    def __init__(
        self,
        model,
        device: Optional[torch.device] = None,
    ) -> None:
        self.model = model
        self.device = device or torch.device(
            "cuda" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device)
        self.model.eval()

    @torch.no_grad()
    def evaluate(
        self,
        dataloader: DataLoader,
        verbose:    bool = True,
    ) -> dict:
        """
        Run inference over the full dataloader and compute all metrics.

        Returns:
            dict from compute_metrics()
        """
        all_preds:  list[np.ndarray] = []
        all_labels: list[np.ndarray] = []
        all_probs:  list[np.ndarray] = []

        for batch in dataloader:
            batch = batch.to(self.device)
            out = self.model(
                batch.x,
                batch.edge_index,
                batch.edge_attr if hasattr(batch, "edge_attr") else None,
                batch.batch,
            )
            node_probs = F.softmax(out["node_logits"], dim=-1)
            preds = node_probs.argmax(dim=-1)

            all_preds.append(preds.cpu().numpy())
            all_labels.append(batch.y.cpu().numpy())
            all_probs.append(node_probs.cpu().numpy())

        preds_arr = np.concatenate(all_preds)
        labels_arr = np.concatenate(all_labels)
        probs_arr = np.concatenate(all_probs)

        metrics = compute_metrics(preds_arr, labels_arr, probs_arr)

        if verbose:
            self._print_report(metrics)

        return metrics

    @staticmethod
    def _print_report(metrics: dict) -> None:
        """Print a formatted metrics summary."""
        print("\n" + "═" * 55)
        print("  CCDT Causal GNN — Evaluation Report")
        print("═" * 55)
        print(f"  Samples evaluated : {metrics['num_samples']}")
        print(f"  Accuracy          : {metrics['accuracy']:.4f}")
        print(f"  Macro F1          : {metrics['macro_f1']:.4f}")
        print(f"  Weighted F1       : {metrics['weighted_f1']:.4f}")
        print(f"  AUROC (macro)     : {metrics['auroc_macro']:.4f}")
        print()
        print("  Per-class metrics:")
        print(
            f"  {'Class':<12} {'Precision':>9} {'Recall':>7} {'F1':>7} {'AUROC':>7} {'Support':>8}")
        print("  " + "-" * 53)
        for cls in CLASS_NAMES:
            pc = metrics["per_class"].get(cls, {})
            auroc = metrics["auroc_per_class"].get(cls, 0.0)
            print(
                f"  {cls:<12} "
                f"{pc.get('precision', 0):.4f}    "
                f"{pc.get('recall', 0):.4f}  "
                f"{pc.get('f1', 0):.4f}  "
                f"{auroc:.4f}  "
                f"{pc.get('support', 0):>7}"
            )
        print()
        print("  Confusion matrix (rows=true, cols=pred):")
        print(f"  {'':12} {'healthy':>7} {'fault':>7} {'attack':>7}")
        for i, row in enumerate(metrics["confusion_matrix"]):
            print(
                f"  {CLASS_NAMES[i]:<12} {row[0]:>7} {row[1]:>7} {row[2]:>7}")
        print("═" * 55 + "\n")

    def save_metrics(self, metrics: dict, output_dir: str) -> None:
        """Save metrics.json and a markdown report.txt to output_dir."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        # metrics.json
        metrics_path = out / "metrics.json"
        with open(metrics_path, "w") as f:
            json.dump(metrics, f, indent=2)
        logger.info("Metrics saved: %s", metrics_path)

        # report.txt
        report_path = out / "report.txt"
        with open(report_path, "w") as f:
            f.write("# CCDT Causal GNN — Evaluation Report\n\n")
            f.write(f"Samples    : {metrics['num_samples']}\n")
            f.write(f"Accuracy   : {metrics['accuracy']:.4f}\n")
            f.write(f"Macro F1   : {metrics['macro_f1']:.4f}\n")
            f.write(f"AUROC      : {metrics['auroc_macro']:.4f}\n\n")
            f.write("## Per-class\n\n")
            f.write("| Class   | Precision | Recall |    F1  | AUROC  |\n")
            f.write("|---------|-----------|--------|--------|--------|\n")
            for cls in CLASS_NAMES:
                pc = metrics["per_class"].get(cls, {})
                auroc = metrics["auroc_per_class"].get(cls, 0.0)
                f.write(
                    f"| {cls:<7} | "
                    f"{pc.get('precision',0):.4f}    | "
                    f"{pc.get('recall',0):.4f}  | "
                    f"{pc.get('f1',0):.4f}  | "
                    f"{auroc:.4f} |\n"
                )
            f.write("\n## Confusion matrix\n\n")
            f.write("rows=true, cols=pred  | healthy | fault | attack\n")
            for i, row in enumerate(metrics["confusion_matrix"]):
                f.write(
                    f"{CLASS_NAMES[i]:<20} | {row[0]:>7} | {row[1]:>5} | {row[2]:>6}\n")

        logger.info("Report saved: %s", report_path)

    def try_plot_confusion_matrix(self, metrics: dict, output_dir: str) -> None:
        """Plot confusion matrix with matplotlib (optional dependency)."""
        try:
            import matplotlib.pyplot as plt
            import seaborn as sns

            cm = np.array(metrics["confusion_matrix"])
            fig, ax = plt.subplots(figsize=(6, 5))
            sns.heatmap(
                cm, annot=True, fmt="d", cmap="Blues",
                xticklabels=CLASS_NAMES, yticklabels=CLASS_NAMES,
                ax=ax,
            )
            ax.set_xlabel("Predicted")
            ax.set_ylabel("True")
            ax.set_title("CCDT GNN — Confusion Matrix")
            path = Path(output_dir) / "confusion_matrix.png"
            fig.savefig(path, bbox_inches="tight", dpi=150)
            plt.close(fig)
            logger.info("Confusion matrix saved: %s", path)
        except ImportError:
            logger.info(
                "matplotlib/seaborn not installed — skipping confusion matrix plot")


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Evaluate CCDT Causal GNN checkpoint")
    parser.add_argument("--checkpoint",   type=str,
                        required=True, help="Path to .ckpt file")
    parser.add_argument("--scenario-dir", type=str, default="data")
    parser.add_argument("--output-dir",   type=str, default="reports")
    parser.add_argument("--batch-size",   type=int, default=32)
    parser.add_argument("--num-samples",  type=int, default=2000)
    parser.add_argument("--plot",         action="store_true",
                        help="Plot confusion matrix")
    args = parser.parse_args()

    # Import here to avoid circular deps when used as a module
    from training.trainer import CCDTLightningModule
    from training.dataset import CausalIncidentDataset

    # Load model
    logger.info("Loading checkpoint: %s", args.checkpoint)
    lit_model = CCDTLightningModule.load_from_checkpoint(args.checkpoint)
    model = lit_model.model

    # Load test data
    test_ds = CausalIncidentDataset(
        scenario_dir=args.scenario_dir,
        num_samples=args.num_samples,
        split="test",
        augment=False,
    )
    test_dl = DataLoader(test_ds, batch_size=args.batch_size,
                         shuffle=False, num_workers=2)
    logger.info("Test set size: %d graphs", len(test_ds))

    # Evaluate
    evaluator = ModelEvaluator(model)
    metrics = evaluator.evaluate(test_dl, verbose=True)
    evaluator.save_metrics(metrics, args.output_dir)

    if args.plot:
        evaluator.try_plot_confusion_matrix(metrics, args.output_dir)
