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
from hpsv3.xai.manifest import build_manifest
from hpsv3.xai.sapiens_segments import load_labels as load_sapiens_labels, resize_seg
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


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _segment_image(img, stem, args):
    """Return (labels, part_names_or_None). Uses Sapiens labels if requested and
    present, otherwise SLIC superpixels (also the fallback for non-human images)."""
    if args.segments == "sapiens" and args.sapiens_dir:
        lp = os.path.join(args.sapiens_dir, f"{stem}_sapiens_labels.npy")
        pp = os.path.join(args.sapiens_dir, f"{stem}_parts.json")
        if os.path.exists(lp) and os.path.exists(pp):
            labels, id2name = load_sapiens_labels(stem, args.sapiens_dir)
            if labels.shape != img.shape[:2]:
                labels = resize_seg(labels, img.shape[:2])
            print(f"  [sapiens] {len(set(id2name.values()))} parts: {sorted(set(id2name.values()))}")
            return labels, {int(k): v for k, v in id2name.items()}
        print(f"  [sapiens] no label map for {stem}; falling back to SLIC")
    return make_segments(img, n_segments=args.n_segments, compactness=args.compactness), None


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
    p.add_argument("--segments", choices=["slic", "sapiens"], default="slic",
                   help="Region source: SLIC superpixels or precomputed Sapiens body parts.")
    p.add_argument("--sapiens-dir", default=None,
                   help="Dir with <stem>_sapiens_labels.npy + <stem>_parts.json (Stage 0 output).")
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
    manifest = build_manifest(args.manifest, args.hpdv3_dir, args.num_dataset)
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
        labels, part_names = _segment_image(img, stem, args)
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
                meta = {"model": "hpsv3", "prompt": prompt, "image": image_path,
                        "method": method, "mode": mode, "segments": args.segments}
                if part_names is not None:
                    meta["part_names"] = part_names
                save_result(
                    os.path.join(args.output_dir, f"{tag}.npz"),
                    img, labels, imp, base, meta,
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
