from __future__ import annotations

import json
from pathlib import Path

import fire
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
from PIL import Image

from hpsv3.sae.analyze import _load_rm_head
from hpsv3.sae.dataset_io import load_activation_metadata, open_activation_bank
from hpsv3.sae.model import TopKSAE
from hpsv3.sae.norm import load_norm_stats


def _parse_feature_indices(feature_indices: str | int | list | tuple | None) -> list[int] | None:
    if feature_indices is None:
        return None
    if isinstance(feature_indices, int):
        return [feature_indices]
    if isinstance(feature_indices, (list, tuple)):
        return [int(x) for x in feature_indices]
    return [int(x.strip()) for x in str(feature_indices).split(",") if x.strip()]


def _pick_examples(metadata: pd.DataFrame, n_examples: int, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    sorted_meta = metadata.sort_values("mu")
    picks: list[int] = []
    if n_examples >= 1:
        picks.append(int(sorted_meta.iloc[-1].name))
    if n_examples >= 2:
        picks.append(int(sorted_meta.iloc[0].name))
    if n_examples >= 3:
        picks.append(int(sorted_meta.iloc[len(sorted_meta) // 2].name))
    remaining = n_examples - len(picks)
    if remaining > 0:
        pool = [i for i in range(len(metadata)) if i not in picks]
        extra = rng.choice(pool, size=min(remaining, len(pool)), replace=False)
        picks.extend(int(i) for i in extra)
    return metadata.iloc[picks].reset_index(drop=True)


def _pick_features(report: pd.DataFrame, n_features: int) -> list[int]:
    n_each = max(1, n_features // 2)
    pos = report.sort_values("rm_mu_score", ascending=False).head(n_each)["feature_idx"].tolist()
    neg = report.sort_values("rm_mu_score", ascending=True).head(n_each)["feature_idx"].tolist()
    features = []
    for left, right in zip(pos, neg):
        features.extend([int(left), int(right)])
    if len(features) < n_features:
        rest = (
            report.sort_values("empirical_mu_corr", key=lambda s: s.abs(), ascending=False)["feature_idx"]
            .tolist()
        )
        for feat in rest:
            if int(feat) not in features:
                features.append(int(feat))
            if len(features) >= n_features:
                break
    return features[:n_features]


@torch.no_grad()
def _steer_curve(
    rm_head: torch.nn.Module,
    base_hidden: torch.Tensor,
    direction: torch.Tensor,
    alphas: torch.Tensor,
) -> tuple[np.ndarray, float]:
    baseline_mu = rm_head(base_hidden.unsqueeze(0))[0, 0].item()
    mus: list[float] = []
    for alpha in alphas:
        steered = base_hidden + alpha * direction
        mu = rm_head(steered.unsqueeze(0))[0, 0].item()
        mus.append(mu)
    return np.asarray(mus, dtype=np.float32), baseline_mu


def _auto_alpha_range(
    report: pd.DataFrame,
    features: list[int],
    target_delta_mu: float,
    alpha_min: float | None,
    alpha_max: float | None,
) -> tuple[float, float]:
    if alpha_min is not None and alpha_max is not None:
        return alpha_min, alpha_max
    rm_vals = []
    for feat_idx in features:
        rows = report.loc[report["feature_idx"] == feat_idx, "rm_mu_score"]
        if len(rows):
            rm_vals.append(abs(float(rows.iloc[0])))
    scale = max(rm_vals) if rm_vals else 0.02
    limit = max(10.0, target_delta_mu / max(scale, 1e-4))
    return -limit, limit


def _encode_feature_activations(
    sae: TopKSAE,
    norm_stats: dict,
    activations: np.memmap,
    global_indices: np.ndarray,
    feature_indices: list[int],
    device: str,
    batch_size: int = 512,
) -> np.ndarray:
    from hpsv3.sae.norm import normalize_activations

    n_rows = len(global_indices)
    n_features = len(feature_indices)
    out = np.zeros((n_rows, n_features), dtype=np.float32)
    for start in range(0, n_rows, batch_size):
        end = min(start + batch_size, n_rows)
        idx_batch = global_indices[start:end]
        batch = torch.from_numpy(np.asarray(activations[idx_batch], dtype=np.float32)).to(device)
        batch = normalize_activations(batch, norm_stats)
        acts, _ = sae.encode(batch)
        acts_np = acts.float().cpu().numpy()
        for col, feat_idx in enumerate(feature_indices):
            out[start:end, col] = acts_np[:, feat_idx]
    return out


def _show_image(ax, image_path: str, title: str) -> None:
    image = Image.open(image_path).convert("RGB")
    ax.imshow(image)
    ax.set_title(title, fontsize=8)
    ax.axis("off")


def _save_score_bar_panel(
    panels_dir: Path,
    ex_idx: int,
    global_idx: int,
    image_path: str,
    recorded_mu: float,
    prompt: str,
    features: list[int],
    curves: dict[int, tuple[np.ndarray, float, np.ndarray]],
    report: pd.DataFrame,
    alpha_hi: float,
) -> None:
    fig = plt.figure(figsize=(10, 5))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.1, 1.4])
    ax_img = fig.add_subplot(gs[0, 0])
    ax_bar = fig.add_subplot(gs[0, 1])
    _show_image(ax_img, image_path, f"Query image\nμ={recorded_mu:.2f}")
    names, deltas, colors = [], [], []
    for feat_idx in features:
        delta = curves[feat_idx][2]
        d = float(delta[-1])
        names.append(f"f{feat_idx}")
        deltas.append(d)
        colors.append("#2ca02c" if d >= 0 else "#d62728")
    ax_bar.barh(names, deltas, color=colors)
    ax_bar.axvline(0.0, color="black", linewidth=1)
    ax_bar.set_xlabel(f"Δμ if feature added (α={alpha_hi:.0f})")
    ax_bar.set_title("Score-only steering (pixels unchanged)")
    ax_bar.grid(True, axis="x", alpha=0.3)
    prompt_short = str(prompt)[:100].replace("\n", " ")
    fig.suptitle(
        f"Internal steering does not edit pixels | index {global_idx} | {prompt_short}...",
        fontsize=10,
    )
    fig.tight_layout()
    fig.savefig(panels_dir / f"example_{ex_idx:02d}_idx{global_idx}_score_panel.png", dpi=140)
    plt.close(fig)


def _save_visual_compare_panel(
    compare_dir: Path,
    ex_idx: int,
    global_idx: int,
    query_row: pd.Series,
    feat_idx: int,
    feat_col: int,
    metadata: pd.DataFrame,
    feat_acts: np.ndarray,
    rm_mu: float,
    corr: float,
    n_neighbors: int,
) -> None:
    query_row_idx = metadata.index[metadata["index"] == global_idx]
    if len(query_row_idx) == 0:
        return
    query_meta_idx = int(query_row_idx[0])
    query_act = float(feat_acts[query_meta_idx, feat_col])

    order = np.argsort(feat_acts[:, feat_col])
    low_rows = [i for i in order if i != query_meta_idx][:n_neighbors]
    high_rows = [i for i in order[::-1] if i != query_meta_idx][:n_neighbors]

    n_cols = n_neighbors
    fig, axes = plt.subplots(3, n_cols, figsize=(2.8 * n_cols, 8))
    if n_cols == 1:
        axes = np.array(axes).reshape(3, 1)

    for col, row_idx in enumerate(low_rows):
        row = metadata.iloc[row_idx]
        _show_image(
            axes[0, col],
            row["image_path"],
            f"LOW act={feat_acts[row_idx, feat_col]:.2f}\nμ={row['mu']:.1f}",
        )
    for col, row_idx in enumerate(high_rows):
        row = metadata.iloc[row_idx]
        _show_image(
            axes[2, col],
            row["image_path"],
            f"HIGH act={feat_acts[row_idx, feat_col]:.2f}\nμ={row['mu']:.1f}",
        )

    center_col = n_cols // 2
    for col in range(n_cols):
        ax = axes[1, col]
        if col == center_col:
            _show_image(
                ax,
                query_row["image_path"],
                f"YOUR image\nact={query_act:.2f}\nμ={query_row['mu']:.1f}",
            )
            for spine in ax.spines.values():
                spine.set_edgecolor("gold")
                spine.set_linewidth(3)
        else:
            ax.axis("off")

    fig.suptitle(
        f"Feature {feat_idx} visual contrast (rm_mu={rm_mu:+.3f}, corr={corr:+.2f})\n"
        "Top/bottom = real dataset images where this feature fires most/least — not pixel edits",
        fontsize=11,
    )
    fig.tight_layout()
    fig.savefig(compare_dir / f"example_{ex_idx:02d}_idx{global_idx}_feature_{feat_idx:05d}_compare.png", dpi=140)
    plt.close(fig)


@torch.no_grad()
def steer_sae(
    sae_path: str = "/gpfs/scratch1/shared/scur0077/sae/checkpoints/sae_epoch_006.pt",
    activation_dir: str = "/gpfs/scratch1/shared/scur0077/sae/activations",
    hpsv3_checkpoint: str = "/gpfs/scratch1/shared/scur0077/models/HPSv3/HPSv3.safetensors",
    analysis_dir: str = "/gpfs/scratch1/shared/scur0077/sae/analysis",
    output_dir: str = "/gpfs/scratch1/shared/scur0077/sae/steering",
    n_examples: int = 3,
    n_features: int = 6,
    n_neighbors: int = 4,
    alpha_min: float | None = None,
    alpha_max: float | None = None,
    target_delta_mu: float = 2.0,
    n_steps: int = 41,
    plot_absolute: bool = False,
    visual_compare: bool = True,
    device: str = "cuda",
    feature_indices: str | None = None,
) -> None:
    output = Path(output_dir)
    curves_dir = output / "curves"
    panels_dir = output / "panels"
    compare_dir = output / "compare"
    curves_dir.mkdir(parents=True, exist_ok=True)
    panels_dir.mkdir(parents=True, exist_ok=True)
    compare_dir.mkdir(parents=True, exist_ok=True)

    sae, norm_stats = TopKSAE.load(sae_path, device=device)
    sae.eval()
    assert norm_stats is not None

    activations = open_activation_bank(Path(activation_dir) / "activations.npy")
    metadata = load_activation_metadata(activation_dir)
    rm_head = _load_rm_head(hpsv3_checkpoint, device=device).to(device)

    report_path = Path(analysis_dir) / "feature_attribution.csv"
    if report_path.exists():
        report = pd.read_csv(report_path)
    else:
        report = pd.DataFrame({"feature_idx": np.arange(sae.config.n_features), "rm_mu_score": 0.0, "empirical_mu_corr": 0.0})

    if feature_indices:
        features = _parse_feature_indices(feature_indices)
    else:
        features = _pick_features(report, n_features)

    examples = _pick_examples(metadata, n_examples)
    alpha_lo, alpha_hi = _auto_alpha_range(report, features, target_delta_mu, alpha_min, alpha_max)
    alphas = torch.linspace(alpha_lo, alpha_hi, n_steps, device=device)
    decoder = sae.decoder.weight.detach().float()
    rms = float(norm_stats["rms"])
    global_indices = metadata["index"].astype(np.int64).to_numpy()
    feat_to_col = {feat: col for col, feat in enumerate(features)}
    feat_acts = _encode_feature_activations(
        sae, norm_stats, activations, global_indices, features, device
    )

    summary_rows: list[dict] = []

    for ex_idx, row in examples.iterrows():
        global_idx = int(row["index"])
        base_hidden = torch.from_numpy(np.asarray(activations[global_idx], dtype=np.float32)).to(device)
        recorded_mu = float(row["mu"])

        curves: dict[int, tuple[np.ndarray, float, np.ndarray]] = {}
        feature_stats: list[dict] = []

        for feat_idx in features:
            direction = decoder[:, feat_idx] * rms
            mus, baseline_mu = _steer_curve(rm_head, base_hidden, direction, alphas)
            delta = mus - baseline_mu
            curves[feat_idx] = (mus, baseline_mu, delta)
            feat_row = report.loc[report["feature_idx"] == feat_idx]
            rm_mu = float(feat_row["rm_mu_score"].iloc[0]) if len(feat_row) else 0.0
            corr = float(feat_row["empirical_mu_corr"].iloc[0]) if len(feat_row) else 0.0
            feature_stats.append(
                {
                    "feature_idx": int(feat_idx),
                    "rm_mu_score": rm_mu,
                    "empirical_mu_corr": corr,
                    "baseline_mu": baseline_mu,
                    "mu_at_alpha_max": float(mus[-1]),
                    "mu_at_alpha_min": float(mus[0]),
                    "delta_mu_at_alpha_max": float(delta[-1]),
                    "delta_mu_at_alpha_min": float(delta[0]),
                }
            )

        alpha_np = alphas.cpu().numpy()

        fig, ax = plt.subplots(figsize=(9, 5))
        for feat_idx in features:
            mus, baseline_mu, delta = curves[feat_idx]
            feat_row = report.loc[report["feature_idx"] == feat_idx]
            rm_mu = float(feat_row["rm_mu_score"].iloc[0]) if len(feat_row) else 0.0
            corr = float(feat_row["empirical_mu_corr"].iloc[0]) if len(feat_row) else 0.0
            y = mus if plot_absolute else delta
            label = f"f{feat_idx} (slope≈{rm_mu:+.3f}, r={corr:+.2f})"
            ax.plot(alpha_np, y, label=label, linewidth=2.5)

        if plot_absolute:
            baseline_mu = feature_stats[0]["baseline_mu"]
            ax.axhline(baseline_mu, color="black", linestyle="--", linewidth=1, label=f"baseline μ={baseline_mu:.2f}")
            ax.axhline(recorded_mu, color="gray", linestyle=":", linewidth=1, label=f"stored μ={recorded_mu:.2f}")
            ax.set_ylabel("Predicted HPS μ")
            ax.set_title(f"Absolute μ steering | example {ex_idx} | index {global_idx}")
        else:
            ax.axhline(0.0, color="black", linestyle="--", linewidth=1)
            ax.set_ylabel("Δμ vs baseline (zoomed)")
            ax.set_title(
                f"Δμ steering | example {ex_idx} | index {global_idx} | baseline μ={feature_stats[0]['baseline_mu']:.2f}"
            )
        ax.axvline(0.0, color="black", linewidth=0.8, alpha=0.4)
        ax.set_xlabel(f"Steering α in [{alpha_lo:.0f}, {alpha_hi:.0f}]  (h' = h + α · decoder direction)")
        ax.legend(fontsize=8, loc="best")
        ax.grid(True, alpha=0.25)
        if not plot_absolute:
            ymax = max(abs(curves[f][2]).max() for f in features)
            ax.set_ylim(-ymax * 1.15, ymax * 1.15)
        fig.tight_layout()
        suffix = "absolute" if plot_absolute else "delta"
        fig.savefig(curves_dir / f"example_{ex_idx:02d}_idx{global_idx}_{suffix}.png", dpi=140)
        plt.close(fig)

        _save_score_bar_panel(
            panels_dir,
            ex_idx,
            global_idx,
            row["image_path"],
            recorded_mu,
            row["prompt"],
            features,
            curves,
            report,
            alpha_hi,
        )

        if visual_compare:
            for feat_idx in features:
                feat_row = report.loc[report["feature_idx"] == feat_idx]
                rm_mu = float(feat_row["rm_mu_score"].iloc[0]) if len(feat_row) else 0.0
                corr = float(feat_row["empirical_mu_corr"].iloc[0]) if len(feat_row) else 0.0
                _save_visual_compare_panel(
                    compare_dir,
                    ex_idx,
                    global_idx,
                    row,
                    feat_idx,
                    feat_to_col[feat_idx],
                    metadata,
                    feat_acts,
                    rm_mu,
                    corr,
                    n_neighbors,
                )

        summary_rows.append(
            {
                "example_idx": int(ex_idx),
                "global_index": global_idx,
                "image_path": row["image_path"],
                "stored_mu": recorded_mu,
                "baseline_mu": feature_stats[0]["baseline_mu"],
                "alpha_range": [alpha_lo, alpha_hi],
                "prompt": row["prompt"],
                "features": feature_stats,
            }
        )

    (output / "steering_summary.json").write_text(json.dumps(summary_rows, indent=2))
    print(
        json.dumps(
            {
                "n_examples": len(summary_rows),
                "features": features,
                "alpha_range": [alpha_lo, alpha_hi],
                "note": "Steering edits internal activations only; see compare/ for visual contrast across dataset images.",
                "output_dir": str(output),
            },
            indent=2,
        )
    )
    print(f"Steering visualizations saved to {output}")


if __name__ == "__main__":
    fire.Fire(steer_sae)
