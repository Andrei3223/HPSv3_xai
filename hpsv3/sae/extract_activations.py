from __future__ import annotations

import json
import logging
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import fire
import numpy as np
import pandas as pd
import torch
from tqdm import tqdm

from hpsv3.inference import HPSv3RewardInferencer
from hpsv3.sae.dataset_io import create_activation_bank, load_hpdv3_samples
from hpsv3.sae.prefetch import move_batch_to_device, prepare_batch_cpu
from hpsv3.sae.sharding import ensure_samples_and_bank, shard_range, wait_for_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)


class RewardTokenActivationExtractor:
    def __init__(self, inferencer: HPSv3RewardInferencer):
        self.inferencer = inferencer
        self.model = inferencer.model
        self.processor = inferencer.processor
        self.use_special_tokens = inferencer.use_special_tokens
        self._hidden_states: torch.Tensor | None = None
        self._handle = self.model.rm_head.register_forward_pre_hook(self._capture_hidden)

    def _capture_hidden(self, module, args) -> None:
        self._hidden_states = args[0]

    def close(self) -> None:
        self._handle.remove()

    def prepare_batch_cpu(self, image_paths: list[str], prompts: list[str]) -> dict:
        return prepare_batch_cpu(self.processor, self.use_special_tokens, image_paths, prompts)

    @torch.inference_mode()
    def forward_prepared(self, batch: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        input_ids = batch["input_ids"]
        rewards = self.model(return_dict=True, **batch)["logits"]
        assert self._hidden_states is not None

        hidden = self._hidden_states
        batch_size = input_ids.shape[0]
        reward_token_id = self.model.special_token_ids[0]
        token_mask = input_ids == reward_token_id
        if not token_mask.any():
            raise RuntimeError("Reward token not found in input_ids")

        seq_indices = token_mask.int().argmax(dim=-1)
        batch_indices = torch.arange(batch_size, device=hidden.device)
        reward_hidden = hidden[batch_indices, seq_indices].float().cpu().numpy()
        mu = rewards[:, 0].float().cpu().numpy()
        sigma = rewards[:, 1].float().cpu().numpy()
        return reward_hidden, mu, sigma


def _write_metadata(metadata_path: Path, metadata_rows: list[dict]) -> None:
    pd.DataFrame(metadata_rows).to_parquet(metadata_path, index=False)


def _backfill_metadata_gaps(
    metadata_rows: list[dict],
    shard_samples: pd.DataFrame,
    shard_start: int,
    local_start: int,
) -> list[dict]:
    existing = {int(row["index"]) for row in metadata_rows}
    filled = list(metadata_rows)
    for local_idx in range(local_start):
        global_index = shard_start + local_idx
        if global_index in existing:
            continue
        row = shard_samples.iloc[local_idx].to_dict()
        filled.append(
            {
                **row,
                "index": global_index,
                "mu": float("nan"),
                "sigma": float("nan"),
            }
        )
    filled.sort(key=lambda row: int(row["index"]))
    return filled


def _submit_cpu_batch(
    executor: ThreadPoolExecutor,
    extractor: RewardTokenActivationExtractor,
    image_paths: list[str],
    prompts: list[str],
) -> Future:
    return executor.submit(extractor.prepare_batch_cpu, image_paths, prompts)


def extract_activations(
    dataset_root: str = "/gpfs/scratch1/shared/scur0077/datasets/HPDv3",
    checkpoint_path: str = "/gpfs/scratch1/shared/scur0077/models/HPSv3/HPSv3.safetensors",
    output_dir: str = "/gpfs/scratch1/shared/scur0077/sae/activations",
    config_path: str | None = None,
    batch_size: int = 16,
    device: str = "cuda",
    target_images: int | None = None,
    resume: bool = True,
    shard_id: int = 0,
    num_shards: int = 1,
    prefetch: bool = True,
    metadata_write_every: int = 50,
) -> None:
    log_prefix = f"[shard {shard_id}/{num_shards}]"
    logger.info("%s Starting activation extraction", log_prefix)
    logger.info(
        "%s batch_size=%d prefetch=%s metadata_write_every=%d",
        log_prefix,
        batch_size,
        prefetch,
        metadata_write_every,
    )

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    samples_path = output / "samples.parquet"
    activations_path = output / "activations.npy"
    metadata_path = output / f"metadata_shard_{shard_id}.parquet" if num_shards > 1 else output / "metadata.parquet"
    progress_path = output / f"progress_shard_{shard_id}.json" if num_shards > 1 else output / "progress.json"

    if num_shards > 1:
        if shard_id == 0:
            samples = ensure_samples_and_bank(output, dataset_root, target_images)
            logger.info("%s Prepared samples.parquet and activations.npy (%d rows)", log_prefix, len(samples))
        else:
            wait_for_file(samples_path)
            wait_for_file(activations_path)
            samples = pd.read_parquet(samples_path)
            logger.info("%s Loaded shared sample list (%d rows)", log_prefix, len(samples))
    elif resume and samples_path.exists():
        samples = pd.read_parquet(samples_path)
    else:
        samples = load_hpdv3_samples(dataset_root, target_images=target_images)
        samples.to_parquet(samples_path, index=False)

    if len(samples) == 0:
        raise RuntimeError(f"No extracted images found under {dataset_root}.")

    shard_start, shard_end = shard_range(len(samples), shard_id, num_shards)
    shard_samples = samples.iloc[shard_start:shard_end].reset_index(drop=True)
    logger.info(
        "%s Processing global indices [%d, %d) -> %d samples",
        log_prefix,
        shard_start,
        shard_end,
        len(shard_samples),
    )

    local_start = 0
    if resume and progress_path.exists():
        progress = json.loads(progress_path.read_text())
        local_start = int(progress.get("next_local_index", 0))
        if progress.get("complete"):
            logger.info("%s Shard already complete.", log_prefix)
            return

    if num_shards > 1 and shard_id != 0:
        wait_for_file(activations_path)
    if activations_path.exists() and (local_start > 0 or num_shards > 1):
        bank = np.memmap(
            activations_path,
            dtype=np.float16,
            mode="r+",
            shape=(len(samples), 3584),
        )
    else:
        bank = create_activation_bank(activations_path, len(samples))

    metadata_rows: list[dict] = []
    if resume and metadata_path.exists() and local_start > 0:
        metadata_rows = pd.read_parquet(metadata_path).to_dict(orient="records")
        if len(metadata_rows) < local_start:
            metadata_rows = _backfill_metadata_gaps(metadata_rows, shard_samples, shard_start, local_start)

    logger.info("%s Loading HPSv3 from %s ...", log_prefix, checkpoint_path)
    inferencer = HPSv3RewardInferencer(
        config_path=config_path,
        checkpoint_path=checkpoint_path,
        device=device,
    )
    logger.info("%s HPSv3 loaded on %s", log_prefix, device)
    extractor = RewardTokenActivationExtractor(inferencer)

    batch_starts = list(range(local_start, len(shard_samples), batch_size))
    samples_processed = local_start

    def batch_slice(batch_start: int) -> tuple[list[str], list[str], pd.DataFrame]:
        batch = shard_samples.iloc[batch_start : batch_start + batch_size]
        return batch["image_path"].tolist(), batch["prompt"].tolist(), batch

    try:
        executor = ThreadPoolExecutor(max_workers=1) if prefetch else None
        next_cpu_future: Future | None = None

        for batch_idx, local_batch_start in enumerate(
            tqdm(
                batch_starts,
                desc=f"{log_prefix} extracting",
                file=sys.stdout,
                mininterval=5,
            )
        ):
            image_paths, prompts, batch_df = batch_slice(local_batch_start)

            if next_cpu_future is not None:
                batch_cpu = next_cpu_future.result()
            else:
                batch_cpu = extractor.prepare_batch_cpu(image_paths, prompts)

            next_start = local_batch_start + len(batch_df)
            if prefetch and executor is not None and next_start < len(shard_samples):
                n_paths, n_prompts, _ = batch_slice(next_start)
                next_cpu_future = _submit_cpu_batch(executor, extractor, n_paths, n_prompts)
            else:
                next_cpu_future = None

            batch = move_batch_to_device(batch_cpu, device)
            hidden, mu, sigma = extractor.forward_prepared(batch)

            global_batch_start = shard_start + local_batch_start
            bank[global_batch_start : global_batch_start + len(batch_df)] = hidden.astype(np.float16)

            for row_idx, (_, row) in enumerate(batch_df.iterrows()):
                metadata_rows.append(
                    {
                        **row.to_dict(),
                        "index": global_batch_start + row_idx,
                        "mu": float(mu[row_idx]),
                        "sigma": float(sigma[row_idx]),
                    }
                )

            samples_processed = local_batch_start + len(batch_df)
            bank.flush()
            progress_path.write_text(
                json.dumps(
                    {
                        "next_local_index": samples_processed,
                        "global_start": shard_start,
                        "global_end": shard_end,
                        "complete": samples_processed >= len(shard_samples),
                    }
                )
            )

            if (batch_idx + 1) % metadata_write_every == 0 or samples_processed >= len(shard_samples):
                _write_metadata(metadata_path, metadata_rows)

            if (batch_idx + 1) % 25 == 0:
                logger.info(
                    "%s Progress: %d / %d shard samples",
                    log_prefix,
                    samples_processed,
                    len(shard_samples),
                )
    finally:
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        extractor.close()

    _write_metadata(metadata_path, metadata_rows)
    progress_path.write_text(
        json.dumps(
            {
                "next_local_index": len(shard_samples),
                "global_start": shard_start,
                "global_end": shard_end,
                "complete": True,
            }
        )
    )
    logger.info("%s Finished shard indices [%d, %d)", log_prefix, shard_start, shard_end)


if __name__ == "__main__":
    fire.Fire(extract_activations)
