#!/usr/bin/env python
"""
Stage 0 for LANDSCAPES / scenes: semantic segmentation with SegFormer (ADE20K, 150
classes) -> region label maps for the HPSv3 attribution pipeline.

Runs in the HPSv3 venv (uses `transformers`, already a dependency) -- no new env.
It writes the SAME file format the attribution driver already consumes, so you then
run the normal job with `--segments sapiens --sapiens-dir <this output dir>`
(the loader is region-source agnostic; the name "sapiens" is just the flag).

Coarse scene grouping collapses ADE20K's 150 classes into readable buckets:
sky, vegetation, water, ground, building, mountain, person, furniture, vehicle, other.

Example:
    python scripts/scene_segment.py \
        --manifest manifest.json \
        --output-dir results/scene_labels \
        --merge coarse --device cuda
"""

import argparse
import importlib.util
import json
import os

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image
from transformers import SegformerForSemanticSegmentation, SegformerImageProcessor

_SCENE_RULES = [
    ("sky", ["sky"]),
    ("water", ["water", "sea", "river", "lake", "pool", "waterfall"]),
    ("vegetation", ["tree", "grass", "plant", "flower", "palm", "field"]),
    ("mountain", ["mountain", "rock", "hill", "stone"]),
    ("ground", ["earth", "sand", "road", "sidewalk", "path", "ground", "floor", "dirt"]),
    ("building", ["building", "house", "wall", "skyscraper", "hovel", "tower", "fence", "bridge"]),
    ("person", ["person"]),
    ("vehicle", ["car", "truck", "bus", "boat", "van", "bicycle", "airplane"]),
    ("furniture", ["chair", "table", "sofa", "bed", "cabinet", "lamp", "cushion"]),
]


def scene_group(name):
    n = name.lower()
    for g, keys in _SCENE_RULES:
        if any(k in n for k in keys):
            return g
    return "other"


def _load_converter():
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "..", "hpsv3", "xai", "sapiens_segments.py")
    spec = importlib.util.spec_from_file_location("sapiens_segments", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def stem_of(path):
    return os.path.splitext(os.path.basename(path))[0]


@torch.inference_mode()
def segment(model, proc, pil, device):
    H, W = pil.size[1], pil.size[0]
    inputs = proc(images=pil, return_tensors="pt").to(device)
    logits = model(**inputs).logits  # [1, C, h/4, w/4]
    up = F.interpolate(logits, size=(H, W), mode="bilinear", align_corners=False)
    return up.argmax(dim=1)[0].cpu().numpy().astype(np.int64)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--images", nargs="*", default=[])
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--model", default="nvidia/segformer-b2-finetuned-ade-512-512")
    ap.add_argument("--merge", choices=["coarse", "fine"], default="coarse")
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    conv = _load_converter()
    proc = SegformerImageProcessor.from_pretrained(args.model)
    model = SegformerForSemanticSegmentation.from_pretrained(args.model).to(args.device).eval()
    id2label = model.config.id2label
    n_classes = max(id2label) + 1
    class_names = [id2label.get(i, f"class_{i}") for i in range(n_classes)]
    print(f"[scene] {args.model}: {n_classes} ADE20K classes, merge={args.merge}")

    imgs = list(args.images)
    if args.manifest:
        imgs += [it["image"] for it in json.load(open(args.manifest))]
    imgs = [p for p in imgs if os.path.exists(p)]
    if not imgs:
        raise SystemExit("No images to segment.")
    os.makedirs(args.output_dir, exist_ok=True)

    for p in imgs:
        pil = Image.open(p).convert("RGB")
        seg = segment(model, proc, pil, args.device)
        if args.merge == "coarse":
            # remap each ADE class id -> scene group, then to contiguous labels
            name_of = {int(c): scene_group(class_names[c]) for c in np.unique(seg)}
            groups = sorted(set(name_of.values()))
            gid = {g: i for i, g in enumerate(groups)}
            labels = np.zeros_like(seg)
            for c, g in name_of.items():
                labels[seg == c] = gid[g]
            id2name = {i: g for g, i in gid.items()}
        else:
            labels, id2name = conv.seg_to_labels(seg, class_names=class_names, merge="fine")
        conv.save_labels(stem_of(p), args.output_dir, labels, id2name)
        print(f"[ok] {stem_of(p)}: {sorted(set(id2name.values()))}")

    print(f"[done] scene label maps in {args.output_dir}")


if __name__ == "__main__":
    main()
