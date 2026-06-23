from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import fire
import numpy as np
import pandas as pd

from hpsv3.sae.dataset_io import open_activation_bank

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)


def merge_activations(
    output_dir: str = "/gpfs/scratch1/shared/scur0077/sae/activations",
    num_shards: int = 4,
    d_in: int = 3584,
) -> None:
    output = Path(output_dir)
    activations_path = output / "activations.npy"
    metadata_path = output / "metadata.parquet"
    progress_path = output / "progress.json"
    samples_path = output / "samples.parquet"

    if not samples_path.exists():
        raise FileNotFoundError(f"Missing {samples_path}")
    if not activations_path.exists():
        raise FileNotFoundError(f"Missing {activations_path}")

    n_samples = len(pd.read_parquet(samples_path))
    bank = open_activation_bank(activations_path, d_in=d_in)
    if len(bank) != n_samples:
        raise RuntimeError(f"Activation bank rows {len(bank)} != samples {n_samples}")

    metadata_parts: list[pd.DataFrame] = []
    for shard_id in range(num_shards):
        shard_meta = output / f"metadata_shard_{shard_id}.parquet"
        shard_progress = output / f"progress_shard_{shard_id}.json"
        if not shard_meta.exists():
            raise FileNotFoundError(f"Missing shard metadata: {shard_meta}")
        if not shard_progress.exists():
            raise FileNotFoundError(f"Missing shard progress: {shard_progress}")
        progress = json.loads(shard_progress.read_text())
        if not progress.get("complete"):
            raise RuntimeError(f"Shard {shard_id} not complete: {progress}")
        metadata_parts.append(pd.read_parquet(shard_meta))
        logger.info("Shard %d complete (%d rows)", shard_id, len(metadata_parts[-1]))

    metadata = pd.concat(metadata_parts, ignore_index=True).sort_values("index").reset_index(drop=True)
    if len(metadata) != n_samples:
        raise RuntimeError(f"Merged metadata rows {len(metadata)} != samples {n_samples}")
    if metadata["index"].tolist() != list(range(n_samples)):
        raise RuntimeError("Merged metadata indices are not a contiguous 0..N-1 range")

    metadata.to_parquet(metadata_path, index=False)
    progress_path.write_text(json.dumps({"next_index": n_samples, "complete": True, "num_shards": num_shards}))
    logger.info("Wrote merged metadata to %s (%d rows)", metadata_path, len(metadata))


if __name__ == "__main__":
    fire.Fire(merge_activations)
