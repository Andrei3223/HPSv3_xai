from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import NamedTuple

import torch
import torch.nn as nn
import torch.nn.functional as F


@dataclass
class SAEConfig:
    d_in: int = 3584
    n_features: int = 16384
    k: int = 32
    auxk_alpha: float = 0.03125
    auxk_k: int = 256

    def to_dict(self) -> dict:
        return asdict(self)


class SAELoss(NamedTuple):
    total: torch.Tensor
    mse: torch.Tensor
    auxk: torch.Tensor


class TopKSAE(nn.Module):
    def __init__(self, config: SAEConfig):
        super().__init__()
        self.config = config
        self.b_dec = nn.Parameter(torch.zeros(config.d_in))
        self.encoder = nn.Linear(config.d_in, config.n_features, bias=True)
        self.decoder = nn.Linear(config.n_features, config.d_in, bias=False)
        self._init_weights()

    def _init_weights(self) -> None:
        nn.init.kaiming_uniform_(self.encoder.weight, a=5**0.5)
        nn.init.zeros_(self.encoder.bias)
        nn.init.kaiming_uniform_(self.decoder.weight, a=5**0.5)
        self._normalize_decoder_columns()

    @torch.no_grad()
    def _normalize_decoder_columns(self) -> None:
        norms = self.decoder.weight.norm(dim=0, keepdim=True).clamp(min=1e-8)
        self.decoder.weight.div_(norms)

    def encode(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        x_centered = x - self.b_dec
        pre_acts = self.encoder(x_centered)
        post_acts = F.relu(pre_acts)
        topk_vals, topk_idx = torch.topk(post_acts, self.config.k, dim=-1)
        acts = torch.zeros_like(post_acts)
        acts.scatter_(-1, topk_idx, topk_vals)
        return acts, pre_acts

    def decode(self, acts: torch.Tensor) -> torch.Tensor:
        return self.decoder(acts)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        acts, pre_acts = self.encode(x)
        recon = self.decode(acts)
        return recon, acts, pre_acts

    def loss(
        self,
        x: torch.Tensor,
        dead_mask: torch.Tensor | None = None,
    ) -> SAELoss:
        recon, acts, pre_acts = self.forward(x)
        mse = F.mse_loss(recon, x)

        auxk = torch.zeros((), device=x.device, dtype=x.dtype)
        if dead_mask is not None and dead_mask.any() and self.config.auxk_alpha > 0:
            residual = x - recon
            dead_pre = pre_acts[:, dead_mask]
            if dead_pre.numel() > 0:
                k = min(self.config.auxk_k, dead_pre.shape[-1])
                topk_vals, topk_idx = torch.topk(dead_pre, k, dim=-1)
                aux_acts = torch.zeros_like(pre_acts)
                dead_indices = dead_mask.nonzero(as_tuple=False).squeeze(-1)
                for local_idx, feature_idx in enumerate(dead_indices):
                    aux_acts[:, feature_idx] = topk_vals[:, local_idx]
                aux_acts = F.relu(aux_acts)
                aux_recon = self.decode(aux_acts)
                auxk = F.mse_loss(aux_recon, residual)

        total = mse + self.config.auxk_alpha * auxk
        return SAELoss(total=total, mse=mse, auxk=auxk)

    @torch.no_grad()
    def fraction_variance_explained(self, x: torch.Tensor, recon: torch.Tensor) -> float:
        total_var = ((x - x.mean(dim=0, keepdim=True)) ** 2).mean()
        if total_var.item() <= 0:
            return 0.0
        return (1.0 - F.mse_loss(recon, x) / total_var).item()

    def save(self, path: str, norm_stats: dict | None = None) -> None:
        payload = {
            "config": self.config.to_dict(),
            "state_dict": self.state_dict(),
            "norm_stats": norm_stats,
        }
        torch.save(payload, path)

    @classmethod
    def load(cls, path: str, device: str | torch.device = "cpu") -> tuple["TopKSAE", dict | None]:
        payload = torch.load(path, map_location=device, weights_only=False)
        config = SAEConfig(**payload["config"])
        model = cls(config).to(device)
        model.load_state_dict(payload["state_dict"])
        return model, payload.get("norm_stats")
