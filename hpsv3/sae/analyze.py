from __future__ import annotations

import json
from pathlib import Path

import fire
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
from PIL import Image

from hpsv3.sae.dataset_io import load_activation_metadata, open_activation_bank
from hpsv3.sae.model import TopKSAE
from hpsv3.sae.norm import normalize_activations


def _build_rm_head() -> torch.nn.Sequential:
    return torch.nn.Sequential(
        torch.nn.Linear(3584, 1024),
        torch.nn.ReLU(),
        torch.nn.Dropout(0.05),
        torch.nn.Linear(1024, 16),
        torch.nn.ReLU(),
        torch.nn.Linear(16, 2),
    )


def _load_rm_head(checkpoint_path: str, device: str = "cpu") -> torch.nn.Sequential:
    import safetensors.torch

    state = safetensors.torch.load_file(checkpoint_path, device=device)
    rm_head = _build_rm_head()
    rm_state = {
        key.removeprefix("rm_head."): value
        for key, value in state.items()
        if key.startswith("rm_head.")
    }
    rm_head.load_state_dict(rm_state, strict=True)
    rm_head.eval()
    return rm_head


@torch.no_grad()
def _rm_head_mu_scores(rm_head: torch.nn.Module, decoder_weight: torch.Tensor) -> torch.Tensor:
    hidden = decoder_weight.unsqueeze(1)
    logits = rm_head(hidden)[:, 0, :]
    return logits[:, 0]


@torch.no_grad()
def analyze_sae(
    sae_path: str = "/gpfs/scratch1/shared/scur0077/sae/checkpoints/sae.pt",
    activation_dir: str = "/gpfs/scratch1/shared/scur0077/sae/activations",
    hpsv3_checkpoint: str = "/gpfs/scratch1/shared/scur0077/models/HPSv3/HPSv3.safetensors",
    output_dir: str = "/gpfs/scratch1/shared/scur0077/sae/analysis",
    top_k_features: int = 32,
    top_n_images: int = 16,
    device: str = "cuda",
) -> None:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    model, norm_stats = TopKSAE.load(sae_path, device=device)
    model.eval()
    assert norm_stats is not None

    activations = open_activation_bank(Path(activation_dir) / "activations.npy")
    metadata = load_activation_metadata(activation_dir)
    global_indices = metadata["index"].astype(np.int64).to_numpy()

    rng = np.random.default_rng(42)
    sample_rows = rng.choice(len(metadata), size=min(8192, len(metadata)), replace=False)
    sample_global = global_indices[sample_rows]
    batch = torch.from_numpy(np.asarray(activations[sample_global], dtype=np.float32)).to(device)
    batch = normalize_activations(batch, norm_stats)
    acts, _ = model.encode(batch)
    mean_activation = acts.mean(dim=0).cpu().numpy()

    rm_head = _load_rm_head(hpsv3_checkpoint, device="cpu")
    decoder_directions = model.decoder.weight.detach().T.cpu().float()
    linear_score = _rm_head_mu_scores(rm_head, decoder_directions)
    empirical_corr = []
    mus = metadata.iloc[sample_rows]["mu"].to_numpy(dtype=np.float32)
    for feat_idx in range(model.config.n_features):
        feat_vals = acts[:, feat_idx].float().cpu().numpy()
        if feat_vals.std() < 1e-8:
            empirical_corr.append(0.0)
            continue
        empirical_corr.append(float(np.corrcoef(feat_vals, mus)[0, 1]))
    empirical_corr = np.asarray(empirical_corr)

    report = pd.DataFrame(
        {
            "feature_idx": np.arange(model.config.n_features),
            "mean_activation": mean_activation,
            "rm_mu_score": linear_score.numpy(),
            "empirical_mu_corr": empirical_corr,
        }
    )
    report["combined_rank"] = (
        report["rm_mu_score"].rank(ascending=False)
        + report["empirical_mu_corr"].abs().rank(ascending=False)
    )
    report = report.sort_values("combined_rank").reset_index(drop=True)
    report.to_csv(output / "feature_attribution.csv", index=False)

    top_features = report.head(top_k_features)["feature_idx"].tolist()
    dashboards_dir = output / "dashboards"
    dashboards_dir.mkdir(exist_ok=True)

    encode_batch_size = 512
    full_acts = torch.zeros((len(metadata), model.config.n_features), dtype=torch.float32)
    for start in range(0, len(global_indices), encode_batch_size):
        end = start + encode_batch_size
        idx_batch = global_indices[start:end]
        batch = torch.from_numpy(np.asarray(activations[idx_batch], dtype=np.float32)).to(device)
        batch = normalize_activations(batch, norm_stats)
        acts, _ = model.encode(batch)
        full_acts[start:end] = acts.float().cpu()

    for feature_idx in top_features:
        feat_acts = full_acts[:, feature_idx].numpy()
        top_rows = np.argsort(feat_acts)[-top_n_images:][::-1]
        cols = 4
        rows = int(np.ceil(top_n_images / cols))
        fig, axes = plt.subplots(rows, cols, figsize=(4 * cols, 4 * rows))
        axes = np.atleast_1d(axes).flatten()
        for ax_idx, row_idx in enumerate(top_rows):
            row = metadata.iloc[row_idx]
            image = Image.open(row["image_path"]).convert("RGB")
            axes[ax_idx].imshow(image)
            axes[ax_idx].set_title(
                f"mu={row['mu']:.2f}\nact={feat_acts[row_idx]:.3f}",
                fontsize=8,
            )
            axes[ax_idx].axis("off")
        for ax_idx in range(len(top_rows), len(axes)):
            axes[ax_idx].axis("off")
        fig.suptitle(
            f"Feature {feature_idx} | rm_mu={linear_score[feature_idx]:.4f} | corr={empirical_corr[feature_idx]:.3f}",
            fontsize=12,
        )
        fig.tight_layout()
        fig.savefig(dashboards_dir / f"feature_{feature_idx:05d}.png", dpi=120)
        plt.close(fig)

    summary = {
        "top_positive_mu_features": report.sort_values("rm_mu_score", ascending=False)
        .head(10)[["feature_idx", "rm_mu_score", "empirical_mu_corr"]]
        .to_dict(orient="records"),
        "top_negative_mu_features": report.sort_values("rm_mu_score", ascending=True)
        .head(10)[["feature_idx", "rm_mu_score", "empirical_mu_corr"]]
        .to_dict(orient="records"),
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"Analysis saved to {output}")


if __name__ == "__main__":
    fire.Fire(analyze_sae)
