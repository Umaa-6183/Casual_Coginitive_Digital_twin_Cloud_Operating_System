"""
CCDT Layer-3 Guardian — PPO RL Agent
═══════════════════════════════════════════════════════════════════════════════
Wraps Stable-Baselines3 PPO with:
  - Multi-env parallel training (SubprocVecEnv)
  - Curriculum learning: starts with fault scenarios, then attack
  - Action confidence ranking for Ghost Preview
  - Checkpoint save/load
  - Inference in <1ms on CPU

Training:
    agent = GuardianAgent()
    agent.train(total_timesteps=500_000)
    agent.save("/app/checkpoints/guardian_ppo")

Inference:
    agent = GuardianAgent.load("/app/checkpoints/guardian_ppo")
    result = agent.predict(obs, topology=topo_dict)
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np
import torch
from stable_baselines3 import PPO
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.callbacks import (
    BaseCallback,
    CheckpointCallback,
    EvalCallback,
)
from stable_baselines3.common.monitor import Monitor
from stable_baselines3.common.vec_env import SubprocVecEnv, VecNormalize

from rl.env import ClusterIncidentEnv, ACTION_NAMES, NUM_ACTIONS
from rl.reward import action_reward_table

logger = logging.getLogger("ccdt.guardian.agent")


# ─── Hyperparameters ──────────────────────────────────────────────────────────

PPO_HYPERPARAMS = {
    "learning_rate":    3e-4,
    "n_steps":          2048,       # rollout buffer size per env
    "batch_size":       256,
    "n_epochs":         10,
    "gamma":            0.99,       # discount factor
    "gae_lambda":       0.95,       # GAE lambda
    "clip_range":       0.2,
    "ent_coef":         0.01,       # entropy coefficient (exploration)
    "vf_coef":          0.5,        # value function coefficient
    "max_grad_norm":    0.5,
    "policy_kwargs": {
        "net_arch": dict(pi=[256, 256], vf=[256, 256]),
        "activation_fn": torch.nn.ReLU,
    },
}


# ─── Training callback ────────────────────────────────────────────────────────

class GuardianTrainingCallback(BaseCallback):
    """
    Logs key training metrics every 'log_freq' steps.
    Monitors:
      - Mean episode reward
      - Mean episode length
      - Escalation rate (action 14 frequency)
      - Full recovery rate
    """

    def __init__(self, log_freq: int = 10_000, verbose: int = 1) -> None:
        super().__init__(verbose)
        self.log_freq = log_freq
        self._episode_count = 0
        self._escalations = 0
        self._recoveries = 0

    def _on_step(self) -> bool:
        if self.n_calls % self.log_freq == 0:
            if len(self.model.ep_info_buffer) > 0:
                rewards = [ep["r"] for ep in self.model.ep_info_buffer]
                lengths = [ep["l"] for ep in self.model.ep_info_buffer]
                logger.info(
                    "step=%d  mean_reward=%.2f  mean_ep_len=%.0f  "
                    "escalations=%d  recoveries=%d",
                    self.num_timesteps,
                    np.mean(rewards),
                    np.mean(lengths),
                    self._escalations,
                    self._recoveries,
                )
        return True


# ─── GuardianAgent ────────────────────────────────────────────────────────────

class GuardianAgent:
    """
    PPO-based reinforcement learning agent for cluster incident remediation.

    Attributes:
        model        Stable-Baselines3 PPO model
        checkpoint   Path to the most recently saved checkpoint
    """

    def __init__(
        self,
        model_path:     Optional[str] = None,
        device:         str = "cpu",
        n_envs:         int = 4,
        incident_types: list[str] = ("fault", "attack"),
    ) -> None:
        self.device = device
        self.n_envs = n_envs
        self.incident_types = list(incident_types)
        self.model: Optional[PPO] = None
        self.checkpoint = model_path

        if model_path and os.path.exists(model_path + ".zip"):
            self._load(model_path)
        else:
            self._init_model()

    # ── Initialisation ────────────────────────────────────────────────────────

    def _init_model(self) -> None:
        """Create a fresh PPO model with the cluster incident environment."""
        logger.info("Initialising fresh PPO model")

        vec_env = make_vec_env(
            lambda: Monitor(ClusterIncidentEnv(incident_type="fault")),
            n_envs=self.n_envs,
            vec_env_cls=SubprocVecEnv,
        )

        self.model = PPO(
            policy="MlpPolicy",
            env=vec_env,
            device=self.device,
            verbose=0,
            **PPO_HYPERPARAMS,
        )
        logger.info("PPO model created: obs_dim=%d  action_dim=%d",
                    vec_env.observation_space.shape[0],
                    vec_env.action_space.n)

    def _load(self, path: str) -> None:
        """Load a trained model from checkpoint."""
        logger.info("Loading PPO checkpoint: %s", path)
        vec_env = make_vec_env(
            lambda: Monitor(ClusterIncidentEnv()),
            n_envs=1,
        )
        self.model = PPO.load(path, env=vec_env, device=self.device)
        self.checkpoint = path
        logger.info("Checkpoint loaded")

    # ── Training ──────────────────────────────────────────────────────────────

    def train(
        self,
        total_timesteps: int = 500_000,
        checkpoint_dir:  str = "/app/checkpoints",
        log_dir:         str = "/app/logs",
        eval_freq:       int = 20_000,
    ) -> None:
        """
        Train the PPO agent with curriculum:
          Phase 1 (50%)  fault scenarios only
          Phase 2 (50%)  mixed fault + attack scenarios
        """
        os.makedirs(checkpoint_dir, exist_ok=True)
        os.makedirs(log_dir,        exist_ok=True)

        # ── Phase 1: fault only ───────────────────────────────────────────────
        phase1_steps = total_timesteps // 2
        logger.info(
            "Training Phase 1 — fault scenarios (%d steps)", phase1_steps)

        fault_env = make_vec_env(
            lambda: Monitor(ClusterIncidentEnv(incident_type="fault")),
            n_envs=self.n_envs,
            vec_env_cls=SubprocVecEnv,
        )
        fault_eval_env = make_vec_env(
            lambda: Monitor(ClusterIncidentEnv(incident_type="fault")),
            n_envs=1,
        )

        self.model.set_env(fault_env)
        self.model.learn(
            total_timesteps=phase1_steps,
            callback=self._build_callbacks(
                checkpoint_dir, fault_eval_env, eval_freq, "phase1"),
            reset_num_timesteps=True,
            progress_bar=True,
        )
        fault_env.close()
        fault_eval_env.close()

        # ── Phase 2: mixed fault + attack ─────────────────────────────────────
        phase2_steps = total_timesteps - phase1_steps
        logger.info(
            "Training Phase 2 — mixed scenarios (%d steps)", phase2_steps)

        def mixed_env_factory():
            import random
            t = random.choice(["fault", "attack"])
            return Monitor(ClusterIncidentEnv(incident_type=t))

        mixed_env = make_vec_env(
            mixed_env_factory,
            n_envs=self.n_envs,
            vec_env_cls=SubprocVecEnv,
        )
        mixed_eval_env = make_vec_env(mixed_env_factory, n_envs=1)

        self.model.set_env(mixed_env)
        self.model.learn(
            total_timesteps=phase2_steps,
            callback=self._build_callbacks(
                checkpoint_dir, mixed_eval_env, eval_freq, "phase2"),
            reset_num_timesteps=False,
            progress_bar=True,
        )
        mixed_env.close()
        mixed_eval_env.close()

        # Save final checkpoint
        final_path = os.path.join(checkpoint_dir, "guardian_ppo_final")
        self.save(final_path)
        logger.info("Training complete. Final checkpoint: %s", final_path)

    def _build_callbacks(
        self,
        checkpoint_dir: str,
        eval_env,
        eval_freq:  int,
        phase:      str,
    ) -> list[BaseCallback]:
        return [
            GuardianTrainingCallback(log_freq=5_000),
            CheckpointCallback(
                save_freq=eval_freq,
                save_path=checkpoint_dir,
                name_prefix=f"guardian_ppo_{phase}",
                verbose=0,
            ),
            EvalCallback(
                eval_env=eval_env,
                n_eval_episodes=20,
                eval_freq=eval_freq,
                best_model_save_path=os.path.join(checkpoint_dir, "best"),
                deterministic=True,
                verbose=1,
            ),
        ]

    # ── Inference ─────────────────────────────────────────────────────────────

    def predict(
        self,
        obs:          np.ndarray,
        deterministic: bool = True,
    ) -> tuple[int, float]:
        """
        Predict the best action for an observation.

        Returns:
            action       Action index (0-14)
            confidence   Probability of the chosen action (0-1)
        """
        if self.model is None:
            logger.warning("No model loaded — returning no_op")
            return 0, 1.0

        obs_tensor = np.array(obs, dtype=np.float32).reshape(1, -1)
        action, _ = self.model.predict(obs_tensor, deterministic=deterministic)
        action_int = int(action[0]) if hasattr(
            action, "__len__") else int(action)

        # Get probability distribution
        confidence = self._action_confidence(obs_tensor, action_int)
        return action_int, confidence

    def rank_actions(
        self,
        obs:     np.ndarray,
        top_k:   int = 5,
    ) -> list[dict]:
        """
        Return the top-k actions ranked by policy probability.

        Returns list of dicts: {action_id, name, confidence, expected_reward}
        """
        if self.model is None:
            return [{"action_id": 0, "name": "no_op", "confidence": 1.0, "expected_reward": 0.0}]

        obs_tensor = torch.tensor(
            np.array(obs, dtype=np.float32).reshape(1, -1),
            device=self.model.device,
        )

        try:
            with torch.no_grad():
                dist = self.model.policy.get_distribution(obs_tensor)
                # [NUM_ACTIONS]
                probs = dist.distribution.probs[0].cpu().numpy()
        except Exception:
            # Fallback: uniform distribution
            probs = np.ones(NUM_ACTIONS) / NUM_ACTIONS

        reward_table = action_reward_table()
        ranked = []
        for i in np.argsort(-probs)[:top_k]:
            ranked.append({
                "action_id":       int(i),
                "name":            ACTION_NAMES[int(i)],
                "confidence":      round(float(probs[i]), 4),
                "expected_reward": reward_table[int(i)]["expected_reward_incident"],
            })

        return ranked

    def _action_confidence(self, obs_tensor: np.ndarray, action: int) -> float:
        """Get the policy probability for a specific action."""
        try:
            t = torch.tensor(obs_tensor, device=self.model.device)
            with torch.no_grad():
                dist = self.model.policy.get_distribution(t)
                probs = dist.distribution.probs[0].cpu().numpy()
            return round(float(probs[action]), 4)
        except Exception:
            return 0.5

    def obs_from_topology(self, topology: dict) -> np.ndarray:
        """
        Convert a Layer-2 topology dict to an observation vector.
        Convenience method so callers don't need to instantiate the env.
        """
        env = ClusterIncidentEnv(topology=topology)
        obs, _ = env.reset()
        env.close()
        return obs

    # ── Persistence ──────────────────────────────────────────────────────────

    def save(self, path: str) -> None:
        """Save model checkpoint."""
        if self.model:
            self.model.save(path)
            self.checkpoint = path
            logger.info("Checkpoint saved: %s.zip", path)

    @classmethod
    def load(cls, path: str, device: str = "cpu") -> "GuardianAgent":
        """Load agent from checkpoint."""
        agent = cls.__new__(cls)
        agent.device = device
        agent.n_envs = 1
        agent.checkpoint = path
        agent._load(path)
        return agent
