"""
Perturbation-based feature attribution for image-reward models.

Goal: explain *why* a reward model scores an image the way it does, by perturbing
regions (superpixels) of the image and measuring how much the reward DROPS. A
large drop when a region is removed means that region was driving the score up.

This module is **model-agnostic**. The attribution routines take a `score_fn`
that maps a list of PIL images to a 1-D array of scalar rewards. Two scorer
builders are provided:

  * HPSv3      (text-to-image generation):   reward(prompts, image_paths)
  * EditReward (instruction-guided editing): reward(prompts, image_src, image_paths)
    -- here we hold the source image I_s and instruction P fixed and perturb the
       EDITED image I_e.

Both models reuse the same uncertainty-aware (mu, sigma) ranknet design, so in
both cases we attribute the overall mean (mu).

Convention everywhere: positive importance -> region pushes the reward UP.

Because HPSv3 (transformers==4.45.2) and EditReward (transformers==4.57.0) cannot
share one Python environment, the intended workflow is:

  1. In the HPSv3 env:      run --model hpsv3      --save-npz result_hpsv3.npz
  2. In the EditReward env:  run --model editreward --save-npz result_edit.npz
     (reuse the SAME superpixel labels via --labels so the two are comparable)
  3. In any env:             python -m hpsv3.xai.compare result_hpsv3.npz result_edit.npz

See hpsv3/xai/compare.py for step 3.
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

from skimage.segmentation import slic, mark_boundaries

BASELINE_MODES = ("gray", "mean", "blur", "black")

# np.trapz was deprecated in NumPy 2.0 and removed in later 2.x; np.trapezoid is
# the replacement. Pick whichever the installed NumPy provides.
_trapz = getattr(np, "trapezoid", getattr(np, "trapz", None))

ScoreFn = Callable[[Sequence[Image.Image]], np.ndarray]


# --------------------------------------------------------------------------- #
# Image / segmentation helpers
# --------------------------------------------------------------------------- #
def load_image(path: str) -> np.ndarray:
    """Load an image as an HxWx3 uint8 RGB array."""
    return np.array(Image.open(path).convert("RGB"))


def make_segments(
    img: np.ndarray, n_segments: int = 100, compactness: float = 10.0
) -> np.ndarray:
    """Segment an image into superpixels. Returns an HxW int label map (0..K-1)."""
    labels = slic(
        img,
        n_segments=n_segments,
        compactness=compactness,
        start_label=0,
        channel_axis=-1,
    )
    return labels.astype(np.int64)


def _per_superpixel_mean(img: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """Build an image where every superpixel is filled with its own mean color."""
    out = np.zeros_like(img)
    for sp in np.unique(labels):
        mask = labels == sp
        out[mask] = img[mask].reshape(-1, 3).mean(axis=0).round().astype(img.dtype)
    return out


def build_baseline(
    img: np.ndarray,
    labels: np.ndarray,
    mode: str = "gray",
    blur_sigma: float = 15.0,
    fill_value: int = 0,
) -> np.ndarray:
    """
    Build the full "baseline" image used to fill perturbed (off) regions.

    gray  : luminance replicated to 3 channels (removes color, keeps structure)
    mean  : each superpixel filled with its mean color (removes texture/detail)
    blur  : heavily blurred image (removes high-frequency detail)
    black : constant fill (classic hard occlusion); use fill_value (0..255)
    """
    if mode == "gray":
        gray = (
            0.299 * img[..., 0] + 0.587 * img[..., 1] + 0.114 * img[..., 2]
        ).round().astype(img.dtype)
        return np.stack([gray, gray, gray], axis=-1)
    if mode == "mean":
        return _per_superpixel_mean(img, labels)
    if mode == "blur":
        if cv2 is None:
            raise ImportError("opencv-python is required for blur baseline.")
        return cv2.GaussianBlur(img, ksize=(0, 0), sigmaX=blur_sigma, sigmaY=blur_sigma)
    if mode == "black":
        return np.full_like(img, fill_value)
    raise ValueError(f"Unknown baseline mode {mode!r}; choose from {BASELINE_MODES}.")


def perturb(
    img: np.ndarray, labels: np.ndarray, on: np.ndarray, baseline: np.ndarray
) -> Image.Image:
    """
    Compose one perturbed image.

    `on` is a boolean array of length n_superpixels: True keeps the original
    pixels, False replaces them with the baseline.
    """
    off_mask = ~on[labels]  # HxW, True where the superpixel is removed
    out = np.where(off_mask[..., None], baseline, img)
    return Image.fromarray(out.astype(np.uint8))


# --------------------------------------------------------------------------- #
# Scorer builders (the only model-specific code)
# --------------------------------------------------------------------------- #
def _materialize(images: Sequence[Image.Image], temp_dir: str) -> List[str]:
    """Write PIL images to temp_dir, return their paths (caller cleans up)."""
    os.makedirs(temp_dir, exist_ok=True)
    paths = []
    for im in images:
        p = os.path.join(temp_dir, f"{uuid.uuid4().hex}.png")
        im.save(p)
        paths.append(p)
    return paths


def _looped_scorer(
    reward_call: Callable[[List], list],
    extract: Callable[[object], float],
    batch_size: int = 8,
    temp_dir: Optional[str] = None,
) -> ScoreFn:
    """
    Wrap a per-chunk reward call into a score_fn over arbitrarily long image lists.

    reward_call(items) -> list of per-image reward objects, where `items` are
    either PIL images or, if temp_dir is set, file paths. `extract(obj) -> float`
    pulls the scalar mu out of one reward object.
    """

    def score_fn(images: Sequence[Image.Image]) -> np.ndarray:
        out: List[float] = []
        for start in range(0, len(images), batch_size):
            chunk = list(images[start : start + batch_size])
            if temp_dir is not None:
                paths = _materialize(chunk, temp_dir)
                try:
                    rewards = reward_call(paths)
                finally:
                    for p in paths:
                        os.path.exists(p) and os.remove(p)
            else:
                rewards = reward_call(chunk)
            out.extend(extract(r) for r in rewards)
        return np.asarray(out, dtype=np.float64)

    return score_fn


def make_hpsv3_scorer(
    inferencer, prompt: str, batch_size: int = 8, temp_dir: Optional[str] = None
) -> ScoreFn:
    """score_fn for HPSv3: reward(prompts, image_paths) -> [mu, sigma] per image."""

    def reward_call(items):
        return inferencer.reward(prompts=[prompt] * len(items), image_paths=items)

    return _looped_scorer(reward_call, _extract_mu, batch_size, temp_dir)


def make_editreward_scorer(
    inferencer,
    instruction: str,
    source_image: str,
    batch_size: int = 8,
    temp_dir: Optional[str] = None,
    extract: Optional[Callable[[object], float]] = None,
) -> ScoreFn:
    """
    score_fn for EditReward: the source image I_s and instruction P are fixed; we
    perturb the edited image I_e (passed in `images`).

        reward(prompts=[P]*n, image_src=[I_s]*n, image_paths=[I_e perturbed])

    `extract` pulls the overall mu out of one reward object. The default handles
    common shapes (see _extract_overall); override it once you've confirmed the
    exact output shape of your EditReward build (print one reward to check).
    """

    def reward_call(items):
        return inferencer.reward(
            prompts=[instruction] * len(items),
            image_src=[source_image] * len(items),
            image_paths=items,
        )

    return _looped_scorer(reward_call, extract or _extract_overall, batch_size, temp_dir)


def _extract_mu(reward) -> float:
    """HPSv3: reward is a tensor [mu, sigma] (or [mu, sigma]-like); take mu."""
    return float(reward[0].item() if hasattr(reward[0], "item") else reward[0])


def _extract_overall(reward) -> float:
    """
    EditReward: extract the overall mean (mu).

    Defensive across builds:
      * scalar / length-2 [mu, sigma]      -> reward[0]
      * 2-D [D, 2] (mu, sigma per dim)     -> balanced mean over dims of column 0
    Confirm your build's shape once and, if needed, pass a custom `extract`.
    """
    arr = reward.detach().cpu().numpy() if hasattr(reward, "detach") else np.asarray(reward)
    arr = np.atleast_1d(arr)
    if arr.ndim == 1:
        return float(arr[0])  # [mu, sigma] -> mu
    if arr.ndim == 2 and arr.shape[1] == 2:
        return float(arr[:, 0].mean())  # balanced mean of per-dim mu
    return float(arr.flatten()[0])


# --------------------------------------------------------------------------- #
# Attribution methods (model-agnostic: they only need a score_fn)
# --------------------------------------------------------------------------- #
def explain_occlusion(
    score_fn: ScoreFn,
    img: np.ndarray,
    labels: np.ndarray,
    mode: str = "gray",
    **baseline_kwargs,
) -> Tuple[np.ndarray, float]:
    """
    Occlusion attribution: remove one superpixel at a time.

        importance[i] = score(original) - score(image with superpixel i removed)

    Returns (importances [n_superpixels], base_score).
    """
    n_sp = int(labels.max()) + 1
    baseline = build_baseline(img, labels, mode=mode, **baseline_kwargs)

    base_score = float(score_fn([Image.fromarray(img)])[0])

    perturbed = []
    for i in range(n_sp):
        on = np.ones(n_sp, dtype=bool)
        on[i] = False
        perturbed.append(perturb(img, labels, on, baseline))

    occluded_scores = score_fn(perturbed)
    importances = base_score - occluded_scores
    return importances, base_score


def explain_lime(
    score_fn: ScoreFn,
    img: np.ndarray,
    labels: np.ndarray,
    mode: str = "gray",
    n_samples: int = 1000,
    p_on: float = 0.5,
    kernel_width: float = 0.25,
    seed: Optional[int] = 0,
    **baseline_kwargs,
) -> Tuple[np.ndarray, float]:
    """
    LIME attribution: sample random binary superpixel masks, score each perturbed
    image, then fit a locality-weighted linear surrogate. The surrogate's
    coefficients are the per-superpixel importances.

    Returns (importances [n_superpixels], base_score).
    """
    rng = np.random.default_rng(seed)
    n_sp = int(labels.max()) + 1
    baseline = build_baseline(img, labels, mode=mode, **baseline_kwargs)

    base_score = float(score_fn([Image.fromarray(img)])[0])

    # Z[s, i] == 1 means superpixel i is present (original) in sample s.
    Z = (rng.random((n_samples, n_sp)) < p_on).astype(np.float64)

    perturbed = [perturb(img, labels, Z[s].astype(bool), baseline) for s in range(n_samples)]
    y = score_fn(perturbed)

    # Locality weights: samples closer to the all-present image weigh more.
    frac_on = Z.mean(axis=1)
    distance = 1.0 - frac_on
    weights = np.exp(-(distance ** 2) / (kernel_width ** 2))

    # Weighted least squares: [Z | 1] @ beta ~= y, weighted by `weights`.
    A = np.concatenate([Z, np.ones((n_samples, 1))], axis=1)
    sw = np.sqrt(weights)[:, None]
    beta, *_ = np.linalg.lstsq(A * sw, y * sw[:, 0], rcond=None)
    importances = beta[:-1]  # drop the intercept
    return importances, base_score


# --------------------------------------------------------------------------- #
# Faithfulness evaluation (deletion / insertion)
# --------------------------------------------------------------------------- #
def faithfulness_curves(
    score_fn: ScoreFn,
    img: np.ndarray,
    labels: np.ndarray,
    importances: np.ndarray,
    mode: str = "gray",
    seed: Optional[int] = 0,
    **baseline_kwargs,
) -> dict:
    """
    Deletion and insertion curves to validate the explanation.

    Deletion : start from the full image and progressively REMOVE superpixels,
               most-important-first. Faithful => reward drops fast (low AUC).
    Insertion: start from the all-baseline image and progressively ADD
               superpixels, most-important-first. Faithful => reward rises fast
               (high AUC). A random ordering is included as reference.
    """
    rng = np.random.default_rng(seed)
    n_sp = int(labels.max()) + 1
    baseline = build_baseline(img, labels, mode=mode, **baseline_kwargs)

    order = np.argsort(importances)[::-1]  # most important first
    random_order = rng.permutation(n_sp)
    fractions = np.arange(0, n_sp + 1) / n_sp

    def _curve(ranking: np.ndarray, deletion: bool) -> np.ndarray:
        images = []
        for k in range(n_sp + 1):
            on = np.ones(n_sp, dtype=bool) if deletion else np.zeros(n_sp, dtype=bool)
            changed = ranking[:k]
            on[changed] = False if deletion else True
            images.append(perturb(img, labels, on, baseline))
        return score_fn(images)

    del_imp = _curve(order, deletion=True)
    del_rnd = _curve(random_order, deletion=True)
    ins_imp = _curve(order, deletion=False)
    ins_rnd = _curve(random_order, deletion=False)

    return {
        "fractions": fractions,
        "deletion_importance": del_imp,
        "deletion_random": del_rnd,
        "insertion_importance": ins_imp,
        "insertion_random": ins_rnd,
        "deletion_auc": float(_trapz(del_imp, fractions)),
        "deletion_auc_random": float(_trapz(del_rnd, fractions)),
        "insertion_auc": float(_trapz(ins_imp, fractions)),
        "insertion_auc_random": float(_trapz(ins_rnd, fractions)),
    }


# --------------------------------------------------------------------------- #
# Edit-region localization (EditReward-specific evaluation)
# --------------------------------------------------------------------------- #
def edit_region_fraction(
    source_img: np.ndarray,
    edited_img: np.ndarray,
    labels: np.ndarray,
    diff_thresh: int = 20,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Estimate which superpixels were actually changed by the edit.

    Resizes the source to the edited image's size, computes a per-pixel change
    mask |I_e - I_s| > diff_thresh, then the fraction changed inside each
    superpixel. Returns (frac_changed [n_superpixels], changed_mask [HxW bool]).
    """
    h, w = edited_img.shape[:2]
    src = np.array(Image.fromarray(source_img).resize((w, h), Image.BILINEAR))
    diff = np.abs(src.astype(np.int32) - edited_img.astype(np.int32)).mean(axis=2)
    changed = diff > diff_thresh

    n_sp = int(labels.max()) + 1
    frac = np.zeros(n_sp, dtype=np.float64)
    for sp in range(n_sp):
        m = labels == sp
        frac[sp] = changed[m].mean() if m.any() else 0.0
    return frac, changed


def localization_score(
    importances: np.ndarray, edit_frac: np.ndarray, edit_thresh: float = 0.25
) -> dict:
    """
    How much of the *positive* attribution falls on the actually-edited region.

    edited_mass : sum of positive importance over edited superpixels / total
                  positive importance (1.0 == all credit on the edit).
    top1_in_edit: is the single most-important superpixel inside the edit region?
    """
    edited = edit_frac >= edit_thresh
    pos = np.clip(importances, 0, None)
    total = pos.sum()
    edited_mass = float(pos[edited].sum() / total) if total > 0 else 0.0
    top1 = int(np.argmax(importances))
    return {
        "edited_mass": edited_mass,
        "top1_in_edit": bool(edited[top1]),
        "n_edited_superpixels": int(edited.sum()),
    }


# --------------------------------------------------------------------------- #
# Persistence (decouples per-model runs from the comparison step)
# --------------------------------------------------------------------------- #
def save_result(
    path: str,
    img: np.ndarray,
    labels: np.ndarray,
    importances: np.ndarray,
    base_score: float,
    meta: Optional[dict] = None,
) -> None:
    """Save everything needed to reproduce the heatmap and compare models."""
    np.savez_compressed(
        path,
        img=img,
        labels=labels,
        importances=importances,
        base_score=np.float64(base_score),
        meta=json.dumps(meta or {}),
    )


def load_result(path: str) -> dict:
    d = np.load(path, allow_pickle=False)
    return {
        "img": d["img"],
        "labels": d["labels"],
        "importances": d["importances"],
        "base_score": float(d["base_score"]),
        "meta": json.loads(str(d["meta"])),
    }


# --------------------------------------------------------------------------- #
# Visualization
# --------------------------------------------------------------------------- #
def importance_heatmap(labels: np.ndarray, importances: np.ndarray) -> np.ndarray:
    """Map per-superpixel importances back to a per-pixel HxW float map."""
    return importances[labels]


def save_explanation(
    img: np.ndarray,
    labels: np.ndarray,
    importances: np.ndarray,
    out_path: str,
    base_score: Optional[float] = None,
    title: Optional[str] = None,
) -> None:
    """Save a heatmap overlay (red = pushes reward up, blue = down)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    heat = importance_heatmap(labels, importances)
    vmax = float(np.abs(heat).max()) or 1.0  # symmetric, diverging around 0

    fig, ax = plt.subplots(figsize=(7, 7))
    ax.imshow(mark_boundaries(img, labels))
    im = ax.imshow(heat, cmap="coolwarm", vmin=-vmax, vmax=vmax, alpha=0.5)
    ax.axis("off")
    ttl = title or "Perturbation attribution"
    if base_score is not None:
        ttl += f"  (base reward={base_score:.3f})"
    ax.set_title(ttl)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="importance (reward delta)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def save_faithfulness(curves: dict, out_path: str) -> None:
    """Save deletion/insertion curves as a two-panel figure."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    f = curves["fractions"]
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))

    axes[0].plot(f, curves["deletion_importance"], label="by importance", color="C3")
    axes[0].plot(f, curves["deletion_random"], "--", label="random", color="C7")
    axes[0].set_title(
        f"Deletion (lower AUC = better)\n"
        f"AUC={curves['deletion_auc']:.3f} vs random {curves['deletion_auc_random']:.3f}"
    )
    axes[0].set_xlabel("fraction of superpixels removed")
    axes[0].set_ylabel("reward")
    axes[0].legend()

    axes[1].plot(f, curves["insertion_importance"], label="by importance", color="C2")
    axes[1].plot(f, curves["insertion_random"], "--", label="random", color="C7")
    axes[1].set_title(
        f"Insertion (higher AUC = better)\n"
        f"AUC={curves['insertion_auc']:.3f} vs random {curves['insertion_auc_random']:.3f}"
    )
    axes[1].set_xlabel("fraction of superpixels added")
    axes[1].set_ylabel("reward")
    axes[1].legend()

    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Model loading + high-level wrapper
# --------------------------------------------------------------------------- #
def build_scorer(args, img_for_temp: bool = False) -> Tuple[ScoreFn, dict]:
    """Load the requested reward model and return (score_fn, meta)."""
    temp_dir = args.temp_dir if args.temp_files else None

    if args.model == "hpsv3":
        from hpsv3.inference import HPSv3RewardInferencer

        inferencer = HPSv3RewardInferencer(device=args.device)
        if not args.prompt:
            raise ValueError("--prompt is required for HPSv3.")
        score_fn = make_hpsv3_scorer(inferencer, args.prompt, args.batch_size, temp_dir)
        meta = {"model": "hpsv3", "prompt": args.prompt}

    elif args.model == "editreward":
        from EditReward import EditRewardInferencer  # separate package/env

        if not (args.source and args.instruction and args.editreward_config and args.editreward_ckpt):
            raise ValueError(
                "EditReward needs --source, --instruction, --editreward-config, --editreward-ckpt."
            )
        inferencer = EditRewardInferencer(
            config_path=args.editreward_config,
            checkpoint_path=args.editreward_ckpt,
            device=args.device,
            reward_dim=args.editreward_reward_dim,
            rm_head_type=args.editreward_head,
        )
        score_fn = make_editreward_scorer(
            inferencer, args.instruction, args.source, args.batch_size, temp_dir
        )
        meta = {
            "model": "editreward",
            "instruction": args.instruction,
            "source": args.source,
        }
    else:
        raise ValueError(f"Unknown model {args.model!r}.")

    meta.update({"method": args.method, "mode": args.mode, "image": args.image})
    return score_fn, meta


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", choices=["hpsv3", "editreward"], default="hpsv3")
    p.add_argument("--image", required=True, help="Image whose regions are perturbed (I_e for editing).")
    p.add_argument("--prompt", default=None, help="HPSv3: text prompt to score against.")
    # EditReward-specific
    p.add_argument("--source", default=None, help="EditReward: source image I_s path.")
    p.add_argument("--instruction", default=None, help="EditReward: edit instruction P.")
    p.add_argument("--editreward-config", default=None)
    p.add_argument("--editreward-ckpt", default=None)
    p.add_argument("--editreward-reward-dim", default="overall_detail")
    p.add_argument("--editreward-head", default="ranknet_multi_head")
    # attribution
    p.add_argument("--method", choices=["occlusion", "lime"], default="occlusion")
    p.add_argument("--mode", choices=list(BASELINE_MODES), default="gray")
    p.add_argument("--n-segments", type=int, default=100)
    p.add_argument("--compactness", type=float, default=10.0)
    p.add_argument("--labels", default=None, help="Reuse a saved labels .npy (keeps models comparable).")
    p.add_argument("--n-samples", type=int, default=1000, help="LIME only.")
    p.add_argument("--p-on", type=float, default=0.5, help="LIME only.")
    p.add_argument("--kernel-width", type=float, default=0.25, help="LIME only.")
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--device", default="cuda")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--blur-sigma", type=float, default=15.0)
    p.add_argument("--fill-value", type=int, default=0)
    p.add_argument("--temp-files", action="store_true",
                   help="Write perturbed images to disk instead of passing PIL objects.")
    p.add_argument("--temp-dir", default="temp_xai")
    p.add_argument("--faithfulness", action="store_true")
    p.add_argument("--out", default="attribution.png")
    p.add_argument("--save-npz", default=None, help="Save raw result for cross-model comparison.")
    p.add_argument("--save-labels", default=None, help="Save the superpixel labels .npy for reuse.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = _build_arg_parser().parse_args(argv)

    img = load_image(args.image)
    if args.labels:
        labels = np.load(args.labels)
        assert labels.shape == img.shape[:2], "saved labels don't match image size."
    else:
        labels = make_segments(img, n_segments=args.n_segments, compactness=args.compactness)
    if args.save_labels:
        np.save(args.save_labels, labels)

    score_fn, meta = build_scorer(args)
    baseline_kwargs = dict(blur_sigma=args.blur_sigma, fill_value=args.fill_value)

    if args.method == "occlusion":
        importances, base = explain_occlusion(
            score_fn, img, labels, mode=args.mode, **baseline_kwargs
        )
    else:
        importances, base = explain_lime(
            score_fn, img, labels, mode=args.mode, n_samples=args.n_samples,
            p_on=args.p_on, kernel_width=args.kernel_width, seed=args.seed,
            **baseline_kwargs,
        )

    print(f"[{args.model}] base reward (mu): {base:.4f}")
    top = np.argsort(importances)[::-1][:5]
    print("Top-5 superpixels driving the reward UP:")
    for rank, sp in enumerate(top, 1):
        print(f"  {rank}. superpixel {sp:>4d}  importance={importances[sp]:+.4f}")

    save_explanation(img, labels, importances, args.out, base_score=base,
                     title=f"{args.model} / {args.method} / {args.mode}")
    print(f"Saved heatmap -> {args.out}")

    if args.save_npz:
        save_result(args.save_npz, img, labels, importances, base, meta)
        print(f"Saved result -> {args.save_npz}")

    if args.faithfulness:
        curves = faithfulness_curves(
            score_fn, img, labels, importances, mode=args.mode, seed=args.seed,
            **baseline_kwargs,
        )
        fpath = args.out.rsplit(".", 1)[0] + "_faithfulness.png"
        save_faithfulness(curves, fpath)
        print(
            f"Deletion AUC {curves['deletion_auc']:.3f} (random {curves['deletion_auc_random']:.3f}); "
            f"Insertion AUC {curves['insertion_auc']:.3f} (random {curves['insertion_auc_random']:.3f})"
        )
        print(f"Saved faithfulness curves -> {fpath}")


if __name__ == "__main__":
    main()
