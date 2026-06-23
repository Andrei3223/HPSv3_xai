"""Run sparse-autoencoder interventions on cached Qwen2-VL layer outputs.

An activation record is a torch-saved dictionary with these tensor entries:
    hidden_states: [batch, sequence, hidden_size], output of ``layer_index``
    attention_mask: [batch, sequence]
    position_ids: [3, batch, sequence] for Qwen2-VL multimodal RoPE
    input_ids: [batch, sequence]
    layer_index: zero-based transformer layer whose output was cached

The SAE supplied to this module only needs two methods:
    encode(hidden_states) -> sparse_activations
    decode(sparse_activations) -> reconstructed_hidden_states
"""

import json
from pathlib import Path

import torch


REQUIRED_RECORD_KEYS = {
    "hidden_states",
    "attention_mask",
    "position_ids",
    "input_ids",
    "layer_index",
}


def _module_device(module):
    parameter = next(module.parameters(), None)
    return parameter.device if parameter is not None else torch.device("cpu")


def _as_json(value):
    if isinstance(value, torch.Tensor):
        return value.detach().float().cpu().tolist()
    return value


class SAEInterventionRunner:
    """Apply one sparse-unit intervention and score both baseline and intervention."""

    def __init__(self, reward_model, sae):
        self.reward_model = reward_model
        self.sae = sae

    @staticmethod
    def load_activation_record(path):
        """Load and validate a cached activation record created with ``torch.save``."""
        record = torch.load(path, map_location="cpu", weights_only=False)
        missing = REQUIRED_RECORD_KEYS.difference(record)
        if missing:
            raise ValueError(f"Activation record is missing keys: {sorted(missing)}")
        if record["hidden_states"].ndim != 3:
            raise ValueError("hidden_states must have shape [batch, sequence, hidden_size]")
        return record

    @staticmethod
    def save_activation_record(path, record):
        """Save a validated activation record for later SAE intervention."""
        missing = REQUIRED_RECORD_KEYS.difference(record)
        if missing:
            raise ValueError(f"Activation record is missing keys: {sorted(missing)}")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        torch.save(record, path)

    @staticmethod
    def _perturb_sparse_unit(sparse, unit_index, magnitude, mode, token_index):
        if not 0 <= unit_index < sparse.shape[-1]:
            raise IndexError(
                f"unit_index {unit_index} is outside sparse dimension {sparse.shape[-1]}"
            )

        perturbed = sparse.clone()
        target = perturbed[..., unit_index]
        if token_index is not None:
            if not 0 <= token_index < target.shape[-1]:
                raise IndexError(
                    f"token_index {token_index} is outside sequence length {target.shape[-1]}"
                )
            target = target[..., token_index]

        if mode == "scale":
            target.mul_(magnitude)
        elif mode == "add":
            target.add_(magnitude)
        elif mode == "set":
            target.fill_(magnitude)
        else:
            raise ValueError("mode must be one of: scale, add, set")
        return perturbed

    def _pool_reward(self, hidden_states, input_ids, attention_mask):
        logits = self.reward_model.rm_head(hidden_states.float())
        reward_token = self.reward_model.reward_token

        if reward_token == "special":
            mask = torch.zeros_like(input_ids, dtype=torch.bool)
            for token_id in self.reward_model.special_token_ids:
                mask |= input_ids == token_id
            counts = mask.sum(dim=1)
            if not torch.all(counts == 1):
                raise ValueError(
                    "Each cached example must contain exactly one reward special token"
                )
            return logits[mask].view(hidden_states.shape[0], -1)

        if reward_token == "last":
            last_index = attention_mask.long().sum(dim=1).sub(1)
            return logits[torch.arange(logits.shape[0], device=logits.device), last_index]

        if reward_token == "mean":
            weights = attention_mask.unsqueeze(-1).to(logits.dtype)
            return (logits * weights).sum(dim=1) / weights.sum(dim=1).clamp_min(1)

        raise ValueError(f"Unsupported reward token mode: {reward_token}")

    @torch.inference_mode()
    def _forward_tail(self, hidden_states, record):
        """Run cached layer outputs through the remaining Qwen2-VL text layers."""
        text_model = self.reward_model.model
        layer_index = record["layer_index"]
        if not 0 <= layer_index < len(text_model.layers):
            raise ValueError(f"Invalid cached layer_index: {layer_index}")

        device = _module_device(text_model)
        hidden_states = hidden_states.to(device=device, dtype=text_model.norm.weight.dtype)
        attention_mask = record["attention_mask"].to(device)
        position_ids = record["position_ids"].to(device)
        input_ids = record["input_ids"].to(device)

        sequence_length = hidden_states.shape[1]
        cache_position = torch.arange(sequence_length, device=device)
        causal_mask = text_model._update_causal_mask(
            attention_mask,
            hidden_states,
            cache_position,
            None,
            False,
        )
        position_embeddings = text_model.rotary_emb(hidden_states, position_ids)

        for decoder_layer in text_model.layers[layer_index + 1 :]:
            hidden_states = decoder_layer(
                hidden_states,
                attention_mask=causal_mask,
                position_ids=position_ids,
                past_key_value=None,
                output_attentions=False,
                use_cache=False,
                cache_position=cache_position,
                position_embeddings=position_embeddings,
            )[0]

        hidden_states = text_model.norm(hidden_states)
        return self._pool_reward(hidden_states, input_ids, attention_mask)

    @torch.inference_mode()
    def run(
        self,
        record,
        unit_index,
        magnitude,
        mode="scale",
        token_index=None,
    ):
        """Reconstruct, intervene, correct with the residual, and score both states.

        The baseline is the cached activation itself. The SAE residual is added to
        the perturbed decode, isolating the output difference to the sparse unit.
        """
        missing = REQUIRED_RECORD_KEYS.difference(record)
        if missing:
            raise ValueError(f"Activation record is missing keys: {sorted(missing)}")

        sae_device = _module_device(self.sae)
        dense = record["hidden_states"].to(sae_device)
        sparse = self.sae.encode(dense)
        reconstruction = self.sae.decode(sparse)
        if reconstruction.shape != dense.shape:
            raise ValueError(
                f"SAE reconstruction shape {tuple(reconstruction.shape)} does not match "
                f"dense activation shape {tuple(dense.shape)}"
            )

        residual = dense - reconstruction
        baseline_dense = dense
        perturbed_sparse = self._perturb_sparse_unit(
            sparse, unit_index, magnitude, mode, token_index
        )
        perturbed_dense = self.sae.decode(perturbed_sparse) + residual

        baseline_output = self._forward_tail(baseline_dense, record)
        perturbed_output = self._forward_tail(perturbed_dense, record)

        return {
            "layer_index": record["layer_index"],
            "baseline_output": baseline_output,
            "perturbed_output": perturbed_output,
            "output_delta": perturbed_output - baseline_output,
            "reconstruction_mse": residual.float().square().mean(),
            "residual": residual,
            "sparse_activations": sparse,
            "perturbed_sparse_activations": perturbed_sparse,
            "reconstruction": reconstruction,
            "baseline_dense": baseline_dense,
            "perturbed_dense": perturbed_dense,
        }

    @staticmethod
    def save_results(
        json_path,
        tensor_path,
        result,
        record_path,
        unit_index,
        magnitude,
        mode,
        token_index,
    ):
        """Save scalar/model outputs as JSON and large SAE tensors as a torch sidecar."""
        tensor_path = Path(tensor_path)
        json_path = Path(json_path)
        tensor_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.parent.mkdir(parents=True, exist_ok=True)

        torch.save(
            {
                key: result[key].detach().cpu()
                for key in (
                    "residual",
                    "sparse_activations",
                    "perturbed_sparse_activations",
                    "reconstruction",
                    "baseline_dense",
                    "perturbed_dense",
                )
            },
            tensor_path,
        )
        summary = {
            "activation_record": str(record_path),
            "tensor_results": str(tensor_path),
            "layer_index": int(result.get("layer_index", -1)),
            "unit_index": unit_index,
            "token_index": token_index,
            "intervention_mode": mode,
            "intervention_magnitude": magnitude,
            "reconstruction_mse": _as_json(result["reconstruction_mse"]),
            "baseline_output": _as_json(result["baseline_output"]),
            "perturbed_output": _as_json(result["perturbed_output"]),
            "output_delta": _as_json(result["output_delta"]),
        }
        with json_path.open("w") as file:
            json.dump(summary, file, indent=2)
