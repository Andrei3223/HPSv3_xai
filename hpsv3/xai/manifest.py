"""
Shared (image, prompt) manifest builder.

Pure stdlib (os/json/glob) — importable with or without torch, so both the HPSv3
attribution driver and the standalone Sapiens segmentation script can produce the
*same* image list (matching filenames => matching label maps).
"""

from __future__ import annotations

import json
import os
from typing import List, Optional

_FOX_PROMPT = (
    "cute chibi anime cartoon fox, smiling wagging tail with a small cartoon "
    "heart above sticker"
)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_ASSETS = [
    {"image": os.path.join(_REPO_ROOT, "assets/example1.png"), "prompt": _FOX_PROMPT},
    {"image": os.path.join(_REPO_ROOT, "assets/example2.png"), "prompt": _FOX_PROMPT},
]


def sample_from_hpdv3(d: str, n: int) -> List[dict]:
    """Best-effort: pull n (image, prompt) pairs from an HPDv3 json manifest."""
    for name in ("test.json", "train.json", "all.json"):
        p = os.path.join(d, name)
        if not os.path.exists(p):
            continue
        try:
            data = json.load(open(p))
            out = []
            for e in data:
                rel = e.get("path1") or e.get("path")
                if not rel:
                    continue
                img = rel if os.path.isabs(rel) else os.path.join(d, rel)
                if os.path.exists(img) and e.get("prompt"):
                    out.append({"image": img, "prompt": e["prompt"]})
                if len(out) >= n:
                    break
            if out:
                print(f"[manifest] sampled {len(out)} pairs from {p}")
                return out
        except Exception as ex:  # pragma: no cover
            print(f"[manifest] could not parse {p}: {ex}")
    print(f"[manifest] no usable json found in {d}; assets only.")
    return []


def build_manifest(
    manifest: Optional[str] = None,
    hpdv3_dir: Optional[str] = None,
    num_dataset: int = 0,
    include_assets: bool = True,
) -> List[dict]:
    """Return a list of {"image", "prompt"} dicts; only images that exist are kept."""
    if manifest:
        items = json.load(open(manifest))
        print(f"[manifest] loaded {len(items)} pairs from {manifest}")
    else:
        items = list(DEFAULT_ASSETS) if include_assets else []
        if hpdv3_dir and num_dataset > 0:
            items += sample_from_hpdv3(hpdv3_dir, num_dataset)
    return [it for it in items if os.path.exists(it["image"])]
