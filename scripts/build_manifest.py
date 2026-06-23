#!/usr/bin/env python
"""
Write a shared (image, prompt) manifest.json so the Sapiens segmentation stage
and the HPSv3 attribution stage operate on the EXACT same images.

Pure stdlib; loads hpsv3/xai/manifest.py by file path so it runs in any venv
(including the Sapiens one, without the hpsv3 package installed).

    python scripts/build_manifest.py --hpdv3-dir /path/HPDv3 --num-dataset 5 --out manifest.json
"""

import argparse
import importlib.util
import json
import os


def _load_manifest_module():
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "..", "hpsv3", "xai", "manifest.py")
    spec = importlib.util.spec_from_file_location("xai_manifest", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hpdv3-dir", default=None)
    ap.add_argument("--num-dataset", type=int, default=5)
    ap.add_argument("--no-assets", action="store_true")
    ap.add_argument("--out", default="manifest.json")
    args = ap.parse_args()

    mani = _load_manifest_module()
    items = mani.build_manifest(
        hpdv3_dir=args.hpdv3_dir,
        num_dataset=args.num_dataset,
        include_assets=not args.no_assets,
    )
    if not items:
        raise SystemExit("No valid (image, prompt) pairs found.")
    json.dump(items, open(args.out, "w"), indent=2)
    print(f"[manifest] wrote {len(items)} pairs -> {args.out}")


if __name__ == "__main__":
    main()
