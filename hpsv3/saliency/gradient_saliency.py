"""Gradient-based saliency maps for the HPSv3 reward model.

HPSv3 scores how well an image matches a prompt, outputting two values per sample:
``mu`` (the preference score, ``logits[:, 0]``) and ``sigma`` (an uncertainty /
variance head, ``logits[:, 1]``). This module explains which input regions drive
either output head, via three gradient-based attribution methods:

- ``vanilla``               -- raw input gradients ``| d target / d x |`` (channel-max).
- ``integrated_gradients``  -- path-integrated gradients from a black baseline; defeats
                               gradient saturation, pixel resolution.
- ``grad_cam``              -- Grad-CAM on the vision tower's merged patch features;
                               a coarse, object-focused heatmap (~16x16 upsampled).
- ``attention_rollout``     -- composes the LLM decoder's self-attention across layers
                               to trace how the <|Reward|> token routes to image tokens;
                               gradient-free, saturation-immune, head-agnostic.

All methods reuse the model's *differentiable* image processor (enabled via
``HPSv3RewardInferencer(differentiable=True)``), so gradients flow from the chosen
scalar output back to the raw RGB pixels.

Run:

    python -m hpsv3.saliency.gradient_saliency \\
        --checkpoint_path=/gpfs/scratch1/shared/scur0077/models/HPSv3/HPSv3.safetensors \\
        --output_dir=/gpfs/scratch1/shared/scur0077/saliency \\
        --methods=vanilla,integrated_gradients,grad_cam \\
        --target=both
"""

import json
import os
from pathlib import Path

import fire
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image

from hpsv3.inference import HPSv3RewardInferencer

REPO_ROOT = Path(__file__).resolve().parents[2]

# Default test samples: the repo's example images with the prompt used in
# hpsv3/inference.py's __main__.
_DEFAULT_PROMPT = (
    "cute chibi anime cartoon fox, smiling wagging tail with a small cartoon heart "
    "above sticker"
)
DEFAULT_IMAGES = [
    str(REPO_ROOT / "assets" / "example1.png"),
    str(REPO_ROOT / "assets" / "example2.png"),
]
DEFAULT_PROMPTS = [_DEFAULT_PROMPT, _DEFAULT_PROMPT]

# logits column -> output head name
_TARGET_COLUMN = {"mu": 0, "sigma": 1}
METHODS = ("vanilla", "integrated_gradients", "grad_cam", "attention_rollout")


class SaliencyMapper:
    """Compute and visualize gradient-based saliency maps for HPSv3."""

    def __init__(self, checkpoint_path=None, device="cuda"):
        self.device = device
        self.inferencer = HPSv3RewardInferencer(
            checkpoint_path=checkpoint_path, device=device, differentiable=True
        )
        self.model = self.inferencer.model
        # Saliency differentiates the output w.r.t. the input pixels only, never the
        # weights. Freeze every parameter so autograd does not allocate ~16 GB of
        # parameter-gradient buffers (and can drop activations only needed for them).
        # The input-pixel gradient (the saliency map) is unchanged: autograd still
        # backpropagates through the frozen weights to reach pixel_values, which
        # requires grad. Without this the full forward+backward OOMs a 40 GB A100.
        for p in self.model.parameters():
            p.requires_grad_(False)
        # The differentiable patchifier lives on the image_processor (see
        # hpsv3/train.py: processor.image_processor = Qwen2VLImageProcessor()).
        self.image_processor = self.inferencer.processor.image_processor
        self.patch_size = self.image_processor.patch_size  # 14
        self.merge_size = self.image_processor.merge_size  # 2
        # Token ids used by attention_rollout: the <|Reward|> token is the readout
        # position (config reward_token="special"); image tokens are the rollout cols.
        self.image_token_id = self.model.config.image_token_id
        self.reward_token_id = self.inferencer.processor.tokenizer.convert_tokens_to_ids(
            "<|Reward|>"
        )

    # ------------------------------------------------------------------ helpers

    def _build_batch(self, image_path, prompt):
        """Build the text/grid batch from the PIL path.

        Fixes the image-token count via ``image_grid_thw``; we later swap in
        differentiable ``pixel_values`` that share the same grid. Returns the batch
        and the (full, unmerged) patch grid ``(grid_h, grid_w)``.
        """
        batch = self.inferencer.prepare_batch([image_path], [prompt])
        _, grid_h, grid_w = (int(v) for v in batch["image_grid_thw"][0].tolist())
        return batch, grid_h, grid_w

    def _load_resized_leaf(self, image_path, grid_h, grid_w, requires_grad=True):
        """Load ``image_path`` resized to exactly (grid_h*14, grid_w*14) px.

        Returns a float32 tensor in [0, 1] of shape [1, 3, H, W].
        """
        target_h = grid_h * self.patch_size
        target_w = grid_w * self.patch_size
        pil = Image.open(image_path).convert("RGB")
        # PIL.resize takes (width, height); BICUBIC matches the processor's resample.
        pil = pil.resize((target_w, target_h), Image.BICUBIC)
        arr = np.asarray(pil, dtype=np.float32) / 255.0  # [H, W, 3] in [0, 1]
        img = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to(self.device)
        if requires_grad:
            img = img.requires_grad_(True)
        return img

    def _logits_from_leaf(self, batch, img01):
        """Patchify ``img01`` differentiably, swap it into ``batch``, return logits.

        ``img01`` is a [1,3,H,W] tensor in [0,1] that requires grad. The returned
        ``logits`` row carries grad back to it.
        """
        diff = self.image_processor.preprocess_tensor(img01 * 255.0, do_resize=False)[
            "pixel_values"
        ]
        ref_pv = batch["pixel_values"]
        batch = dict(batch)
        batch["pixel_values"] = diff.to(device=ref_pv.device, dtype=ref_pv.dtype)
        return self.model(return_dict=True, **batch)["logits"]

    @staticmethod
    def _normalize(sal, percentile):
        vmax = np.percentile(sal, percentile)
        if vmax <= 0:
            vmax = sal.max()
        if vmax <= 0:
            return np.zeros_like(sal)
        return np.clip(sal / vmax, 0.0, 1.0)

    # ------------------------------------------------------------------ methods

    def _vanilla(self, batch, image_path, grid_h, grid_w, head, percentile, **_):
        """Raw input gradient, max over color channels."""
        img01 = self._load_resized_leaf(image_path, grid_h, grid_w)
        logits = self._logits_from_leaf(batch, img01)
        logits[0, _TARGET_COLUMN[head]].backward()
        grad = img01.grad.detach()  # [1, 3, H, W]
        sal = grad.abs().amax(dim=1)[0].float().cpu().numpy()  # [H, W]
        return self._normalize(sal, percentile), self._detach_rgb(img01)

    def _integrated_gradients(
        self, batch, image_path, grid_h, grid_w, head, percentile, ig_steps=32, **_
    ):
        """Integrated Gradients from a black baseline (Riemann midpoint sum)."""
        img01 = self._load_resized_leaf(image_path, grid_h, grid_w, requires_grad=False)
        baseline = torch.zeros_like(img01)
        total_grad = torch.zeros_like(img01)
        # Midpoint rule: alpha = (i + 0.5) / steps avoids the saturated endpoints.
        for i in range(ig_steps):
            alpha = (i + 0.5) / ig_steps
            interp = (baseline + alpha * (img01 - baseline)).requires_grad_(True)
            logits = self._logits_from_leaf(batch, interp)
            logits[0, _TARGET_COLUMN[head]].backward()
            total_grad += interp.grad.detach()
        avg_grad = total_grad / ig_steps
        ig = (img01 - baseline) * avg_grad  # [1, 3, H, W]
        # Sum signed attributions over channels, take magnitude.
        sal = ig.sum(dim=1)[0].abs().float().cpu().numpy()  # [H, W]
        return self._normalize(sal, percentile), self._detach_rgb(img01)

    def _grad_cam(self, batch, image_path, grid_h, grid_w, head, percentile, **_):
        """Grad-CAM on the vision tower's merged patch embeddings."""
        img01 = self._load_resized_leaf(image_path, grid_h, grid_w)

        captured = {}

        def hook(_module, _inputs, output):
            # output: [num_merged_tokens, llm_hidden]; non-leaf, requires grad.
            output.retain_grad()
            captured["A"] = output

        handle = self.model.visual.register_forward_hook(hook)
        try:
            logits = self._logits_from_leaf(batch, img01)
            logits[0, _TARGET_COLUMN[head]].backward()
        finally:
            handle.remove()

        A = captured["A"].detach().float()  # [N, C]
        grads = captured["A"].grad.detach().float()  # [N, C]
        weights = grads.mean(dim=0)  # [C] global-average-pool over tokens
        cam = F.relu((A * weights).sum(dim=-1))  # [N]

        # Merged grid is row-major over (grid_h//2, grid_w//2).
        mh, mw = grid_h // self.merge_size, grid_w // self.merge_size
        cam = cam.reshape(1, 1, mh, mw)
        H, W = img01.shape[-2], img01.shape[-1]
        cam = F.interpolate(cam, size=(H, W), mode="bilinear", align_corners=False)
        sal = cam[0, 0].cpu().numpy()  # [H, W]
        return self._normalize(sal, percentile), self._detach_rgb(img01)

    def _attention_rollout(self, batch, image_path, grid_h, grid_w, head, percentile, **_):
        """Attention rollout (Abnar & Zuidema, 2020).

        Composes the LLM decoder's self-attention across all layers (with a residual
        term) to estimate how much the <|Reward|> readout token depends on each image
        token, then maps that to the image grid. Gradient-free and saturation-immune,
        so it is **head-agnostic** (the same map for mu and sigma).
        """
        img01 = self._load_resized_leaf(image_path, grid_h, grid_w, requires_grad=False)

        captured = {}

        def hook(_module, _inputs, output):
            captured["attentions"] = output.attentions

        # self.model is the reward model; self.model.model is the Qwen2VL decoder.
        handle = self.model.model.register_forward_hook(hook)
        try:
            with torch.no_grad():
                diff = self.image_processor.preprocess_tensor(
                    img01 * 255.0, do_resize=False
                )["pixel_values"]
                ref_pv = batch["pixel_values"]
                b = dict(batch)
                b["pixel_values"] = diff.to(device=ref_pv.device, dtype=ref_pv.dtype)
                self.model(return_dict=True, output_attentions=True, **b)
        finally:
            handle.remove()

        attns = captured.get("attentions")
        if not attns or attns[0] is None:
            raise RuntimeError(
                "Decoder returned no attentions; the SDPA kernel did not fall back to "
                "eager. Load the model with attn_implementation='eager' for rollout."
            )

        L = attns[0].shape[-1]
        device = attns[0].device
        eye = torch.eye(L, device=device, dtype=torch.float32)
        rollout = eye.clone()
        for A in attns:
            a = A[0].mean(dim=0).float()  # avg heads -> [L, L]
            a = 0.5 * a + 0.5 * eye      # residual connection
            a = a / a.sum(dim=-1, keepdim=True)
            rollout = a @ rollout         # R = A_L @ ... @ A_1

        input_ids = batch["input_ids"][0]
        reward_pos = int((input_ids == self.reward_token_id).nonzero().flatten()[-1])
        image_pos = (input_ids == self.image_token_id).nonzero().flatten()
        flow = rollout[reward_pos, image_pos]  # [num_image_tokens]

        mh, mw = grid_h // self.merge_size, grid_w // self.merge_size
        if flow.numel() != mh * mw:
            raise RuntimeError(
                f"Got {flow.numel()} image tokens but expected {mh * mw} (={mh}x{mw})."
            )
        cam = flow.reshape(1, 1, mh, mw)
        H, W = img01.shape[-2], img01.shape[-1]
        cam = F.interpolate(cam, size=(H, W), mode="bilinear", align_corners=False)
        sal = cam[0, 0].cpu().numpy()
        return self._normalize(sal, percentile), self._detach_rgb(img01)

    _DISPATCH = {
        "vanilla": _vanilla,
        "integrated_gradients": _integrated_gradients,
        "grad_cam": _grad_cam,
        "attention_rollout": _attention_rollout,
    }

    @staticmethod
    def _detach_rgb(img01):
        return img01.detach()[0].permute(1, 2, 0).cpu().numpy()

    # ------------------------------------------------------------------ public

    def compute(
        self, image_path, prompt, method="vanilla", target="mu", percentile=99.0,
        ig_steps=32,
    ):
        """Compute saliency map(s) for one (image, prompt) with one method.

        Args:
            method: one of vanilla, integrated_gradients, grad_cam, attention_rollout.
            target: "mu", "sigma", or "both". (attention_rollout is head-agnostic.)

        Returns a dict with ``mu``, ``sigma`` (floats), ``resized_rgb`` ([H,W,3] in
        [0,1]) and ``saliency`` (dict head -> normalized [H,W] array in [0,1]).
        """
        if method not in self._DISPATCH:
            raise ValueError(f"Unknown method {method!r}; expected one of {METHODS}.")
        heads = ["mu", "sigma"] if target == "both" else [target]
        for h in heads:
            if h not in _TARGET_COLUMN:
                raise ValueError(f"Unknown target {h!r}; expected mu, sigma, or both.")

        fn = self._DISPATCH[method]
        batch, grid_h, grid_w = self._build_batch(image_path, prompt)

        # mu/sigma reported once (detached) from a plain forward.
        with torch.no_grad():
            ref = self._load_resized_leaf(image_path, grid_h, grid_w, requires_grad=False)
            logits = self._logits_from_leaf(batch, ref)
            mu = float(logits[0, _TARGET_COLUMN["mu"]].cpu())
            sigma = float(logits[0, _TARGET_COLUMN["sigma"]].cpu())

        saliency, resized_rgb = {}, None
        for h in heads:
            with torch.enable_grad():
                sal, resized_rgb = fn(
                    self, batch, image_path, grid_h, grid_w, h, percentile,
                    ig_steps=ig_steps,
                )
            saliency[h] = sal

        return {
            "mu": mu,
            "sigma": sigma,
            "resized_rgb": resized_rgb,
            "saliency": saliency,
        }

    @staticmethod
    def visualize(result, head, method, out_path, title):
        """Save a 1x3 panel: original | saliency heatmap | overlay."""
        rgb = result["resized_rgb"]
        sal = result["saliency"][head]

        fig, axes = plt.subplots(1, 3, figsize=(15, 5))
        axes[0].imshow(rgb)
        axes[0].set_title("input (resized)")
        axes[1].imshow(sal, cmap="inferno")
        axes[1].set_title(f"{method} | {head}")
        axes[2].imshow(rgb)
        axes[2].imshow(sal, cmap="inferno", alpha=0.5)
        axes[2].set_title("overlay")
        for ax in axes:
            ax.axis("off")
        fig.suptitle(title)
        fig.tight_layout()
        fig.savefig(out_path, dpi=120, bbox_inches="tight")
        plt.close(fig)


# Backwards-compatible alias for the v1 class name.
GradientSaliency = SaliencyMapper


def _load_samples_from_csv(csv_path):
    """Read (image, prompt) samples from a CSV.

    Requires an ``image_path`` (or ``image``) column and an optional ``prompt``
    column. If ``feature_idx`` / ``img_rank`` columns are present (as in the SAE
    ``top_images_per_feature.csv``), they are used to group outputs into per-feature
    subdirectories and to prefix filenames, preserving provenance.
    """
    import csv as _csv

    samples = []
    with open(csv_path, newline="") as f:
        for row in _csv.DictReader(f):
            image = (row.get("image_path") or row.get("image") or "").strip()
            if not image:
                continue
            feature_idx = row.get("feature_idx")
            img_rank = row.get("img_rank")
            subdir = ""
            if feature_idx not in (None, ""):
                subdir = f"feature_{int(float(feature_idx)):05d}"
            prefix = []
            if img_rank not in (None, ""):
                prefix.append(f"rank{int(float(img_rank))}")
            prefix.append(Path(image).stem)
            samples.append(
                {
                    "image": image,
                    "prompt": row.get("prompt", "").strip() or _DEFAULT_PROMPT,
                    "subdir": subdir,
                    "prefix": "_".join(prefix),
                    "feature_idx": int(float(feature_idx))
                    if feature_idx not in (None, "")
                    else None,
                    "img_rank": int(float(img_rank))
                    if img_rank not in (None, "")
                    else None,
                }
            )
    return samples


def main(
    checkpoint_path="/gpfs/scratch1/shared/scur0077/models/HPSv3/HPSv3.safetensors",
    output_dir="/gpfs/scratch1/shared/scur0077/saliency",
    csv_path=None,
    images=None,
    prompts=None,
    methods="vanilla,integrated_gradients,grad_cam,attention_rollout",
    target="mu",
    ig_steps=32,
    percentile=99.0,
    device="cuda",
):
    """Generate gradient-based saliency panels for (image, prompt) samples.

    Args:
        csv_path: optional CSV with image_path[,prompt[,feature_idx,img_rank]] columns;
            overrides --images/--prompts and groups outputs per feature.
        methods: comma-separated subset of
            {vanilla, integrated_gradients, grad_cam, attention_rollout}.
        target: which output head to explain -- "mu" (preference score),
            "sigma" (uncertainty/variance), or "both".
        ig_steps: number of integration steps for integrated_gradients.
    """
    if csv_path is not None:
        samples = _load_samples_from_csv(csv_path)
        if not samples:
            raise ValueError(f"No usable rows found in {csv_path}.")
    else:
        imgs = list(images) if images is not None else DEFAULT_IMAGES
        if prompts is None:
            prmpts = DEFAULT_PROMPTS if imgs is DEFAULT_IMAGES else [_DEFAULT_PROMPT] * len(imgs)
        else:
            prmpts = list(prompts)
        if len(imgs) != len(prmpts):
            raise ValueError(
                f"Got {len(imgs)} images but {len(prmpts)} prompts; counts must match."
            )
        samples = [
            {"image": im, "prompt": pr, "subdir": "", "prefix": Path(im).stem,
             "feature_idx": None, "img_rank": None}
            for im, pr in zip(imgs, prmpts)
        ]

    if isinstance(methods, str):
        method_list = [m.strip() for m in methods.split(",") if m.strip()]
    else:
        method_list = list(methods)
    for m in method_list:
        if m not in METHODS:
            raise ValueError(f"Unknown method {m!r}; expected subset of {METHODS}.")

    heads = ["mu", "sigma"] if target == "both" else [target]
    os.makedirs(output_dir, exist_ok=True)

    mapper = SaliencyMapper(checkpoint_path=checkpoint_path, device=device)

    summary = []
    n = len(samples)
    for idx, sample in enumerate(samples):
        image_path, prompt = sample["image"], sample["prompt"]
        sample_dir = os.path.join(output_dir, sample["subdir"]) if sample["subdir"] else output_dir
        os.makedirs(sample_dir, exist_ok=True)
        prefix = sample["prefix"]

        outputs = {}
        mu = sigma = None
        for method in method_list:
            result = mapper.compute(
                image_path, prompt, method=method, target=target,
                percentile=percentile, ig_steps=ig_steps,
            )
            mu, sigma = result["mu"], result["sigma"]
            outputs[method] = {}
            for head in heads:
                out_path = os.path.join(sample_dir, f"{prefix}_{head}_{method}.png")
                title = f"{prefix} | mu={mu:.3f} sigma={sigma:.3f} | {method} | {head}"
                mapper.visualize(result, head, method, out_path, title)
                outputs[method][head] = out_path

        print(f"[{idx + 1}/{n}] {prefix}: mu={mu:.4f} sigma={sigma:.4f}")
        summary.append(
            {
                "image": image_path,
                "prompt": prompt,
                "feature_idx": sample["feature_idx"],
                "img_rank": sample["img_rank"],
                "mu": mu,
                "sigma": sigma,
                "target": target,
                "methods": method_list,
                "outputs": outputs,
            }
        )

    summary_path = os.path.join(output_dir, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Wrote summary for {len(summary)} samples to {summary_path}")


if __name__ == "__main__":
    fire.Fire(main)
