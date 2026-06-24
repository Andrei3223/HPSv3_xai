"""
Global image-property sensitivity for HPSv3.

Unlike the region-based attribution (which asks *where* the model looks), this asks
*what image properties* the score depends on. We take the whole image and apply a
degradation at increasing strength -- brightness, contrast, colour/saturation,
Gaussian blur, JPEG compression, additive noise -- and watch how the reward moves.

The output is a sensitivity curve per property: reward change vs strength, averaged
over many images. It answers questions like "does HPSv3 punish JPEG artefacts?",
"how dark can an image get before the score drops?", "is it robust to mild blur?".

CLI:
    python -m hpsv3.xai.global_perturbation \
        --hpsv3-ckpt <ckpt> --manifest manifest.json \
        --output-dir results/global_<jobid>

Outputs:
    global_perturbation.csv     image, transform, strength, reward
    agg_global_perturbation.csv  transform, strength, n, mean_delta, se_delta
    fig_global_perturbation.png  one curve panel per transform
"""

from __future__ import annotations

import argparse
import csv
import io
import os
from collections import defaultdict

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter


# --------------------------------------------------------------------------- #
# Transforms: name -> (function(PIL, strength)->PIL, strength grid, baseline value)
# baseline value is the strength that leaves the image (essentially) unchanged.
# --------------------------------------------------------------------------- #
def _brightness(img, f):
    return ImageEnhance.Brightness(img).enhance(f)


def _contrast(img, f):
    return ImageEnhance.Contrast(img).enhance(f)


def _color(img, f):  # saturation: 0 = greyscale, 1 = original, >1 = more vivid
    return ImageEnhance.Color(img).enhance(f)


def _blur(img, s):
    return img.filter(ImageFilter.GaussianBlur(radius=s)) if s > 0 else img


def _jpeg(img, q):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=int(q))
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def _noise(img, std):
    if std <= 0:
        return img
    a = np.asarray(img).astype(np.float32)
    a = a + np.random.default_rng(0).normal(0, std, a.shape)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


TRANSFORMS = {
    "brightness": (_brightness, [0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6], 1.0),
    "contrast":   (_contrast,   [0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6], 1.0),
    "color":      (_color,      [0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0], 1.0),
    "blur":       (_blur,       [0, 1, 2, 3, 4, 6, 8], 0),
    "jpeg":       (_jpeg,       [5, 10, 20, 30, 50, 70, 90], 90),  # lower q = more artefacts
    "noise":      (_noise,      [0, 5, 10, 20, 30, 40], 0),
}


def apply_transform(img: Image.Image, name: str, strength) -> Image.Image:
    return TRANSFORMS[name][0](img, strength)


# --------------------------------------------------------------------------- #
# Sweeps
# --------------------------------------------------------------------------- #
def sweep_image(score_fn, img: Image.Image, transforms) -> dict:
    """Return {transform: {strength: reward}} for one image (one model batch each)."""
    out = {}
    for name in transforms:
        _, grid, _ = TRANSFORMS[name]
        variants = [apply_transform(img, name, s) for s in grid]
        rewards = score_fn(variants)
        out[name] = {s: float(r) for s, r in zip(grid, rewards)}
    return out


def aggregate(rows, transforms):
    """rows: list of {image, transform, strength, reward}. Returns agg rows with
    per-image delta relative to that transform's baseline strength, then mean/SE."""
    # group rewards by (image, transform, strength)
    by = defaultdict(dict)
    for r in rows:
        by[(r["image"], r["transform"])][float(r["strength"])] = float(r["reward"])
    deltas = defaultdict(lambda: defaultdict(list))  # transform -> strength -> [delta]
    for (image, tname), d in by.items():
        base_val = TRANSFORMS[tname][2]
        if base_val not in d:
            continue
        base = d[base_val]
        for s, rew in d.items():
            deltas[tname][s].append(rew - base)
    agg = []
    for tname in transforms:
        for s in TRANSFORMS[tname][1]:
            vals = np.array(deltas[tname].get(s, []))
            if len(vals) == 0:
                continue
            se = float(vals.std(ddof=1) / np.sqrt(len(vals))) if len(vals) > 1 else 0.0
            agg.append({"transform": tname, "strength": s, "n": len(vals),
                        "mean_delta": round(float(vals.mean()), 4), "se_delta": round(se, 4)})
    return agg


def plot_curves(agg, transforms, out_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = len(transforms)
    cols = 3
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 4 * rows))
    axes = np.array(axes).reshape(-1)
    for ax, tname in zip(axes, transforms):
        pts = [a for a in agg if a["transform"] == tname]
        xs = [a["strength"] for a in pts]
        ys = [a["mean_delta"] for a in pts]
        es = [a["se_delta"] for a in pts]
        ax.errorbar(xs, ys, yerr=es, marker="o", capsize=3)
        ax.axhline(0, color="k", lw=0.5)
        ax.axvline(TRANSFORMS[tname][2], color="C7", ls=":", lw=1, label="original")
        ax.set_title(tname)
        ax.set_xlabel("strength")
        ax.set_ylabel("Δ reward vs original")
        ax.legend(fontsize=8)
    for ax in axes[len(transforms):]:
        ax.axis("off")
    fig.suptitle("HPSv3 sensitivity to global image degradations", fontsize=14)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# --------------------------------------------------------------------------- #
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output-dir", default="results/global")
    p.add_argument("--manifest", default=None)
    p.add_argument("--hpdv3-dir", default=None)
    p.add_argument("--num-dataset", type=int, default=0)
    p.add_argument("--hpsv3-ckpt", default=None)
    p.add_argument("--hpsv3-config", default=None)
    p.add_argument("--device", default="cuda")
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--transforms", nargs="+", default=list(TRANSFORMS.keys()),
                   choices=list(TRANSFORMS.keys()))
    args = p.parse_args(argv)

    from hpsv3.inference import HPSv3RewardInferencer
    from hpsv3.xai.manifest import build_manifest
    from hpsv3.xai.perturbation import make_hpsv3_scorer

    os.makedirs(args.output_dir, exist_ok=True)
    manifest = build_manifest(args.manifest, args.hpdv3_dir, args.num_dataset)
    if not manifest:
        raise SystemExit("No valid (image, prompt) pairs.")
    print(f"[global] {len(manifest)} images x {len(args.transforms)} transforms")

    print("[global] loading HPSv3 (once)...")
    inferencer = HPSv3RewardInferencer(device=args.device, checkpoint_path=args.hpsv3_ckpt,
                                       config_path=args.hpsv3_config)

    rows = []
    for item in manifest:
        img = Image.open(item["image"]).convert("RGB")
        score_fn = make_hpsv3_scorer(inferencer, item["prompt"], batch_size=args.batch_size)
        swept = sweep_image(score_fn, img, args.transforms)
        for tname, d in swept.items():
            for s, rew in d.items():
                rows.append({"image": item["image"], "transform": tname, "strength": s, "reward": round(rew, 4)})
        print(f"  done {os.path.basename(item['image'])}")

    with open(os.path.join(args.output_dir, "global_perturbation.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["image", "transform", "strength", "reward"])
        w.writeheader(); w.writerows(rows)

    agg = aggregate(rows, args.transforms)
    with open(os.path.join(args.output_dir, "agg_global_perturbation.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["transform", "strength", "n", "mean_delta", "se_delta"])
        w.writeheader(); w.writerows(agg)
    plot_curves(agg, args.transforms, os.path.join(args.output_dir, "fig_global_perturbation.png"))

    print(f"[global] done -> {args.output_dir}/fig_global_perturbation.png")


if __name__ == "__main__":
    main()
