#!/usr/bin/env python3
"""
CCDT Layer-2 Cognitive Core — GNN Training Script
══════════════════════════════════════════════════
Standalone CLI to train the Causal GNN model.

Usage (inside Docker via make train-gnn-quick / make train-gnn):
    python train_gnn.py --quick
    python train_gnn.py --epochs 50 --num-samples 4000

Usage (local, from services/layer2-cognitive/):
    python train_gnn.py --quick --checkpoint-dir ./checkpoints
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ccdt.cognitive.train")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train the CCDT Causal GNN (Layer-2)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--epochs",         type=int,   default=50)
    parser.add_argument("--batch-size",     type=int,   default=32)
    parser.add_argument("--lr",             type=float, default=3e-4)
    parser.add_argument("--num-samples",    type=int,   default=4000,
                        help="Synthetic training samples to generate")
    parser.add_argument("--num-workers",    type=int,   default=0,
                        help="DataLoader workers (0 = main process, safest on macOS)")
    parser.add_argument("--scenario-dir",   type=str,   default="data")
    parser.add_argument("--checkpoint-dir", type=str,   default="checkpoints")
    parser.add_argument("--device",         type=str,   default="cpu",
                        choices=["cpu", "cuda", "mps"])
    parser.add_argument("--quick",          action="store_true",
                        help="3 epochs, 200 samples — fast smoke test")
    args = parser.parse_args()

    if args.quick:
        logger.info("⚡ Quick mode — 3 epochs, 200 samples")
        args.epochs      = 3
        args.num_samples = 200
        args.num_workers = 0

    # Ensure checkpoint dir exists and is writable.
    # "docker compose run --rm" doesn't always auto-create named volumes,
    # so we mkdir explicitly and fall back to /tmp if the path is read-only.
    checkpoint_dir = Path(args.checkpoint_dir)
    try:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        _probe = checkpoint_dir / ".write_test"
        _probe.touch()
        _probe.unlink()
    except (OSError, PermissionError):
        _fallback = Path("/tmp/ccdt-checkpoints")
        _fallback.mkdir(parents=True, exist_ok=True)
        logger.warning("Cannot write to %s — falling back to %s", checkpoint_dir, _fallback)
        checkpoint_dir = _fallback

    logger.info("─────────────────────────────────────────────────")
    logger.info("  CCDT Causal GNN Training")
    logger.info("  Epochs      : %d", args.epochs)
    logger.info("  Batch size  : %d", args.batch_size)
    logger.info("  Samples     : %s", f"{args.num_samples:,}")
    logger.info("  Device      : %s", args.device)
    logger.info("  Scenarios   : %s", args.scenario_dir)
    logger.info("  Checkpoints : %s", checkpoint_dir)
    logger.info("─────────────────────────────────────────────────")

    # ── Imports (after arg-parse so --help is instant) ────────────────────────
    try:
        import torch
    except ImportError:
        logger.error("PyTorch not installed. Run: pip install torch")
        sys.exit(1)

    try:
        from torch_geometric.loader import DataLoader
    except ImportError:
        logger.error("torch-geometric not installed.")
        logger.error("Run: pip install torch-geometric")
        sys.exit(1)

    # Import the ACTUAL names from causal_gnn.py
    try:
        from models.causal_gnn import (
            CausalGNN,        # ← real model class
            CausalLoss,       # ← real loss class
            build_model,      # ← factory function
        )
    except ImportError as e:
        logger.error("Cannot import from models.causal_gnn: %s", e)
        logger.error("Make sure PYTHONPATH=/app and you are inside the container.")
        sys.exit(1)

    try:
        from training.dataset import CausalIncidentDataset
    except ImportError as e:
        logger.error("Cannot import CausalIncidentDataset: %s", e)
        sys.exit(1)

    # ── Device ────────────────────────────────────────────────────────────────
    if args.device == "mps" and not torch.backends.mps.is_available():
        logger.warning("MPS not available — falling back to CPU")
        args.device = "cpu"
    if args.device == "cuda" and not torch.cuda.is_available():
        logger.warning("CUDA not available — falling back to CPU")
        args.device = "cpu"
    device = torch.device(args.device)
    logger.info("Using device: %s", device)

    # ── Dataset ───────────────────────────────────────────────────────────────
    logger.info("Building synthetic dataset (%d samples) ...", args.num_samples)
    t0 = time.time()

    full_dataset = CausalIncidentDataset(
        scenario_dir=args.scenario_dir,
        num_samples=args.num_samples,
        augment=True,
        seed=42,
    )

    n_total = len(full_dataset)
    n_train = int(n_total * 0.80)
    n_val   = int(n_total * 0.10)
    n_test  = n_total - n_train - n_val

    train_ds, val_ds, test_ds = torch.utils.data.random_split(
        full_dataset,
        [n_train, n_val, n_test],
        generator=torch.Generator().manual_seed(42),
    )

    train_loader = DataLoader(train_ds, batch_size=args.batch_size,
                              shuffle=True,  num_workers=args.num_workers,
                              drop_last=False)
    val_loader   = DataLoader(val_ds,   batch_size=args.batch_size,
                              shuffle=False, num_workers=args.num_workers)
    test_loader  = DataLoader(test_ds,  batch_size=args.batch_size,
                              shuffle=False, num_workers=args.num_workers)

    logger.info("Dataset ready in %.1fs — train=%d  val=%d  test=%d",
                time.time() - t0, n_train, n_val, n_test)

    # ── Model ─────────────────────────────────────────────────────────────────
    logger.info("Initialising Causal GNN (CausalGNN) ...")
    model     = build_model().to(device)
    criterion = CausalLoss(lambda_node=1.0, lambda_graph=1.0, lambda_dag=1e-5)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
        optimizer, T_0=max(args.epochs // 5, 1), T_mult=2,
    )

    n_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logger.info("Model parameters: %s", f"{n_params:,}")

    # ── Training loop ─────────────────────────────────────────────────────────
    best_val_loss = float("inf")
    best_epoch    = 0
    train_start   = time.time()

    for epoch in range(1, args.epochs + 1):

        # ── Train ──────────────────────────────────────────────────────────────
        model.train()
        train_loss_sum = 0.0
        train_correct  = 0
        train_total    = 0

        for batch in train_loader:
            batch = batch.to(device)
            optimizer.zero_grad()

            # CausalGNN.forward returns:
            #   node_logits  [N, 3]
            #   graph_logits [B, 3]
            #   attn_weights list
            #   A_soft       [n, n]
            node_logits, graph_logits, _, A_soft = model(
                batch.x,
                batch.edge_index,
                getattr(batch, "edge_attr", None),
                getattr(batch, "batch",     None),
            )

            # CausalLoss.forward expects explicit tensors:
            #   node_logits, graph_logits, A_soft, node_labels, graph_labels
            loss, _ = criterion(
                node_logits,
                graph_logits,
                A_soft,
                batch.y,                           # node labels
                batch.graph_y.squeeze(-1),         # graph labels [B]
            )

            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()

            train_loss_sum += loss.item() * int(batch.num_graphs)
            pred            = graph_logits.argmax(dim=1)
            train_correct  += (pred == batch.graph_y.squeeze(-1)).sum().item()
            train_total    += int(batch.num_graphs)

        scheduler.step()
        train_loss = train_loss_sum / max(n_train, 1)
        train_acc  = train_correct  / max(train_total, 1)

        # ── Validate ───────────────────────────────────────────────────────────
        model.eval()
        val_loss_sum = 0.0
        val_correct  = 0
        val_total    = 0

        with torch.no_grad():
            for batch in val_loader:
                batch = batch.to(device)
                node_logits, graph_logits, _, A_soft = model(
                    batch.x,
                    batch.edge_index,
                    getattr(batch, "edge_attr", None),
                    getattr(batch, "batch",     None),
                )
                loss, _ = criterion(
                    node_logits,
                    graph_logits,
                    A_soft,
                    batch.y,
                    batch.graph_y.squeeze(-1),
                )
                val_loss_sum += loss.item() * int(batch.num_graphs)
                pred          = graph_logits.argmax(dim=1)
                val_correct  += (pred == batch.graph_y.squeeze(-1)).sum().item()
                val_total    += int(batch.num_graphs)

        val_loss = val_loss_sum / max(n_val, 1)
        val_acc  = val_correct  / max(val_total, 1)

        # ── Save checkpoint ───────────────────────────────────────────────────
        is_best = val_loss < best_val_loss
        if is_best:
            best_val_loss = val_loss
            best_epoch    = epoch
            torch.save({
                "epoch":       epoch,
                "model_state": model.state_dict(),
                "optimizer":   optimizer.state_dict(),
                "val_loss":    val_loss,
                "val_acc":     val_acc,
                "args":        vars(args),
            }, checkpoint_dir / "causal_gnn_best.pt")

        torch.save({
            "epoch":       epoch,
            "model_state": model.state_dict(),
            "optimizer":   optimizer.state_dict(),
            "val_loss":    val_loss,
        }, checkpoint_dir / "causal_gnn_latest.pt")

        star    = " ⭐" if is_best else ""
        elapsed = time.time() - train_start
        logger.info(
            "Epoch %3d/%d  train_loss=%.4f  acc=%.2f  "
            "val_loss=%.4f  val_acc=%.2f  [%.0fs]%s",
            epoch, args.epochs,
            train_loss, train_acc,
            val_loss,   val_acc,
            elapsed,    star,
        )

    # ── Test set evaluation ───────────────────────────────────────────────────
    logger.info("Evaluating best model on test set ...")
    ckpt = torch.load(checkpoint_dir / "causal_gnn_best.pt", map_location=device)
    model.load_state_dict(ckpt["model_state"])
    model.eval()

    test_correct = 0
    test_total   = 0
    with torch.no_grad():
        for batch in test_loader:
            batch = batch.to(device)
            _, graph_logits, _, _ = model(
                batch.x,
                batch.edge_index,
                getattr(batch, "edge_attr", None),
                getattr(batch, "batch",     None),
            )
            pred          = graph_logits.argmax(dim=1)
            test_correct += (pred == batch.graph_y.squeeze(-1)).sum().item()
            test_total   += int(batch.num_graphs)

    test_acc = test_correct / max(test_total, 1)
    elapsed  = time.time() - train_start

    logger.info("─────────────────────────────────────────────────")
    logger.info("  ✅ Training complete in %.1f minutes", elapsed / 60)
    logger.info("  Best epoch   : %d  (val_loss=%.4f)", best_epoch, best_val_loss)
    logger.info("  Test accuracy: %.1f%%  (%d/%d correct)",
                test_acc * 100, test_correct, test_total)
    logger.info("  Best model   : %s", checkpoint_dir / "causal_gnn_best.pt")
    logger.info("─────────────────────────────────────────────────")


if __name__ == "__main__":
    main()
