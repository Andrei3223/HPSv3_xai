"""
Convert Sapiens body-part segmentation maps into label maps for perturbation
attribution. Pure numpy/cv2 (no torch) so it can run in any environment.

A Sapiens segmentation output is an HxW array of class indices (0..27 of the
Goliath taxonomy). This module turns it into the contiguous `labels` map the
perturbation pipeline expects, plus an id->part-name mapping, with an optional
coarse grouping of the fine classes into interpretable body parts.
"""

from __future__ import annotations

import json
import os
from typing import Dict, Optional, Sequence, Tuple

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

# Best-known Goliath 28-class order (index 0 = Background). The model's actual
# order is the single source of truth: pass `class_names` from the installed
# sapiens repo (classes_and_palettes.GOLIATH_CLASSES) when available.
GOLIATH_CLASSES: Tuple[str, ...] = (
    "Background", "Apparel", "Face_Neck", "Hair", "Left_Foot", "Left_Hand",
    "Left_Lower_Arm", "Left_Lower_Leg", "Left_Shoe", "Left_Sock", "Left_Upper_Arm",
    "Left_Upper_Leg", "Lower_Clothing", "Right_Foot", "Right_Hand", "Right_Lower_Arm",
    "Right_Lower_Leg", "Right_Shoe", "Right_Sock", "Right_Upper_Arm", "Right_Upper_Leg",
    "Torso", "Upper_Clothing", "Lower_Lip", "Upper_Lip", "Lower_Teeth", "Upper_Teeth",
    "Tongue",
)

# Coarse grouping by substring (first match wins). Robust to class-order changes.
_COARSE_RULES = [
    ("background", ["background"]),
    ("face", ["face", "neck", "lip", "teeth", "tongue", "mouth"]),
    ("hair", ["hair"]),
    ("hands", ["hand"]),
    ("arms", ["arm"]),
    ("legs", ["leg"]),
    ("feet", ["foot", "shoe", "sock"]),
    ("torso", ["torso"]),
    ("upper_clothing", ["upper_clothing"]),
    ("lower_clothing", ["lower_clothing", "apparel", "spandex"]),
]


def coarse_group(name: str) -> str:
    n = name.lower()
    for group, keys in _COARSE_RULES:
        if any(k in n for k in keys):
            return group
    return "other"


def resize_seg(seg: np.ndarray, target_hw: Tuple[int, int]) -> np.ndarray:
    """Nearest-neighbour resize of a class-index map to (H, W)."""
    h, w = target_hw
    if seg.shape[:2] == (h, w):
        return seg.astype(np.int64)
    if cv2 is None:
        raise ImportError("opencv-python is required to resize segmentation maps.")
    return cv2.resize(seg.astype(np.int32), (w, h), interpolation=cv2.INTER_NEAREST).astype(np.int64)


def seg_to_labels(
    seg: np.ndarray,
    class_names: Sequence[str] = GOLIATH_CLASSES,
    merge: str = "coarse",
    target_hw: Optional[Tuple[int, int]] = None,
) -> Tuple[np.ndarray, Dict[int, str]]:
    """
    seg: HxW int class-index map from Sapiens.
    merge="coarse": collapse fine classes into body-part groups (face, hair, ...).
    merge="fine":   keep each present Goliath class as its own region.
    Returns (labels, id2name) with labels contiguous over the PRESENT regions.
    """
    if target_hw is not None:
        seg = resize_seg(seg, target_hw)
    seg = seg.astype(np.int64)

    def _name(ci: int) -> str:
        return class_names[ci] if 0 <= ci < len(class_names) else f"class_{ci}"

    if merge == "coarse":
        name_of = {int(ci): coarse_group(_name(int(ci))) for ci in np.unique(seg)}
        groups = sorted(set(name_of.values()))
        gid = {g: i for i, g in enumerate(groups)}
        labels = np.zeros_like(seg)
        for ci, g in name_of.items():
            labels[seg == ci] = gid[g]
        return labels, {i: g for g, i in gid.items()}

    present = sorted(int(c) for c in np.unique(seg))
    remap = {c: i for i, c in enumerate(present)}
    labels = np.zeros_like(seg)
    for c, i in remap.items():
        labels[seg == c] = i
    return labels, {i: _name(c) for c, i in remap.items()}


def save_labels(stem: str, out_dir: str, labels: np.ndarray, id2name: Dict[int, str]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    np.save(os.path.join(out_dir, f"{stem}_sapiens_labels.npy"), labels.astype(np.int64))
    with open(os.path.join(out_dir, f"{stem}_parts.json"), "w") as f:
        json.dump({str(k): v for k, v in id2name.items()}, f, indent=2)


def load_labels(stem: str, in_dir: str) -> Tuple[np.ndarray, Dict[int, str]]:
    labels = np.load(os.path.join(in_dir, f"{stem}_sapiens_labels.npy"))
    with open(os.path.join(in_dir, f"{stem}_parts.json")) as f:
        id2name = {int(k): v for k, v in json.load(f).items()}
    return labels, id2name
