"""
Run the full HPSv3 perturbation-attribution experiment matrix in one process.

The 7B model is loaded ONCE and reused across every image x method x baseline,
which is the only practical way to run the whole matrix inside a single GPU job.

For each image it produces, under --output-dir:
  <stem>_labels.npy                 superpixel map (reused across methods/modes)
  <stem>_<method>_<mode>.png        heatmap overlay
  <stem>_<method>_<mode>.npz        raw result (for hpsv3.xai.compare later)
  <stem>_<method>_<mode>_faithfulness.png   (for the chosen faithfulness combo)
  summary.csv                       one row per experiment with base reward,
                                    top importances, and faithfulness AUCs
  manifest.json                     exactly which (image, prompt) pairs were run

Image/prompt pairs come from (in priority order):
  1. --manifest file: a JSON list of {"image": ..., "prompt": ...}
  2. --hpdv3-dir: best-effort sampling from the HPDv3 json (test/train/all.json)
  3. the two repo assets (always available) as a fallback
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from typing import List

import numpy as np

from hpsv3.inference import HPSv3RewardInferencer
from hpsv3.xai.perturbation import (
    load_image,
    make_segments,
    make_hpsv3_scorer,
    explain_occlusion,
    explain_lime,
    faithfulness_curves,
    save_explanation,
    save_faithfulness,
    save_result,
)

_FOX_PROMPT = (
    "cute chibi anime cartoon fox, smiling wagging tail with a small cartoon "
    "heart above sticker"
)
# Resolve assets relative to the repo root so they work regardless of the job's
# working directory (this is why the assets were dropped in the first run).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_MANIFEST = [
    {"image": os.path.join(_REPO_ROOT, "assets/example1.png"), "prompt": _FOX_PROMPT},
    {"image": os.path.join(_REPO_ROOT, "assets/example2.png"), "prompt": _FOX_PROMPT},
]


def _sample_from_hpdv3(d: str, n: int) -> List[dict]:
    """Best-effort: pull n (image, prompt) pairs from an HPDv3 json manifest."""
    for name in ("test.json", "train.json", "all.json"):
        p = os.path.join(d, name)
        if not os.path.exists(p):
            continue
        try:
            data = json.load(open(p))
            out = []
            for e in data:
                rel = e.get("path1") or e.get("path")
                if not rel:
                    continue
                img = rel if os.path.isabs(rel) else os.path.join(d, rel)
                if os.path.exists(img) and e.get("prompt"):
                    out.append({"image": img, "prompt": e["prompt"]})
                if len(out) >= n:
                    break
            if out:
                print(f"[manifest] sampled {len(out)} pairs from {p}")
                return out
        except Exception as ex:  # pragma: no cover
            print(f"[manifest] could not parse {p}: {ex}")
    print(f"[manifest] no usable json found in {d}; falling back to assets.")
    return []


def build_manifest(args) -> List[dict]:
    if args.manifest:
        with open(args.manifest) as f:
            items = json.load(f)
        print(f"[manifest] loaded {len(items)} pairs from {args.manifest}")
        return items
    items = list(DEFAULT_MANIFEST)
    if args.hpdv3_dir and args.num_dataset > 0:
        items += _sample_from_hpdv3(args.hpdv3_dir, args.num_dataset)
    # keep only pairs whose image actually exists
    items = [it for it in items if os.path.exists(it["image"])]
    return items


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output-dir", default="results/xai")
    p.add_argument("--manifest", default=None)
    p.add_argument("--hpdv3-dir", default=None)
    p.add_argument("--num-dataset", type=int, default=0)
    p.add_argument("--hpsv3-ckpt", default=None)
    p.add_argument("--hpsv3-config", default=None)
    p.add_argument("--device", default="cuda")
    p.add_argument("--methods", nargs="+", default=["occlusion", "lime"],
                   choices=["occlusion", "lime"])
    p.add_argument("--modes", nargs="+", default=["gray", "mean", "blur", "black"])
    p.add_argument("--n-segments", type=int, default=100)
    p.add_argument("--compactness", type=float, default=10.0)
    p.add_argument("--n-samples", type=int, default=500, help="LIME samples.")
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--faithfulness", action="store_true",
                   help="Run deletion/insertion curves for the selected combos per image.")
    p.add_argument("--faith-methods", nargs="+", default=["occlusion"],
                   choices=["occlusion", "lime"])
    p.add_argument("--faith-modes", nargs="+", default=["gray", "black"])
    args = p.parse_args(argv)

    os.makedirs(args.output_dir, exist_ok=True)
    manifest = build_manifest(args)
    if not manifest:
        raise SystemExit("No valid (image, prompt) pairs to run.")
    json.dump(manifest, open(os.path.join(args.output_dir, "manifest.json"), "w"), indent=2)
    print(f"[run] {len(manifest)} images x {len(args.methods)} methods x "
          f"{len(args.modes)} baselines")

    print("[run] loading HPSv3 (once)...")
    inferencer = HPSv3RewardInferencer(
        device=args.device,
        checkpoint_path=args.hpsv3_ckpt,
        config_path=args.hpsv3_config,
    )

    rows = []
    fieldnames = [
        "image", "prompt", "method", "mode", "base_reward",
        "top1_importance", "mean_abs_importance", "n_superpixels",
        "deletion_auc", "deletion_auc_random",
        "insertion_auc", "insertion_auc_random",
    ]

    for item in manifest:
        image_path, prompt = item["image"], item["prompt"]
        stem = _stem(image_path)
        print(f"\n=== {image_path} ===")
        img = load_image(image_path)
        labels = make_segments(img, n_segments=args.n_segments, compactness=args.compactness)
        np.save(os.path.join(args.output_dir, f"{stem}_labels.npy"), labels)
        n_sp = int(labels.max()) + 1

        for method in args.methods:
            for mode in args.modes:
                tag = f"{stem}_{method}_{mode}"
                score_fn = make_hpsv3_scorer(inferencer, prompt, batch_size=args.batch_size)

                if method == "occlusion":
                    imp, base = explain_occlusion(score_fn, img, labels, mode=mode)
                else:
                    imp, base = explain_lime(
                        score_fn, img, labels, mode=mode,
                        n_samples=args.n_samples, seed=args.seed,
                    )

                save_explanation(
                    img, labels, imp, os.path.join(args.output_dir, f"{tag}.png"),
                    base_score=base, title=f"hpsv3 / {method} / {mode}",
                )
                save_result(
                    os.path.join(args.output_dir, f"{tag}.npz"),
                    img, labels, imp, base,
                    {"model": "hpsv3", "prompt": prompt, "image": image_path,
                     "method": method, "mode": mode},
                )

                row = {
                    "image": image_path, "prompt": prompt, "method": method,
                    "mode": mode, "base_reward": round(base, 4),
                    "top1_importance": round(float(imp.max()), 4),
                    "mean_abs_importance": round(float(np.abs(imp).mean()), 4),
                    "n_superpixels": n_sp,
                    "deletion_auc": "", "deletion_auc_random": "",
                    "insertion_auc": "", "insertion_auc_random": "",
                }

                if args.faithfulness and method in args.faith_methods and mode in args.faith_modes:
                    cur = faithfulness_curves(score_fn, img, labels, imp, mode=mode, seed=args.seed)
                    save_faithfulness(cur, os.path.join(args.output_dir, f"{tag}_faithfulness.png"))
                    row.update(
                        deletion_auc=round(cur["deletion_auc"], 4),
                        deletion_auc_random=round(cur["deletion_auc_random"], 4),
                        insertion_auc=round(cur["insertion_auc"], 4),
                        insertion_auc_random=round(cur["insertion_auc_random"], 4),
                    )

                rows.append(row)
                print(f"  [{method:9s}/{mode:5s}] base={base:.3f} "
                      f"top1={row['top1_importance']:+.3f}")

    with open(os.path.join(args.output_dir, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"\n[done] {len(rows)} experiments -> {args.output_dir}/summary.csv")


if __name__ == "__main__":
    main()
