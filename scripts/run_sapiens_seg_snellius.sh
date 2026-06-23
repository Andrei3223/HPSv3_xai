#!/bin/bash
# =============================================================================
# Snellius Stage 0: Sapiens-lite body-part segmentation -> label maps.
# Runs in the SAPIENS venv. Produces <stem>_sapiens_labels.npy + <stem>_parts.json
# that the HPSv3 attribution job (Stage 1) consumes via --segments sapiens.
#
# Submit AFTER you have built manifest.json (see step-by-step in chat / README).
#   sbatch scripts/run_sapiens_seg_snellius.sh
# =============================================================================
#SBATCH --job-name=sapiens_seg
#SBATCH --partition=gpu_h100
#SBATCH --gpus=1
#SBATCH --cpus-per-task=16
#SBATCH --time=01:00:00
#SBATCH --output=logs/sapiens_%j.out
#SBATCH --error=logs/sapiens_%j.err

set -euo pipefail

# ------------------------------ user settings -------------------------------
PROJECT="${SLURM_SUBMIT_DIR:-$PWD}"

# Sapiens venv (torch + numpy + opencv). Created separately (see step-by-step).
SAPIENS_VENV=/gpfs/scratch1/shared/scur0283/venvs/sapiens

# Sapiens-lite seg TorchScript checkpoint (.pt2). 0.6B is a good speed/quality pick.
SAPIENS_CKPT=/gpfs/scratch1/shared/scur0283/models/sapiens/sapiens_0.6b_goliath_best_goliath_mIoU_7777_epoch_178_torchscript.pt2
# Path to sapiens/lite/demo (so exact class names are read from the repo).
SAPIENS_DEMO_DIR=/gpfs/scratch1/shared/scur0283/sapiens/lite/demo

MANIFEST="$PROJECT/manifest.json"          # shared with the HPSv3 job
SAPIENS_OUT="$PROJECT/results/sapiens_labels"
MERGE=coarse                                # coarse body parts (recommended) or fine
# ----------------------------------------------------------------------------

mkdir -p "$PROJECT/logs"
cd "$PROJECT"

module purge
module load 2023 || true
# shellcheck source=/dev/null
source "$SAPIENS_VENV/bin/activate"
echo "Python: $(which python)"
nvidia-smi || true

if [[ ! -f "$MANIFEST" ]]; then
  echo "ERROR: $MANIFEST not found. Build it first:" >&2
  echo "  python scripts/build_manifest.py --hpdv3-dir <HPDv3> --num-dataset 5 --out manifest.json" >&2
  exit 1
fi

srun python scripts/sapiens_segment.py \
  --checkpoint "$SAPIENS_CKPT" \
  --manifest "$MANIFEST" \
  --output-dir "$SAPIENS_OUT" \
  --merge "$MERGE" \
  --sapiens-demo-dir "$SAPIENS_DEMO_DIR"

echo "================================================================"
echo "DONE. Sapiens label maps in: $SAPIENS_OUT"
echo "Next: run the HPSv3 attribution job with"
echo "  --segments sapiens --sapiens-dir $SAPIENS_OUT --manifest $MANIFEST"
echo "================================================================"
