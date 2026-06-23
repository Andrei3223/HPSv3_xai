from __future__ import annotations

import json
import random
from pathlib import Path

import fire
import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset
from tqdm import tqdm

from hpsv3.sae.dataset_io import load_extracted_indices, open_activation_bank
from hpsv3.sae.model import SAEConfig, TopKSAE
from hpsv3.sae.norm import (
    compute_norm_stats,
    load_norm_stats,
    normalize_activations,
    save_norm_stats,
)


class ActivationDataset(Dataset):
    def __init__(self, activations: np.memmap, indices: np.ndarray, norm_stats: dict):
        self.activations = activations
        self.indices = indices
        self.norm_stats = norm_stats

    def __len__(self) -> int:
        return len(self.indices)

    def __getitem__(self, idx: int) -> torch.Tensor:
        row = np.asarray(self.activations[self.indices[idx]], dtype=np.float32)
        tensor = torch.from_numpy(row)
        return normalize_activations(tensor.unsqueeze(0), self.norm_stats).squeeze(0)


class DeadFeatureTracker:
    def __init__(self, n_features: int, dead_steps: int = 1000):
        self.n_features = n_features
        self.dead_steps = dead_steps
        self.steps_since_fired = torch.zeros(n_features, dtype=torch.long)

    def update(self, acts: torch.Tensor) -> None:
        fired = (acts > 0).any(dim=0).cpu()
        self.steps_since_fired += 1
        self.steps_since_fired[fired] = 0

    def dead_mask(self) -> torch.Tensor:
        return self.steps_since_fired >= self.dead_steps


def train_sae(
    activation_dir: str = "/gpfs/scratch1/shared/scur0077/sae/activations",
    output_dir: str = "/gpfs/scratch1/shared/scur0077/sae/checkpoints",
    d_in: int = 3584,
    n_features: int = 16384,
    k: int = 32,
    auxk_alpha: float = 0.03125,
    batch_size: int = 4096,
    lr: float = 4e-4,
    epochs: int = 15,
    device: str = "cuda",
    seed: int = 42,
    val_fraction: float = 0.01,
) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    activation_dir = Path(activation_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    activations_path = activation_dir / "activations.npy"
    norm_stats_path = activation_dir / "norm_stats.json"
    activations = open_activation_bank(activations_path, d_in=d_in)
    extracted_indices = load_extracted_indices(activation_dir)
    if len(extracted_indices) > len(activations):
        raise RuntimeError(
            f"Metadata references {len(extracted_indices)} rows but activation bank has {len(activations)}"
        )
    if len(extracted_indices) < len(activations):
        print(
            f"Using {len(extracted_indices)} / {len(activations)} extracted rows "
            "(skipping unwritten memmap slots)"
        )

    if norm_stats_path.exists():
        norm_stats = load_norm_stats(norm_stats_path)
    else:
        norm_stats = compute_norm_stats(activations[extracted_indices])
        save_norm_stats(norm_stats, norm_stats_path)

    n = len(extracted_indices)
    indices = extracted_indices.copy()
    np.random.shuffle(indices)
    val_size = max(1, int(n * val_fraction))
    train_indices = indices[val_size:]
    val_indices = indices[:val_size]

    train_loader = DataLoader(
        ActivationDataset(activations, train_indices, norm_stats),
        batch_size=batch_size,
        shuffle=True,
        drop_last=True,
        num_workers=0,
    )
    val_loader = DataLoader(
        ActivationDataset(activations, val_indices, norm_stats),
        batch_size=batch_size,
        shuffle=False,
        num_workers=0,
    )

    config = SAEConfig(
        d_in=d_in,
        n_features=n_features,
        k=k,
        auxk_alpha=auxk_alpha,
    )
    model = TopKSAE(config).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    tracker = DeadFeatureTracker(n_features)

    history: list[dict] = []
    global_step = 0

    for epoch in range(epochs):
        model.train()
        epoch_loss = 0.0
        n_batches = 0

        for batch in tqdm(train_loader, desc=f"Epoch {epoch + 1}/{epochs}"):
            batch = batch.to(device)
            dead_mask = tracker.dead_mask().to(device)
            loss_out = model.loss(batch, dead_mask=dead_mask)

            optimizer.zero_grad(set_to_none=True)
            loss_out.total.backward()
            optimizer.step()
            model._normalize_decoder_columns()
            tracker.update(model.encode(batch)[0].detach())

            epoch_loss += loss_out.total.item()
            n_batches += 1
            global_step += 1

        model.eval()
        val_mse = 0.0
        val_fve = 0.0
        val_l0 = 0.0
        val_batches = 0
        with torch.no_grad():
            for batch in val_loader:
                batch = batch.to(device)
                recon, acts, _ = model(batch)
                val_mse += torch.nn.functional.mse_loss(recon, batch).item()
                val_fve += model.fraction_variance_explained(batch, recon)
                val_l0 += (acts > 0).float().sum(dim=-1).mean().item()
                val_batches += 1

        dead_fraction = tracker.dead_mask().float().mean().item()
        metrics = {
            "epoch": epoch + 1,
            "train_loss": epoch_loss / max(n_batches, 1),
            "val_mse": val_mse / max(val_batches, 1),
            "val_fve": val_fve / max(val_batches, 1),
            "val_l0": val_l0 / max(val_batches, 1),
            "dead_fraction": dead_fraction,
        }
        history.append(metrics)
        print(json.dumps(metrics, indent=2))

        checkpoint_path = output_dir / f"sae_epoch_{epoch + 1:03d}.pt"
        model.save(checkpoint_path, norm_stats=norm_stats)

    final_path = output_dir / "sae.pt"
    model.save(final_path, norm_stats=norm_stats)
    (output_dir / "train_history.json").write_text(json.dumps(history, indent=2))
    print(f"Saved final SAE to {final_path}")


if __name__ == "__main__":
    fire.Fire(train_sae)
