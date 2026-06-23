from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import torch


def compute_norm_stats(
    activations: np.memmap,
    batch_size: int = 8192,
    max_samples: int | None = None,
) -> dict[str, list[float] | float]:
    n = len(activations) if max_samples is None else min(len(activations), max_samples)
    mean = np.zeros(activations.shape[1], dtype=np.float64)
    sq_mean = np.zeros(activations.shape[1], dtype=np.float64)

    for start in range(0, n, batch_size):
        end = min(start + batch_size, n)
        batch = np.asarray(activations[start:end], dtype=np.float64)
        mean += batch.sum(axis=0)
        sq_mean += (batch**2).sum(axis=0)

    mean /= n
    variance = sq_mean / n - mean**2
    variance = np.clip(variance, a_min=1e-8, a_max=None)
    rms = float(np.sqrt(variance.mean()))
    return {
        "mean": mean.astype(np.float32).tolist(),
        "rms": rms,
    }


def save_norm_stats(stats: dict, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats, indent=2))


def load_norm_stats(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def normalize_activations(x: torch.Tensor, stats: dict) -> torch.Tensor:
    mean = torch.tensor(stats["mean"], device=x.device, dtype=x.dtype)
    centered = x - mean
    return centered / stats["rms"]


def denormalize_activations(x: torch.Tensor, stats: dict) -> torch.Tensor:
    mean = torch.tensor(stats["mean"], device=x.device, dtype=x.dtype)
    return x * stats["rms"] + mean
