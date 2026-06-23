from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from hpsv3.sae.dataset_io import create_activation_bank, load_hpdv3_samples


def shard_range(n_total: int, shard_id: int, num_shards: int) -> tuple[int, int]:
    if not 0 <= shard_id < num_shards:
        raise ValueError(f"shard_id must be in [0, {num_shards}), got {shard_id}")
    chunk_size = (n_total + num_shards - 1) // num_shards
    start = shard_id * chunk_size
    end = min(start + chunk_size, n_total)
    return start, end


def wait_for_file(path: Path, timeout_s: int = 3600) -> None:
    deadline = time.time() + timeout_s
    while not path.exists():
        if time.time() > deadline:
            raise TimeoutError(f"Timed out waiting for {path}")
        time.sleep(5)


def ensure_samples_and_bank(
    output_dir: str | Path,
    dataset_root: str | Path,
    target_images: int | None,
    d_in: int = 3584,
) -> pd.DataFrame:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    samples_path = output / "samples.parquet"
    activations_path = output / "activations.npy"

    if samples_path.exists():
        samples = pd.read_parquet(samples_path)
    else:
        samples = load_hpdv3_samples(dataset_root, target_images=target_images)
        samples.to_parquet(samples_path, index=False)

    if not activations_path.exists():
        create_activation_bank(activations_path, len(samples), d_in=d_in)
    return samples
