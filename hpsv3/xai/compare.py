"""
Compare two perturbation-attribution results (e.g. HPSv3 vs EditReward) on the
SAME edited image.

This step needs no reward model and no torch -- it only reads the .npz files
produced by `hpsv3.xai.perturbation --save-npz`. That is what makes it possible
to compare two models whose Python environments conflict
(HPSv3: transformers 4.45.2, EditReward: transformers 4.57.0).

Requirements for a fair comparison:
  * both results were computed on the same image, and
  * with the SAME superpixel labels (use --labels / --save-labels in the
    perturbation CLI so the two importance vectors are index-aligned).

Outputs:
  * a 3-panel figure: heatmap(A), heatmap(B), and their per-superpixel difference
  * Spearman rank correlation between the two attributions (agreement)
  * optional edit-localization: how much of each model's positive attribution
    lands on the actually-edited region (needs --source for the original I_s)

Example:
    python -m hpsv3.xai.compare result_hpsv3.npz result_edit.npz \
        --labels-a hpsv3 --labels-b editreward \
        --source assets/source.png --out compare.png
"""

from __future__ import annotations

import argparse
from typing import Optional

import numpy as np

from .perturbation import (
    load_result,
    importance_heatmap,
    edit_region_fraction,
    localization_score,
)

try:
    from skimage.segmentation import mark_boundaries
except ImportError:  # pragma: no cover
    mark_boundaries = None


def _rankdata(x: np.ndarray) -> np.ndarray:
    """Average ranks (handles ties) -- scipy-free."""
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x), dtype=np.float64)
    ranks[order] = np.arange(1, len(x) + 1)
    # average ties
    _, inv, counts = np.unique(x, return_inverse=True, return_counts=True)
    sums = np.zeros(len(counts))
    np.add.at(sums, inv, ranks)
    return (sums / counts)[inv]


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman rank correlation, numpy-only."""
    ra, rb = _rankdata(a), _rankdata(b)
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    denom = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / denom) if denom > 0 else 0.0


def compare(
    path_a: str,
    path_b: str,
    name_a: str = "A",
    name_b: str = "B",
    source: Optional[str] = None,
    out: str = "compare.png",
    diff_thresh: int = 20,
    edit_thresh: float = 0.25,
) -> dict:
    ra = load_result(path_a)
    rb = load_result(path_b)

    if ra["labels"].shape != rb["labels"].shape:
        raise ValueError("The two results have different image/label shapes.")
    if not np.array_equal(ra["labels"], rb["labels"]):
        raise ValueError(
            "The two results use different superpixel labels and are NOT "
            "comparable. Re-run with a shared --labels map."
        )

    labels = ra["labels"]
    img = ra["img"]
    imp_a, imp_b = ra["importances"], rb["importances"]

    rho = spearman(imp_a, imp_b)
    report = {"spearman": rho, "base_a": ra["base_score"], "base_b": rb["base_score"]}
    print(f"Spearman agreement ({name_a} vs {name_b}): {rho:+.3f}")
    print(f"Base reward {name_a}={ra['base_score']:.3f}  {name_b}={rb['base_score']:.3f}")

    edit_frac = None
    if source is not None:
        src = np.array(__import__("PIL.Image", fromlist=["Image"]).open(source).convert("RGB"))
        edit_frac, _ = edit_region_fraction(src, img, labels, diff_thresh=diff_thresh)
        loc_a = localization_score(imp_a, edit_frac, edit_thresh)
        loc_b = localization_score(imp_b, edit_frac, edit_thresh)
        report["localization_a"] = loc_a
        report["localization_b"] = loc_b
        print(f"Edit-localization {name_a}: edited_mass={loc_a['edited_mass']:.3f}, "
              f"top1_in_edit={loc_a['top1_in_edit']}")
        print(f"Edit-localization {name_b}: edited_mass={loc_b['edited_mass']:.3f}, "
              f"top1_in_edit={loc_b['top1_in_edit']}")

    _plot(img, labels, imp_a, imp_b, name_a, name_b, rho, out)
    print(f"Saved comparison figure -> {out}")
    return report


def _plot(img, labels, imp_a, imp_b, name_a, name_b, rho, out):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    base = mark_boundaries(img, labels) if mark_boundaries else img
    ha = importance_heatmap(labels, imp_a)
    hb = importance_heatmap(labels, imp_b)
    diff = importance_heatmap(labels, _norm(imp_a) - _norm(imp_b))

    vmax = max(np.abs(ha).max(), np.abs(hb).max()) or 1.0
    dvmax = np.abs(diff).max() or 1.0

    fig, axes = plt.subplots(1, 3, figsize=(18, 6))
    for ax, heat, title, vm in (
        (axes[0], ha, name_a, vmax),
        (axes[1], hb, name_b, vmax),
        (axes[2], diff, f"normalized diff ({name_a} - {name_b})", dvmax),
    ):
        ax.imshow(base)
        im = ax.imshow(heat, cmap="coolwarm", vmin=-vm, vmax=vm, alpha=0.5)
        ax.set_title(title)
        ax.axis("off")
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    fig.suptitle(f"Attribution comparison  (Spearman={rho:+.3f})", fontsize=14)
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)


def _norm(x: np.ndarray) -> np.ndarray:
    """Scale to [-1, 1] by max-abs so the two models are on a common scale."""
    m = np.abs(x).max()
    return x / m if m > 0 else x


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("result_a")
    p.add_argument("result_b")
    p.add_argument("--name-a", default="A")
    p.add_argument("--name-b", default="B")
    p.add_argument("--source", default=None, help="Source image I_s for edit-localization.")
    p.add_argument("--diff-thresh", type=int, default=20)
    p.add_argument("--edit-thresh", type=float, default=0.25)
    p.add_argument("--out", default="compare.png")
    args = p.parse_args(argv)
    compare(
        args.result_a, args.result_b, args.name_a, args.name_b,
        source=args.source, out=args.out,
        diff_thresh=args.diff_thresh, edit_thresh=args.edit_thresh,
    )


if __name__ == "__main__":
    main()
