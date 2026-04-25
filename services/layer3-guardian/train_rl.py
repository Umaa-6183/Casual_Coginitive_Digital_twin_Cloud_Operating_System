#!/usr/bin/env python3
"""
CCDT Layer-3 Guardian — RL Training Script
═══════════════════════════════════════════
Standalone CLI to train the Guardian PPO agent.

Usage (inside Docker via make train-rl):
    python train_rl.py --timesteps 500000 --checkpoint-dir /app/checkpoints

Usage (local):
    cd services/layer3-guardian
    python train_rl.py --timesteps 50000 --n-envs 1 --checkpoint-dir ./checkpoints
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ccdt.guardian.train")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train the CCDT Guardian RL Agent (PPO)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--timesteps",       type=int,   default=500_000,
                        help="Total environment timesteps to train for")
    parser.add_argument("--n-envs",          type=int,   default=4,
                        help="Number of parallel environments (reduce to 1 on low RAM)")
    parser.add_argument("--checkpoint-dir",  type=str,   default="checkpoints",
                        help="Directory to save model checkpoints")
    parser.add_argument("--checkpoint-freq", type=int,   default=50_000,
                        help="Save checkpoint every N timesteps")
    parser.add_argument("--eval-freq",       type=int,   default=25_000,
                        help="Evaluate agent every N timesteps")
    parser.add_argument("--eval-episodes",   type=int,   default=20,
                        help="Episodes per evaluation")
    parser.add_argument("--device",          type=str,   default="cpu",
                        choices=["cpu", "cuda", "mps"],
                        help="Torch device (use cpu on macOS, mps on Apple Silicon optionally)")
    parser.add_argument("--resume",          type=str,   default=None,
                        help="Path to checkpoint ZIP to resume from")
    parser.add_argument("--quick",           action="store_true",
                        help="Quick smoke-test: 5000 timesteps, 1 env (for CI or first run)")
    args = parser.parse_args()

    # Quick mode override (useful for testing the training pipeline works)
    if args.quick:
        logger.info("⚡ Quick mode — 5000 timesteps, 1 env")
        args.timesteps = 5_000
        args.n_envs = 1

    checkpoint_dir = Path(args.checkpoint_dir)
    try:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        _probe = checkpoint_dir / ".write_test"
        _probe.touch()
        _probe.unlink()
    except (OSError, PermissionError):
        _fallback = Path("/tmp/ccdt-rl-checkpoints")
        _fallback.mkdir(parents=True, exist_ok=True)
        logger.warning("Cannot write to %s — falling back to %s",
                       checkpoint_dir, _fallback)
        checkpoint_dir = _fallback

    logger.info("─────────────────────────────────────────────────")
    logger.info("  CCDT Guardian RL Training")
    logger.info("  Timesteps   : %s", f"{args.timesteps:,}")
    logger.info("  Parallel envs: %d", args.n_envs)
    logger.info("  Device      : %s", args.device)
    logger.info("  Checkpoints : %s", checkpoint_dir)
    logger.info("─────────────────────────────────────────────────")

    # ── Import after arg parse (avoids slow torch import on --help) ───────────
    try:
        import torch
        import numpy as np
        from stable_baselines3 import PPO
        from stable_baselines3.common.env_util import make_vec_env
        from stable_baselines3.common.callbacks import (
            CheckpointCallback, EvalCallback
        )
        from stable_baselines3.common.monitor import Monitor
        from stable_baselines3.common.vec_env import VecNormalize
    except ImportError as e:
        logger.error("Missing dependency: %s", e)
        logger.error(
            "Run:  pip install stable-baselines3 gymnasium torch numpy")
        sys.exit(1)

    try:
        from rl.env import ClusterIncidentEnv
        from rl.agent import GuardianAgent, PPO_HYPERPARAMS
    except ImportError as e:
        logger.error("Cannot import rl module: %s", e)
        logger.error(
            "Make sure you are running from services/layer3-guardian/ or /app/")
        sys.exit(1)

    # ── Build vectorised environment ──────────────────────────────────────────
    logger.info("Building %d parallel environments ...", args.n_envs)

    def make_env():
        env = ClusterIncidentEnv()
        env = Monitor(env)
        return env

    # On macOS with low RAM, use 1 env to avoid fork issues
    if args.n_envs == 1:
        from stable_baselines3.common.vec_env import DummyVecEnv
        vec_env = DummyVecEnv([make_env])
    else:
        vec_env = make_vec_env(make_env, n_envs=args.n_envs)

    # Normalise observations and rewards
    vec_env = VecNormalize(vec_env, norm_obs=True,
                           norm_reward=True, clip_obs=10.0)

    # Eval env (separate, not normalised by training stats)
    eval_env = VecNormalize(
        make_vec_env(make_env, n_envs=1),
        norm_obs=True, norm_reward=False, training=False,
    )

    # ── Build or resume model ─────────────────────────────────────────────────
    if args.resume:
        logger.info("Resuming from checkpoint: %s", args.resume)
        model = PPO.load(
            args.resume,
            env=vec_env,
            device=args.device,
        )
    else:
        logger.info("Initialising fresh PPO model ...")
        model = PPO(
            policy="MlpPolicy",
            env=vec_env,
            device=args.device,
            verbose=1,
            **PPO_HYPERPARAMS,
        )

    # ── Callbacks ─────────────────────────────────────────────────────────────
    callbacks = []

    # Save checkpoint every N timesteps
    checkpoint_cb = CheckpointCallback(
        save_freq=max(args.checkpoint_freq // args.n_envs, 1),
        save_path=str(checkpoint_dir),
        name_prefix="guardian_ppo",
        save_vecnormalize=True,
        verbose=1,
    )
    callbacks.append(checkpoint_cb)

    # Evaluate and keep best model
    eval_cb = EvalCallback(
        eval_env=eval_env,
        best_model_save_path=str(checkpoint_dir),
        log_path=str(checkpoint_dir / "eval_logs"),
        eval_freq=max(args.eval_freq // args.n_envs, 1),
        n_eval_episodes=args.eval_episodes,
        deterministic=True,
        verbose=1,
    )
    callbacks.append(eval_cb)

    # ── Train ─────────────────────────────────────────────────────────────────
    logger.info("Starting training — %s total timesteps ...",
                f"{args.timesteps:,}")
    t0 = time.time()

    try:
        model.learn(
            total_timesteps=args.timesteps,
            callback=callbacks,
            reset_num_timesteps=(args.resume is None),
        )
    except KeyboardInterrupt:
        logger.warning(
            "Training interrupted by user — saving current checkpoint ...")

    # ── Save final model ──────────────────────────────────────────────────────
    final_path = checkpoint_dir / "guardian_ppo_final"
    model.save(str(final_path))
    vec_env.save(str(checkpoint_dir / "vec_normalize.pkl"))

    elapsed = time.time() - t0
    logger.info("─────────────────────────────────────────────────")
    logger.info("  ✅ Training complete in %.1f minutes", elapsed / 60)
    logger.info("  Final model : %s.zip", final_path)
    logger.info("  Best model  : %s/best_model.zip", checkpoint_dir)
    logger.info("─────────────────────────────────────────────────")

    vec_env.close()
    eval_env.close()


if __name__ == "__main__":
    main()
