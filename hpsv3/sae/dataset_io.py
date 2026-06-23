from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _available_images(root: Path) -> set[str]:
    images_dir = root / "images"
    if not images_dir.is_dir():
        return set()
    return {
        f"images/{path.name}"
        for path in images_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    }


def load_hpdv3_samples(
    dataset_root: str | Path,
    target_images: int | None = None,
) -> pd.DataFrame:
    root = Path(dataset_root)
    available = _available_images(root)
    logger.info("Found %d image files under %s", len(available), root / "images")
    if not available:
        return pd.DataFrame()

    records: list[dict] = []

    for json_name in ("train.json", "test.json"):
        json_path = root / json_name
        if not json_path.exists():
            logger.warning("Missing metadata file: %s", json_path)
            continue
        logger.info("Loading %s ...", json_path)
        with json_path.open() as f:
            data = json.load(f)
        logger.info("Scanning %d rows in %s", len(data), json_name)
        matched = 0
        for row_idx, row in enumerate(data):
            if row_idx > 0 and row_idx % 100_000 == 0:
                logger.info("  ... scanned %d / %d rows, matched %d so far", row_idx, len(data), matched)
            for path_key, model_key in (("path1", "model1"), ("path2", "model2")):
                rel_path = row[path_key]
                if rel_path not in available:
                    continue
                matched += 1
                records.append(
                    {
                        "image_path": str(root / rel_path),
                        "prompt": row["prompt"],
                        "model": row.get(model_key),
                        "split": json_name.replace(".json", ""),
                    }
                )
        logger.info("Matched %d image references from %s", matched, json_name)

    df = pd.DataFrame(records).drop_duplicates(subset=["image_path", "prompt"]).reset_index(drop=True)
    logger.info("Unique (image, prompt) pairs: %d", len(df))
    if target_images is not None and len(df) > target_images:
        df = df.sample(n=target_images, random_state=42).reset_index(drop=True)
        logger.info("Subsampled to %d pairs for extraction", len(df))
    return df


def activation_bank_n_rows(path: str | Path, d_in: int = 3584) -> int:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Activation bank not found: {path}")
    row_bytes = d_in * np.dtype(np.float16).itemsize
    n_rows = path.stat().st_size // row_bytes
    if n_rows <= 0 or path.stat().st_size % row_bytes != 0:
        raise ValueError(f"Activation bank has invalid size {path.stat().st_size} for d_in={d_in}: {path}")
    return n_rows


def open_activation_bank(path: str | Path, d_in: int = 3584) -> np.memmap:
    n_rows = activation_bank_n_rows(path, d_in=d_in)
    return np.memmap(path, dtype=np.float16, mode="r", shape=(n_rows, d_in))


def load_activation_metadata(activation_dir: str | Path) -> pd.DataFrame:
    activation_dir = Path(activation_dir)
    samples_path = activation_dir / "samples.parquet"
    n_samples = len(pd.read_parquet(samples_path)) if samples_path.exists() else None

    metadata_path = activation_dir / "metadata.parquet"
    progress_path = activation_dir / "progress.json"
    if metadata_path.exists() and progress_path.exists() and n_samples is not None:
        progress = json.loads(progress_path.read_text())
        metadata = pd.read_parquet(metadata_path)
        if progress.get("complete") and len(metadata) == n_samples and "index" in metadata.columns:
            return metadata.sort_values("index").reset_index(drop=True)

    parts: list[pd.DataFrame] = []
    for shard_meta in sorted(activation_dir.glob("metadata_shard_*.parquet")):
        metadata = pd.read_parquet(shard_meta)
        if "index" not in metadata.columns:
            continue
        parts.append(metadata.loc[~metadata["mu"].isna()])
    if not parts:
        raise FileNotFoundError(f"No extracted activation metadata found under {activation_dir}")
    return pd.concat(parts, ignore_index=True).sort_values("index").reset_index(drop=True)


def load_extracted_indices(activation_dir: str | Path) -> np.ndarray:
    return load_activation_metadata(activation_dir)["index"].astype(np.int64).to_numpy()


def create_activation_bank(path: str | Path, n_rows: int, d_in: int = 3584) -> np.memmap:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Allocating activation bank %s with shape (%d, %d)", path, n_rows, d_in)
    return np.memmap(path, dtype=np.float16, mode="w+", shape=(n_rows, d_in))
