#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import fire
from huggingface_hub import hf_hub_download
from tqdm import tqdm


REPO_ID = "MizzenAI/HPDv3"
NUM_SHARDS = 13


def _count_images(images_dir: Path) -> int:
    if not images_dir.exists():
        return 0
    return sum(1 for _ in images_dir.rglob("*.jpg"))


def _extract_shards(shard_paths: list[Path], dest: Path) -> None:
    if not shard_paths:
        return
    dest.mkdir(parents=True, exist_ok=True)
    cat_cmd = ["bash", "-c", f"cat {' '.join(str(p) for p in shard_paths)} | tar -xzf - -C {dest} --skip-old-files"]
    subprocess.run(cat_cmd, check=False)


def download_hpdv3(
    dest: str = "/gpfs/scratch1/shared/scur0077/datasets/HPDv3",
    target_images: int = 150_000,
    delete_shards_after_extract: bool = True,
) -> None:
    dest_path = Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)
    images_dir = dest_path / "images"

    for filename in ("train.json", "test.json"):
        hf_hub_download(
            repo_id=REPO_ID,
            repo_type="dataset",
            filename=filename,
            local_dir=str(dest_path),
        )
        print(f"Downloaded {filename}")

    shard_paths: list[Path] = []
    for shard_idx in tqdm(range(NUM_SHARDS), desc="Downloading image shards"):
        shard_name = f"images.tar.gz.{shard_idx:02d}"
        shard_path = Path(
            hf_hub_download(
                repo_id=REPO_ID,
                repo_type="dataset",
                filename=shard_name,
                local_dir=str(dest_path),
            )
        )
        shard_paths.append(shard_path)
        _extract_shards(shard_paths, dest_path)

        image_count = _count_images(images_dir)
        print(f"After shard {shard_idx:02d}: {image_count} images extracted")
        if image_count >= target_images:
            if delete_shards_after_extract:
                for path in shard_paths:
                    path.unlink(missing_ok=True)
            print(f"Reached target of {target_images} images at {images_dir}")
            return

    final_count = _count_images(images_dir)
    if final_count < target_images:
        print(
            f"Warning: only {final_count} images extracted after all shards "
            f"(target was {target_images})."
        )
    if delete_shards_after_extract:
        for path in shard_paths:
            path.unlink(missing_ok=True)
        cache_dir = dest_path / ".cache"
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)


if __name__ == "__main__":
    fire.Fire(download_hpdv3)
