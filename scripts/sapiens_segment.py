#!/usr/bin/env python
"""
Stage 0: run Sapiens-lite body-part segmentation and emit label maps for the
HPSv3 perturbation pipeline.

Runs in the SAPIENS venv (needs only torch + numpy + opencv). It does NOT import
the hpsv3 package, so it is unaffected by HPSv3/EditReward dependency conflicts.
The conversion helpers in hpsv3/xai/sapiens_segments.py are loaded by file path.

For each image it writes into --output-dir:
    <stem>_sapiens_labels.npy   contiguous region labels (matches image H,W)
    <stem>_parts.json           {label_id: part_name}

Preprocessing matches sapiens-lite seg: input (1024,768), mean [123.5,116.5,103.5],
std [58.5,57.0,57.5], argmax over classes.

Example:
    python scripts/sapiens_segment.py \
        --checkpoint /path/sapiens_0.6b_..._torchscript.pt2 \
        --manifest manifest.json \
        --output-dir results/sapiens_labels \
        --merge coarse \
        --sapiens-demo-dir /path/sapiens/lite/demo
"""

import argparse
import importlib.util
import json
import os
import sys

import cv2
import numpy as np
import torch
import torch.nn.functional as F

MEAN = torch.tensor([123.5, 116.5, 103.5]).view(1, 3, 1, 1)
STD = torch.tensor([58.5, 57.0, 57.5]).view(1, 3, 1, 1)


def _load_converter():
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "..", "hpsv3", "xai", "sapiens_segments.py")
    spec = importlib.util.spec_from_file_location("sapiens_segments", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def load_model(ckpt: str, device: str):
    """torchscript (float32) or torch.export (bfloat16), detected by filename."""
    if "_torchscript" in os.path.basename(ckpt) or ckpt.endswith(".torchscript"):
        model, dtype = torch.jit.load(ckpt), torch.float32
    else:
        try:
            model, dtype = torch.jit.load(ckpt), torch.float32
        except Exception:
            model, dtype = torch.export.load(ckpt).module(), torch.bfloat16
    return model.eval().to(device), dtype


@torch.inference_mode()
def segment(model, dtype, image_bgr, device, shape=(1024, 768)):
    H0, W0 = image_bgr.shape[:2]
    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (shape[1], shape[0]), interpolation=cv2.INTER_LINEAR)
    t = torch.from_numpy(resized).permute(2, 0, 1).unsqueeze(0).float()
    t = (t - MEAN) / STD
    t = t.to(device=device, dtype=dtype)
    logits = model(t).float()                      # [1, C, h, w]
    logits = F.interpolate(logits, size=shape, mode="bilinear", align_corners=False)
    pred = logits.argmax(dim=1)[0].to(torch.int32).cpu().numpy()  # [H,W] at model res
    return cv2.resize(pred, (W0, H0), interpolation=cv2.INTER_NEAREST).astype(np.int64)


def get_classes(demo_dir):
    if demo_dir and os.path.isdir(demo_dir):
        sys.path.insert(0, demo_dir)
        try:
            from classes_and_palettes import GOLIATH_CLASSES  # type: ignore
            print(f"[sapiens] loaded {len(GOLIATH_CLASSES)} class names from {demo_dir}")
            return tuple(GOLIATH_CLASSES)
        except Exception as e:
            print(f"[warn] could not import GOLIATH_CLASSES from {demo_dir}: {e}")
    return None


def stem_of(path):
    return os.path.splitext(os.path.basename(path))[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", required=True, help="Sapiens-lite seg .pt2 checkpoint.")
    ap.add_argument("--manifest", default=None, help="JSON list of {\"image\": ...}.")
    ap.add_argument("--images", nargs="*", default=[], help="Explicit image paths.")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--merge", choices=["coarse", "fine"], default="coarse")
    ap.add_argument("--shape", type=int, nargs=2, default=[1024, 768], help="H W")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--sapiens-demo-dir", default=None,
                    help="Path to sapiens/lite/demo to read exact class names.")
    args = ap.parse_args()

    conv = _load_converter()
    classes = get_classes(args.sapiens_demo_dir) or conv.GOLIATH_CLASSES
    print(f"[sapiens] classes={len(classes)}  merge={args.merge}")

    imgs = list(args.images)
    if args.manifest:
        imgs += [it["image"] for it in json.load(open(args.manifest))]
    imgs = [p for p in imgs if os.path.exists(p)]
    if not imgs:
        raise SystemExit("No images to segment.")

    model, dtype = load_model(args.checkpoint, args.device)
    print(f"[sapiens] model loaded ({dtype}); segmenting {len(imgs)} images")
    os.makedirs(args.output_dir, exist_ok=True)

    for p in imgs:
        bgr = cv2.imread(p)
        if bgr is None:
            print(f"[skip] unreadable: {p}")
            continue
        seg = segment(model, dtype, bgr, args.device, tuple(args.shape))
        labels, id2name = conv.seg_to_labels(seg, class_names=classes, merge=args.merge)
        conv.save_labels(stem_of(p), args.output_dir, labels, id2name)
        print(f"[ok] {stem_of(p)}: {len(id2name)} regions -> {sorted(set(id2name.values()))}")

    print(f"[done] label maps in {args.output_dir}")


if __name__ == "__main__":
    main()
