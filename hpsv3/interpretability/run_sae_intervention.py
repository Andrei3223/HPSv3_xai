"""CLI for a single SAE intervention on one cached activation record."""

import argparse

import torch

from hpsv3.inference import HPSv3RewardInferencer
from hpsv3.interpretability.sae_intervention import SAEInterventionRunner


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--activation-path", required=True)
    parser.add_argument("--sae-path", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-tensors", required=True)
    parser.add_argument("--unit-index", required=True, type=int)
    parser.add_argument("--magnitude", required=True, type=float)
    parser.add_argument("--mode", choices=("scale", "add", "set"), default="scale")
    parser.add_argument("--token-index", type=int)
    parser.add_argument("--config-path")
    parser.add_argument("--checkpoint-path")
    parser.add_argument("--device", default="cuda")
    return parser.parse_args()


def main():
    args = parse_args()
    inferencer = HPSv3RewardInferencer(
        config_path=args.config_path,
        checkpoint_path=args.checkpoint_path,
        device=args.device,
    )
    sae = torch.load(args.sae_path, map_location=args.device, weights_only=False)
    if not isinstance(sae, torch.nn.Module):
        raise TypeError("sae-path must contain a serialized torch.nn.Module")
    sae.to(args.device).eval()

    runner = SAEInterventionRunner(inferencer.model, sae)
    record = runner.load_activation_record(args.activation_path)
    result = runner.run(
        record,
        unit_index=args.unit_index,
        magnitude=args.magnitude,
        mode=args.mode,
        token_index=args.token_index,
    )
    runner.save_results(
        args.output_json,
        args.output_tensors,
        result,
        args.activation_path,
        args.unit_index,
        args.magnitude,
        args.mode,
        args.token_index,
    )


if __name__ == "__main__":
    main()
