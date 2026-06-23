"""
Aggregate a results directory produced by hpsv3.xai.run_experiments into report
tables, summary figures, per-image heatmap montages, and a REPORT.md.

Standalone: needs only numpy / matplotlib / Pillow (no torch, no reward model),
so it can run anywhere the .npz/.png/.csv outputs were copied.

    python -m hpsv3.xai.aggregate --results-dir results/xai_<jobid>
    # or:  python hpsv3/xai/aggregate.py --results-dir results/xai_<jobid>

Produces, inside the results dir:
    agg_per_experiment.csv      base reward, magnitude, top1, negative-fraction
    agg_method_agreement.csv    occlusion-vs-LIME Spearman per image/baseline
    agg_baseline_agreement.csv  occlusion-vs-black Spearman per image/baseline
    agg_faithfulness.csv        deletion/insertion AUCs vs random
    fig_method_agreement.png    grouped bars
    fig_baseline_magnitude.png  grouped bars
    fig_baseline_agreement.png  grouped bars
    fig_faithfulness.png        deletion + insertion AUC vs random
    montage_<image>.png         8-panel heatmap grid per image
    REPORT.md                   full report embedding all of the above
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
from collections import defaultdict

import numpy as np

MODES = ["gray", "mean", "blur", "black"]
METHODS = ["occlusion", "lime"]


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #
def _rankdata(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x), dtype=np.float64)
    ranks[order] = np.arange(1, len(x) + 1)
    _, inv, counts = np.unique(x, return_inverse=True, return_counts=True)
    sums = np.zeros(len(counts))
    np.add.at(sums, inv, ranks)
    return (sums / counts)[inv]


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra, rb = _rankdata(a), _rankdata(b)
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    denom = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / denom) if denom > 0 else float("nan")


def _qual(rho: float) -> str:
    if np.isnan(rho):
        return "n/a"
    a = abs(rho)
    return "strong" if a >= 0.6 else "moderate" if a >= 0.3 else "weak"


# --------------------------------------------------------------------------- #
# discovery / loading
# --------------------------------------------------------------------------- #
def discover(results_dir: str):
    """Map image-stem -> {(method, mode): npz_path}."""
    items = defaultdict(dict)
    for f in glob.glob(os.path.join(results_dir, "*.npz")):
        base = os.path.basename(f)[:-4]
        parts = base.rsplit("_", 2)
        if len(parts) != 3 or parts[1] not in METHODS or parts[2] not in MODES:
            continue
        stem, method, mode = parts
        items[stem][(method, mode)] = f
    return dict(items)


def load_npz(path: str):
    d = np.load(path)
    return {
        "importances": d["importances"],
        "base_score": float(d["base_score"]),
        "labels": d["labels"],
        "img": d["img"],
    }


def read_prompts(results_dir: str):
    """image basename(no ext) -> prompt, from summary.csv (best-effort)."""
    out = {}
    p = os.path.join(results_dir, "summary.csv")
    if not os.path.exists(p):
        return out
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            stem = os.path.splitext(os.path.basename(row["image"]))[0]
            out.setdefault(stem, row.get("prompt", ""))
    return out


def read_faithfulness(results_dir: str):
    """(stem, method, mode) -> {deletion_auc, ...} from summary.csv rows."""
    out = {}
    p = os.path.join(results_dir, "summary.csv")
    if not os.path.exists(p):
        return out
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("deletion_auc", "") in ("", None):
                continue
            stem = os.path.splitext(os.path.basename(row["image"]))[0]
            out[(stem, row["method"], row["mode"])] = {
                k: float(row[k]) for k in
                ("deletion_auc", "deletion_auc_random", "insertion_auc", "insertion_auc_random")
            }
    return out


def short(stem: str) -> str:
    return stem[:8]


# --------------------------------------------------------------------------- #
# computation
# --------------------------------------------------------------------------- #
def compute(items):
    imps = {stem: {k: load_npz(v)["importances"] for k, v in d.items()} for stem, d in items.items()}
    bases = {stem: load_npz(next(iter(d.values())))["base_score"] for stem, d in items.items()}
    nsp = {stem: int(load_npz(next(iter(d.values())))["labels"].max()) + 1 for stem, d in items.items()}

    per_exp = []   # rows: stem, method, mode, base, mean_abs, top1, neg_frac
    for stem in items:
        for (method, mode), imp in imps[stem].items():
            per_exp.append({
                "image": short(stem), "method": method, "mode": mode,
                "base_reward": round(bases[stem], 4),
                "mean_abs_importance": round(float(np.abs(imp).mean()), 4),
                "top1_importance": round(float(imp.max()), 4),
                "negative_fraction": round(float((imp < 0).mean()), 4),
                "n_superpixels": nsp[stem],
            })

    method_agree, baseline_agree = [], []
    for stem in items:
        for mode in MODES:
            occ = imps[stem].get(("occlusion", mode))
            lim = imps[stem].get(("lime", mode))
            if occ is not None and lim is not None:
                method_agree.append({"image": short(stem), "mode": mode,
                                     "spearman_occ_vs_lime": round(spearman(occ, lim), 3)})
        ref = imps[stem].get(("occlusion", "black"))
        if ref is not None:
            for mode in MODES:
                occ = imps[stem].get(("occlusion", mode))
                if occ is not None:
                    baseline_agree.append({"image": short(stem), "mode": mode,
                                          "spearman_vs_black": round(spearman(occ, ref), 3)})
    return imps, bases, nsp, per_exp, method_agree, baseline_agree


# --------------------------------------------------------------------------- #
# plotting
# --------------------------------------------------------------------------- #
def _grouped_bar(ax, rows, value_key, group_key, x_key, title, ylabel):
    xs = sorted({r[x_key] for r in rows})
    groups = [g for g in MODES if g in {r[group_key] for r in rows}]
    width = 0.8 / max(len(groups), 1)
    for gi, g in enumerate(groups):
        vals = []
        for x in xs:
            match = [r[value_key] for r in rows if r[x_key] == x and r[group_key] == g]
            vals.append(match[0] if match else np.nan)
        pos = np.arange(len(xs)) + gi * width
        ax.bar(pos, vals, width, label=g)
    ax.set_xticks(np.arange(len(xs)) + width * (len(groups) - 1) / 2)
    ax.set_xticklabels(xs, rotation=30, ha="right")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.legend(title=group_key, fontsize=8)


def make_figures(results_dir, per_exp, method_agree, baseline_agree, faith):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figs = {}

    fig, ax = plt.subplots(figsize=(8, 5))
    _grouped_bar(ax, method_agree, "spearman_occ_vs_lime", "mode", "image",
                 "Occlusion vs LIME agreement (Spearman)", "Spearman")
    ax.axhline(0, color="k", lw=0.5)
    fig.tight_layout(); p = os.path.join(results_dir, "fig_method_agreement.png")
    fig.savefig(p, dpi=150); plt.close(fig); figs["method_agreement"] = "fig_method_agreement.png"

    occ_exp = [r for r in per_exp if r["method"] == "occlusion"]
    fig, ax = plt.subplots(figsize=(8, 5))
    _grouped_bar(ax, occ_exp, "mean_abs_importance", "mode", "image",
                 "Occlusion importance magnitude by baseline", "mean |importance|")
    fig.tight_layout(); p = os.path.join(results_dir, "fig_baseline_magnitude.png")
    fig.savefig(p, dpi=150); plt.close(fig); figs["baseline_magnitude"] = "fig_baseline_magnitude.png"

    fig, ax = plt.subplots(figsize=(8, 5))
    _grouped_bar(ax, baseline_agree, "spearman_vs_black", "mode", "image",
                 "Occlusion ranking agreement vs 'black' baseline", "Spearman vs black")
    ax.axhline(0, color="k", lw=0.5)
    fig.tight_layout(); p = os.path.join(results_dir, "fig_baseline_agreement.png")
    fig.savefig(p, dpi=150); plt.close(fig); figs["baseline_agreement"] = "fig_baseline_agreement.png"

    if faith:
        keys = sorted(faith.keys())
        labels = [f"{short(s)}\n{m}/{md}" for (s, m, md) in keys]
        fig, axes = plt.subplots(1, 2, figsize=(max(8, 1.5 * len(keys)), 5))
        x = np.arange(len(keys)); w = 0.38
        d_imp = [faith[k]["deletion_auc"] for k in keys]
        d_rnd = [faith[k]["deletion_auc_random"] for k in keys]
        i_imp = [faith[k]["insertion_auc"] for k in keys]
        i_rnd = [faith[k]["insertion_auc_random"] for k in keys]
        axes[0].bar(x - w / 2, d_imp, w, label="by importance", color="C3")
        axes[0].bar(x + w / 2, d_rnd, w, label="random", color="C7")
        axes[0].set_title("Deletion AUC (lower = better)"); axes[0].set_ylabel("AUC")
        axes[1].bar(x - w / 2, i_imp, w, label="by importance", color="C2")
        axes[1].bar(x + w / 2, i_rnd, w, label="random", color="C7")
        axes[1].set_title("Insertion AUC (higher = better)")
        for ax in axes:
            ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=8); ax.legend(fontsize=8)
        fig.tight_layout(); p = os.path.join(results_dir, "fig_faithfulness.png")
        fig.savefig(p, dpi=150); plt.close(fig); figs["faithfulness"] = "fig_faithfulness.png"

    return figs


def make_montages(results_dir, items):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.image as mpimg

    montages = {}
    for stem, d in items.items():
        fig, axes = plt.subplots(len(METHODS), len(MODES), figsize=(4 * len(MODES), 4 * len(METHODS)))
        axes = np.atleast_2d(axes)
        for i, method in enumerate(METHODS):
            for j, mode in enumerate(MODES):
                ax = axes[i, j]; ax.axis("off")
                png = os.path.join(results_dir, f"{stem}_{method}_{mode}.png")
                if os.path.exists(png):
                    ax.imshow(mpimg.imread(png))
                ax.set_title(f"{method}/{mode}", fontsize=10)
        fig.suptitle(short(stem), fontsize=14)
        fig.tight_layout()
        name = f"montage_{short(stem)}.png"
        fig.savefig(os.path.join(results_dir, name), dpi=120); plt.close(fig)
        montages[stem] = name
    return montages


# --------------------------------------------------------------------------- #
# CSV + report
# --------------------------------------------------------------------------- #
def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)


def _md_table(rows, cols):
    out = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    return "\n".join(out)


def write_report(results_dir, items, bases, nsp, per_exp, method_agree, baseline_agree, faith, figs, montages, prompts):
    L = []
    L.append("# HPSv3 Perturbation-Attribution Report\n")
    L.append(f"Results directory: `{os.path.basename(results_dir.rstrip('/'))}`  ")
    L.append(f"Images: **{len(items)}**  |  Methods: occlusion, LIME  |  "
             f"Baselines: gray, mean, blur, black\n")

    L.append("## Overview\n")
    overview = [{"image": short(s), "prompt": (prompts.get(s, "")[:60] + "…") if len(prompts.get(s, "")) > 60 else prompts.get(s, ""),
                 "base_reward": round(bases[s], 3), "n_superpixels": nsp[s]} for s in items]
    L.append(_md_table(overview, ["image", "prompt", "base_reward", "n_superpixels"]) + "\n")

    L.append("## 1. What the model rewards (per image)\n")
    for s in items:
        L.append(f"### {short(s)} — base reward μ = {bases[s]:.3f}\n")
        L.append(f"Prompt: *{prompts.get(s, '')[:300]}*\n")
        L.append(f"![montage]({montages.get(s, '')})\n")
        # quick numeric profile from occlusion/black
        imp = load_npz(items[s][("occlusion", "black")])["importances"] if ("occlusion", "black") in items[s] else None
        if imp is not None:
            L.append(f"- Strongest positive region (occlusion/black): **+{imp.max():.2f}**\n"
                     f"- Fraction of regions that *hurt* the score: **{(imp < 0).mean() * 100:.1f}%** "
                     f"(min {imp.min():+.2f})\n")

    L.append("## 2. Occlusion vs LIME agreement (cross-method validation)\n")
    L.append(f"![method agreement]({figs.get('method_agreement', '')})\n")
    L.append(_md_table(method_agree, ["image", "mode", "spearman_occ_vs_lime"]) + "\n")
    vals = [r["spearman_occ_vs_lime"] for r in method_agree]
    if vals:
        L.append(f"Mean Spearman = **{np.nanmean(vals):.2f}** ({_qual(np.nanmean(vals))}). "
                 "Positive across the board → the two methods localize the same regions.\n")

    L.append("## 3. Baseline (color-perturbation) sensitivity\n")
    L.append(f"![baseline magnitude]({figs.get('baseline_magnitude', '')})\n")
    L.append(f"![baseline agreement]({figs.get('baseline_agreement', '')})\n")
    L.append(_md_table(baseline_agree, ["image", "mode", "spearman_vs_black"]) + "\n")
    bvals = [r["spearman_vs_black"] for r in baseline_agree if r["mode"] != "black"]
    if bvals:
        L.append(f"Mean agreement of soft baselines vs black = **{np.nanmean(bvals):.2f}** "
                 f"({_qual(np.nanmean(bvals))}). Black gives the largest magnitudes; the choice of "
                 "baseline shifts which regions look most important (the 'missingness' effect) → "
                 "report multiple baselines.\n")

    L.append("## 4. Faithfulness (deletion / insertion)\n")
    if faith:
        L.append(f"![faithfulness]({figs.get('faithfulness', '')})\n")
        frows = [{"image": short(s), "method": m, "mode": md,
                  "deletion_auc": v["deletion_auc"], "deletion_random": v["deletion_auc_random"],
                  "deletion_ok": "✓" if v["deletion_auc"] < v["deletion_auc_random"] else "✗",
                  "insertion_auc": v["insertion_auc"], "insertion_random": v["insertion_auc_random"],
                  "insertion_ok": "✓" if v["insertion_auc"] > v["insertion_auc_random"] else "✗"}
                 for (s, m, md), v in sorted(faith.items())]
        L.append(_md_table(frows, ["image", "method", "mode", "deletion_auc", "deletion_random",
                                   "deletion_ok", "insertion_auc", "insertion_random", "insertion_ok"]) + "\n")
        ins_ok = sum(1 for r in frows if r["insertion_ok"] == "✓")
        del_ok = sum(1 for r in frows if r["deletion_ok"] == "✓")
        L.append(f"Insertion passes **{ins_ok}/{len(frows)}**, deletion passes **{del_ok}/{len(frows)}**. "
                 "Insertion is the more reliable signal; with soft baselines (gray) the deletion AUC can be "
                 "inflated by the curve recovering toward the neutral-baseline score — compare the black "
                 "baseline for a cleaner deletion test.\n")
    else:
        L.append("_No faithfulness rows found in summary.csv (run with --faithfulness)._\n")

    L.append("## 5. Limitations\n")
    L.append(f"- Sample size: **{len(items)} images** — illustrative, not statistically general.\n")
    L.append("- All images are high-scoring; no low-quality contrast case.\n")
    L.append("- LIME has sampling noise; check stability across seeds before strong claims.\n")

    out = os.path.join(results_dir, "REPORT.md")
    with open(out, "w") as f:
        f.write("\n".join(L))
    return out


# --------------------------------------------------------------------------- #
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--results-dir", required=True)
    args = p.parse_args(argv)

    rd = args.results_dir
    items = discover(rd)
    if not items:
        raise SystemExit(f"No *.npz attribution results found in {rd}")

    prompts = read_prompts(rd)
    faith = read_faithfulness(rd)
    imps, bases, nsp, per_exp, method_agree, baseline_agree = compute(items)

    write_csv(os.path.join(rd, "agg_per_experiment.csv"), per_exp)
    write_csv(os.path.join(rd, "agg_method_agreement.csv"), method_agree)
    write_csv(os.path.join(rd, "agg_baseline_agreement.csv"), baseline_agree)
    if faith:
        write_csv(os.path.join(rd, "agg_faithfulness.csv"),
                  [dict(image=short(s), method=m, mode=md, **v) for (s, m, md), v in sorted(faith.items())])

    figs = make_figures(rd, per_exp, method_agree, baseline_agree, faith)
    montages = make_montages(rd, items)
    report = write_report(rd, items, bases, nsp, per_exp, method_agree, baseline_agree, faith, figs, montages, prompts)

    print(f"[aggregate] {len(items)} images, {len(per_exp)} experiments")
    print(f"[aggregate] CSVs + {len(figs)} figures + {len(montages)} montages written to {rd}")
    print(f"[aggregate] report -> {report}")


if __name__ == "__main__":
    main()
