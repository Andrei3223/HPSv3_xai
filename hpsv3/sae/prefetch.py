from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import torch
from transformers import AutoProcessor

from hpsv3.dataset.data_collator_qwen import (
    INSTRUCTION,
    prompt_with_special_token,
    prompt_without_special_token,
)
from hpsv3.dataset.utils import process_vision_info

_worker_processor: AutoProcessor | None = None
_worker_use_special_tokens: bool = True


def build_processor(model_name_or_path: str, use_special_tokens: bool) -> AutoProcessor:
    processor = AutoProcessor.from_pretrained(model_name_or_path, padding_side="right")
    if use_special_tokens:
        processor.tokenizer.add_special_tokens({"additional_special_tokens": ["<|Reward|>"]})
    return processor


def init_dataloader_workers(
    worker_id: int,
    model_name_or_path: str,
    use_special_tokens: bool,
) -> None:
    global _worker_processor, _worker_use_special_tokens
    _worker_use_special_tokens = use_special_tokens
    _worker_processor = build_processor(model_name_or_path, use_special_tokens)


def prepare_batch_cpu(
    processor: AutoProcessor,
    use_special_tokens: bool,
    image_paths: Sequence[str],
    prompts: Sequence[str],
) -> dict[str, Any]:
    max_pixels = 256 * 28 * 28
    min_pixels = 256 * 28 * 28
    message_list = []
    for text, image in zip(prompts, image_paths):
        out_message = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "image": image,
                        "min_pixels": min_pixels,
                        "max_pixels": max_pixels,
                    },
                    {
                        "type": "text",
                        "text": (
                            INSTRUCTION.format(text_prompt=text) + prompt_with_special_token
                            if use_special_tokens
                            else prompt_without_special_token
                        ),
                    },
                ],
            }
        ]
        message_list.append(out_message)

    image_inputs, _ = process_vision_info(message_list)
    return processor(
        text=processor.apply_chat_template(message_list, tokenize=False, add_generation_prompt=True),
        images=image_inputs,
        padding=True,
        return_tensors="pt",
        videos_kwargs={"do_rescale": True},
    )


def collate_samples(samples: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if _worker_processor is None:
        raise RuntimeError("DataLoader workers not initialized")
    image_paths = [sample["image_path"] for sample in samples]
    prompts = [sample["prompt"] for sample in samples]
    batch = prepare_batch_cpu(_worker_processor, _worker_use_special_tokens, image_paths, prompts)
    return batch, samples


def collate_with_processor(
    processor: AutoProcessor,
    use_special_tokens: bool,
    samples: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    image_paths = [sample["image_path"] for sample in samples]
    prompts = [sample["prompt"] for sample in samples]
    batch = prepare_batch_cpu(processor, use_special_tokens, image_paths, prompts)
    return batch, samples


def move_batch_to_device(batch: Mapping[str, Any], device: str | torch.device) -> dict[str, Any]:
    moved: dict[str, Any] = {}
    for key, value in batch.items():
        if isinstance(value, torch.Tensor):
            moved[key] = value.to(device, non_blocking=True)
        else:
            moved[key] = value
    return moved
