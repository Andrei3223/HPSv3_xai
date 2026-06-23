#!/usr/bin/env bash
#SBATCH --partition=gpu_a100
#SBATCH --gpus=1
#SBATCH --job-name=sae_analyze
#SBATCH --ntasks=1
#SBATCH --time=02:00:00
#SBATCH --output=logs/analyze_sae_%j.out

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="/home/scur0077/HPSv3_xai"

export ACTIVATION_DIR="${ACTIVATION_DIR:-/gpfs/scratch1/shared/scur0077/sae/activations}"
export SAE_PATH="${SAE_PATH:-/gpfs/scratch1/shared/scur0077/sae/checkpoints/sae.pt}"
export ANALYSIS_DIR="${ANALYSIS_DIR:-/gpfs/scratch1/shared/scur0077/sae/analysis}"
export CHECKPOINT="${CHECKPOINT:-/gpfs/scratch1/shared/scur0077/models/HPSv3/HPSv3.safetensors}"
export PYTHONUNBUFFERED=1

source "${REPO_ROOT}/.venv/bin/activate"
mkdir -p "${REPO_ROOT}/logs"

echo "Job started at $(date)"
echo "SLURM_JOB_ID=${SLURM_JOB_ID:-local} PARTITION=${SLURM_JOB_PARTITION:-unknown} GPUS=${SLURM_GPUS:-unknown}"

python -m hpsv3.sae.analyze \
  --sae_path="${SAE_PATH}" \
  --activation_dir="${ACTIVATION_DIR}" \
  --hpsv3_checkpoint="${CHECKPOINT}" \
  --output_dir="${ANALYSIS_DIR}" \
  --device=cuda
