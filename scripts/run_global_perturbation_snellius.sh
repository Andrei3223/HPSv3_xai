#!/bin/bash
# =============================================================================
# Snellius: HPSv3 sensitivity to GLOBAL image degradations on 1x H100.
# Sweeps brightness / contrast / colour / blur / JPEG / noise over many images
# and plots reward-vs-strength curves. Model loaded once.
#
#   sbatch scripts/run_global_perturbation_snellius.sh
# =============================================================================
#SBATCH --job-name=hpsv3_global
#SBATCH --partition=gpu_h100
#SBATCH --gpus=1
#SBATCH --cpus-per-task=16
#SBATCH --time=02:00:00
#SBATCH --output=logs/global_%j.out
#SBATCH --error=logs/global_%j.err

set -euo pipefail

PROJECT="${SLURM_SUBMIT_DIR:-$PWD}"
MODELS_DIR=/gpfs/scratch1/shared/scur0077/models/HPSv3
HPDV3_DIR=/gpfs/scratch1/shared/scur0077/datasets/HPDv3
VENV_DIR=/gpfs/scratch1/shared/scur0283/venvs/hpsv3
export HF_HOME=/gpfs/scratch1/shared/scur0077/hf_cache
export TOKENIZERS_PARALLELISM=false

NUM_DATASET="${NUM_DATASET:-100}"
BATCH_SIZE="${BATCH_SIZE:-16}"
MANIFEST="$PROJECT/manifest.json"      # reuse the same images as the other runs

mkdir -p "$PROJECT/logs"
cd "$PROJECT"
module purge
module load 2023 || true
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
nvidia-smi || true

CKPT="$(ls "$MODELS_DIR"/*.safetensors 2>/dev/null | head -1 || true)"
[[ -z "${CKPT}" ]] && { echo "ERROR: no .safetensors in $MODELS_DIR" >&2; exit 1; }

if [[ ! -f "$MANIFEST" ]]; then
  python scripts/build_manifest.py --hpdv3-dir "$HPDV3_DIR" \
    --num-dataset "$NUM_DATASET" --shuffle --seed 0 --out "$MANIFEST"
fi

OUT="results/global_${SLURM_JOB_ID:-local}"
srun python -m hpsv3.xai.global_perturbation \
  --hpsv3-ckpt "$CKPT" \
  --manifest "$MANIFEST" \
  --batch-size "$BATCH_SIZE" --device cuda \
  --output-dir "$OUT"

echo "DONE -> $OUT/fig_global_perturbation.png  +  agg_global_perturbation.csv"
