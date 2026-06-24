#!/bin/bash
# =============================================================================
# Snellius sbatch script: HPSv3 perturbation-attribution experiments on 1x H100.
# Loads the 7B model once and runs the full image x method x baseline matrix.
#
# Submit with:   sbatch scripts/run_xai_snellius.sh
# Check status:  squeue --me        Logs: logs/xai_<jobid>.out
# =============================================================================
#SBATCH --job-name=hpsv3_xai
#SBATCH --partition=gpu_h100
#SBATCH --gpus=1
#SBATCH --cpus-per-task=16
#SBATCH --time=04:00:00
#SBATCH --output=logs/xai_%j.out
#SBATCH --error=logs/xai_%j.err

set -euo pipefail

# ------------------------------ user settings -------------------------------
# Repo root (directory containing the `hpsv3/` package). Edit if needed.
PROJECT="${SLURM_SUBMIT_DIR:-$PWD}"

MODELS_DIR=/gpfs/scratch1/shared/scur0077/models/HPSv3
HPDV3_DIR=/gpfs/scratch1/shared/scur0077/datasets/HPDv3

# Cache for the Qwen2-VL base weights / processor (downloaded once, then reused).
export HF_HOME=/gpfs/scratch1/shared/scur0077/hf_cache
export TOKENIZERS_PARALLELISM=false

# Path to the venv created on the login node (see setup commands in the README).
VENV_DIR=/gpfs/scratch1/shared/scur0283/venvs/hpsv3

# Experiment knobs (all overridable at submit time via --export=ALL,VAR=...).
N_SEGMENTS=100
N_SAMPLES="${N_SAMPLES:-500}"
NUM_DATASET="${NUM_DATASET:-5}"     # extra HPDv3 images (plus the 2 repo assets)
BATCH_SIZE="${BATCH_SIZE:-16}"
METHODS="${METHODS:-occlusion lime}"  # e.g. METHODS=occlusion for a fast large run
MODES="${MODES:-gray mean blur black}"
FAITH="${FAITH:-1}"                  # 1 = run faithfulness, 0 = skip (faster)

# Region source: "slic" (default superpixels) or "sapiens" (semantic body parts).
# Override at submit time:  sbatch --export=ALL,SEGMENTS=sapiens scripts/run_xai_snellius.sh
# For sapiens, run scripts/run_sapiens_seg_snellius.sh FIRST (same manifest).
SEGMENTS="${SEGMENTS:-slic}"
MANIFEST="$PROJECT/manifest.json"               # shared with the Sapiens stage
SAPIENS_DIR="${SAPIENS_DIR:-$PROJECT/results/sapiens_labels}"   # Stage-0 region maps (sapiens OR scene)
# ----------------------------------------------------------------------------

mkdir -p "$PROJECT/logs"
cd "$PROJECT"

# --- environment ---
# The venv was built with the system python3.11; activating it is enough
# (no Python module needed). module load 2023 kept for any other system libs.
module purge
module load 2023 || true
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

echo "Python: $(which python)"
nvidia-smi || true

# --- resolve the HPSv3 checkpoint (any *.safetensors in the models dir) ---
CKPT="$(ls "$MODELS_DIR"/*.safetensors 2>/dev/null | head -1 || true)"
if [[ -z "${CKPT}" ]]; then
  echo "ERROR: no .safetensors found in $MODELS_DIR" >&2
  exit 1
fi
echo "Using HPSv3 checkpoint: $CKPT"

OUT="results/xai_${SLURM_JOB_ID:-local}"
echo "Outputs -> $OUT  (segments=$SEGMENTS)"

# Build the shared manifest only if it does not exist, so both stages use the
# SAME images (sampling is reproducible with a fixed seed).
if [[ ! -f "$MANIFEST" ]]; then
  python scripts/build_manifest.py --hpdv3-dir "$HPDV3_DIR" \
    --num-dataset "$NUM_DATASET" --shuffle --seed 0 --out "$MANIFEST"
else
  echo "Using existing manifest: $MANIFEST ($(python -c "import json;print(len(json.load(open('$MANIFEST'))))") images)"
fi

SEG_ARGS=(--n-segments "$N_SEGMENTS")
if [[ "$SEGMENTS" == "sapiens" ]]; then
  if [[ ! -d "$SAPIENS_DIR" ]]; then
    echo "ERROR: SEGMENTS=sapiens but $SAPIENS_DIR missing. Run scripts/run_sapiens_seg_snellius.sh first." >&2
    exit 1
  fi
  SEG_ARGS=(--segments sapiens --sapiens-dir "$SAPIENS_DIR")
fi

FAITH_ARGS=()
if [[ "$FAITH" == "1" ]]; then
  FAITH_ARGS=(--faithfulness --faith-methods occlusion --faith-modes gray black)
fi

# --- run the full experiment matrix (model loaded once) ---
# Faithfulness is run for BOTH the gray (soft) and black (hard) baselines so the
# deletion test can be read off the clean black baseline, not just gray.
srun python -m hpsv3.xai.run_experiments \
  --hpsv3-ckpt "$CKPT" \
  --manifest "$MANIFEST" \
  "${SEG_ARGS[@]}" \
  --methods $METHODS \
  --modes $MODES \
  --n-samples "$N_SAMPLES" \
  --batch-size "$BATCH_SIZE" --device cuda \
  "${FAITH_ARGS[@]}" \
  --output-dir "$OUT"

# --- aggregate into report tables, figures, montages, REPORT.md (no GPU needed) ---
python -m hpsv3.xai.aggregate --results-dir "$OUT"

echo "================================================================"
echo "DONE. Inspect:"
echo "  $OUT/REPORT.md           (full report with embedded figures)"
echo "  $OUT/summary.csv         (raw per-experiment metrics)"
echo "  $OUT/agg_*.csv           (aggregated tables)"
echo "  $OUT/fig_*.png           (summary figures)"
echo "  $OUT/montage_*.png       (per-image heatmap grids)"
echo "  $OUT/*.npz               (raw results for hpsv3.xai.compare)"
echo "================================================================"
