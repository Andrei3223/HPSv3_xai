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
#SBATCH --time=02:00:00
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

# Experiment knobs.
N_SEGMENTS=100
N_SAMPLES=500
NUM_DATASET=3          # how many extra images to sample from HPDv3 (0 = assets only)
BATCH_SIZE=16
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
echo "Outputs -> $OUT"

# --- run the full experiment matrix (model loaded once) ---
srun python -m hpsv3.xai.run_experiments \
  --hpsv3-ckpt "$CKPT" \
  --hpdv3-dir "$HPDV3_DIR" --num-dataset "$NUM_DATASET" \
  --methods occlusion lime \
  --modes gray mean blur black \
  --n-segments "$N_SEGMENTS" --n-samples "$N_SAMPLES" \
  --batch-size "$BATCH_SIZE" --device cuda \
  --faithfulness --faith-method occlusion --faith-mode gray \
  --output-dir "$OUT"

echo "================================================================"
echo "DONE. Inspect:"
echo "  $OUT/summary.csv         (all metrics)"
echo "  $OUT/*.png               (heatmaps + faithfulness curves)"
echo "  $OUT/*.npz               (raw results for hpsv3.xai.compare)"
echo "================================================================"
