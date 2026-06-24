"""Correctness validation for the HPSv3 saliency methods.

Runs four checks and prints PASS/FAIL with the underlying numbers:

A. Differentiable-preprocessing parity -- the differentiable patchifier
   (``preprocess_tensor``) must match the real ``preprocess`` path; otherwise every
   gradient is taken w.r.t. the wrong function. Foundation of all three methods.
B. Integrated-Gradients completeness axiom -- sum of signed IG attributions must
   equal ``mu(x) - mu(baseline)``. The standard IG correctness test.
C. Vanilla-gradient finite-difference -- the autograd input gradient must match a
   central finite-difference directional derivative.
D. Grad-CAM spatial ordering -- the merged-token -> (row, col) reshape used by
   Grad-CAM must place each token at the correct image location. Validated
   deterministically on the patchifier, plus a model-based occlusion corroboration.

Usage:
    python -m hpsv3.saliency.validate \\
        --checkpoint_path=/gpfs/scratch1/shared/scur0077/models/HPSv3/HPSv3.safetensors
"""

import numpy as np
import fire
import torch
from PIL import Image

from hpsv3.saliency.gradient_saliency import (
    DEFAULT_IMAGES,
    _DEFAULT_PROMPT,
    _TARGET_COLUMN,
    SaliencyMapper,
)


def _mu(mapper, batch, img01, col):
    with torch.no_grad():
        logits = mapper._logits_from_leaf(batch, img01)
    return float(logits[0, col].detach().cpu())


def _check_preprocess_parity(mapper, batch, img01, tol=0.02):
    """Compare the differentiable pixel_values (as used by _logits_from_leaf) against
    the real model input produced by the standard prepare_batch path."""
    print("\n[A] Differentiable preprocessing parity (vs prepare_batch input)")
    ref = batch["pixel_values"].float()
    diff = mapper.image_processor.preprocess_tensor(
        img01 * 255.0, do_resize=False
    )["pixel_values"].float().to(ref.device)
    if diff.shape != ref.shape:
        print(f"    FAIL: shape mismatch diff={tuple(diff.shape)} ref={tuple(ref.shape)}")
        return False
    mad = (diff - ref).abs().mean().item()
    denom = ref.abs().mean().item()
    print(f"    mean|diff-ref| = {mad:.5f}  (rel {mad / denom:.5f})  tol={tol}")
    ok = mad < tol
    print("    " + ("PASS" if ok else "FAIL"))
    return ok


def _check_ig_completeness(mapper, batch, img01, col, ig_steps=128, tol=0.1):
    print(f"\n[B] Integrated-Gradients completeness ({ig_steps} steps)")
    baseline = torch.zeros_like(img01)
    x = img01.detach()
    total_grad = torch.zeros_like(x)
    with torch.enable_grad():
        for i in range(ig_steps):
            alpha = (i + 0.5) / ig_steps
            interp = (baseline + alpha * (x - baseline)).requires_grad_(True)
            logits = mapper._logits_from_leaf(batch, interp)
            logits[0, col].backward()
            total_grad += interp.grad.detach()
    avg_grad = total_grad / ig_steps
    ig_sum = ((x - baseline) * avg_grad).sum().item()  # signed, all dims

    f_x = _mu(mapper, batch, x, col)
    f_base = _mu(mapper, batch, baseline, col)
    delta = f_x - f_base
    rel = abs(ig_sum - delta) / (abs(delta) + 1e-6)
    print(f"    sum(IG)        = {ig_sum:.4f}")
    print(f"    mu(x)-mu(base) = {delta:.4f}   (mu(x)={f_x:.4f}, mu(base)={f_base:.4f})")
    print(f"    relative error = {rel:.4f}  tol={tol}")
    ok = rel < tol
    print("    " + ("PASS" if ok else "FAIL"))
    return ok


def _check_vanilla_finite_diff(mapper, batch, img01, col, eps=0.1, tol=0.15):
    """Central finite difference along the steepest (gradient) direction.

    Uses v = g/||g|| so the directional derivative is ||g|| (the largest possible
    signal) and a large-ish eps, because mu is computed in bf16 (abs precision
    ~0.04 at mu~11): a small step would put the mu change below bf16 noise.
    """
    print("\n[C] Vanilla-gradient finite-difference (central, steepest direction)")
    x = img01.detach().clone().requires_grad_(True)
    with torch.enable_grad():
        logits = mapper._logits_from_leaf(batch, x)
        logits[0, col].backward()
    g = x.grad.detach()
    v = g / g.norm()
    analytic = g.norm().item()  # <g, g/||g||> = ||g||

    f_plus = _mu(mapper, batch, x.detach() + eps * v, col)
    f_minus = _mu(mapper, batch, x.detach() - eps * v, col)
    numeric = (f_plus - f_minus) / (2 * eps)
    rel = abs(analytic - numeric) / (abs(numeric) + 1e-6)
    print(f"    ||grad|| (analytic)   = {analytic:.4f}")
    print(f"    finite-diff (numeric) = {numeric:.4f}   (eps={eps})")
    print(f"    relative error = {rel:.4f}  tol={tol}  (bf16 forward limits precision)")
    ok = rel < tol
    print("    " + ("PASS" if ok else "FAIL"))
    return ok


def _check_gradcam_ordering(mapper, batch, img01, grid_h, grid_w, col, n_cells=6):
    print("\n[D] Grad-CAM spatial ordering (merged-token -> (row,col) reshape)")
    ps = mapper.patch_size  # 14
    ms = mapper.merge_size  # 2
    cell = ps * ms          # 28 px per merged cell
    mh, mw = grid_h // ms, grid_w // ms

    # ---- D1: deterministic patchifier-ordering check (no model) ----------------
    # Occluding the 28x28 block of merged cell (r,c) must change exactly the four
    # consecutive patch rows [4w : 4w+4] of pixel_values, with w = r*mw + c.
    x = img01.detach()
    base_pv = mapper.image_processor.preprocess_tensor(x * 255.0, do_resize=False)[
        "pixel_values"
    ]
    rng = np.random.default_rng(0)
    cells = [(0, 0), (0, mw - 1), (mh - 1, 0), (mh - 1, mw - 1), (mh // 2, mw // 2)]
    cells = cells[:n_cells]
    d1_ok = True
    for (r, c) in cells:
        xp = x.clone()
        xp[..., r * cell:(r + 1) * cell, c * cell:(c + 1) * cell] = 0.0
        pv = mapper.image_processor.preprocess_tensor(xp * 255.0, do_resize=False)[
            "pixel_values"
        ]
        changed = (pv - base_pv).abs().sum(dim=1) > 1e-6  # [num_patches]
        idx = torch.nonzero(changed).flatten().tolist()
        w = r * mw + c
        expected = list(range(4 * w, 4 * w + 4))
        match = idx == expected
        d1_ok &= match
        print(f"    cell(r={r},c={c}) w={w}: changed patches={idx} expected={expected} "
              f"{'ok' if match else 'MISMATCH'}")
    print("    D1 (patchifier order) " + ("PASS" if d1_ok else "FAIL"))

    # ---- D2: model-based occlusion corroboration through the real merger -------
    captured = {}

    def hook(_m, _i, output):
        captured["A"] = output.detach()

    handle = mapper.model.visual.register_forward_hook(hook)
    try:
        with torch.no_grad():
            mapper._logits_from_leaf(batch, x)
            A0 = captured["A"].float().clone()
            d2_hits = 0
            for (r, c) in cells:
                xp = x.clone()
                xp[..., r * cell:(r + 1) * cell, c * cell:(c + 1) * cell] = 0.0
                mapper._logits_from_leaf(batch, xp)
                A1 = captured["A"].float()
                delta = (A1 - A0).norm(dim=-1)  # [num_merged_tokens]
                top = int(delta.argmax())
                pr, pc = top // mw, top % mw
                hit = (pr, pc) == (r, c)
                d2_hits += hit
                print(f"    cell(r={r},c={c}): argmax merged-token={top} -> "
                      f"(r={pr},c={pc}) {'hit' if hit else 'miss (attention spread)'}")
    finally:
        handle.remove()
    print(f"    D2 (through merger) {d2_hits}/{len(cells)} argmax-localized "
          "(D1 is the decisive check; D2 can blur via global attention)")
    return d1_ok


def main(
    checkpoint_path="/gpfs/scratch1/shared/scur0077/models/HPSv3/HPSv3.safetensors",
    image=None,
    prompt=None,
    head="mu",
    ig_steps=128,
    device="cuda",
):
    """Validate vanilla / integrated_gradients / grad_cam correctness on one image."""
    image = image or DEFAULT_IMAGES[0]
    prompt = prompt or _DEFAULT_PROMPT
    col = _TARGET_COLUMN[head]
    print(f"Validating on image={image}\n  head={head} prompt={prompt[:60]!r}...")

    mapper = SaliencyMapper(checkpoint_path=checkpoint_path, device=device)
    batch, grid_h, grid_w = mapper._build_batch(image, prompt)
    img01 = mapper._load_resized_leaf(image, grid_h, grid_w, requires_grad=False)
    print(f"  grid (patches) = ({grid_h},{grid_w}); merged = "
          f"({grid_h // mapper.merge_size},{grid_w // mapper.merge_size}); "
          f"image = {img01.shape[-2]}x{img01.shape[-1]} px")

    results = {
        "A_preprocess_parity": _check_preprocess_parity(mapper, batch, img01),
        "B_ig_completeness": _check_ig_completeness(mapper, batch, img01, col, ig_steps),
        "C_vanilla_finite_diff": _check_vanilla_finite_diff(mapper, batch, img01, col),
        "D_gradcam_ordering": _check_gradcam_ordering(
            mapper, batch, img01, grid_h, grid_w, col
        ),
    }

    print("\n==================== SUMMARY ====================")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print("================================================")
    if not all(results.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    fire.Fire(main)
