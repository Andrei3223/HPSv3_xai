#!/usr/bin/env bash
#SBATCH --partition=gpu_a100
#SBATCH --gpus=1
#SBATCH --job-name=sae_train
#SBATCH --ntasks=1
#SBATCH --time=04:00:00
#SBATCH --output=logs/train_sae_%j.out

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="/home/scur0077/HPSv3_xai"

export ACTIVATION_DIR="${ACTIVATION_DIR:-/gpfs/scratch1/shared/scur0077/sae/activations}"
export SAE_OUTPUT_DIR="${SAE_OUTPUT_DIR:-/gpfs/scratch1/shared/scur0077/sae/checkpoints}"
export N_FEATURES="${N_FEATURES:-16384}"
export K="${K:-32}"
export BATCH_SIZE="${BATCH_SIZE:-4096}"
export EPOCHS="${EPOCHS:-15}"
export PYTHONUNBUFFERED=1

source "${REPO_ROOT}/.venv/bin/activate"
mkdir -p "${REPO_ROOT}/logs"

echo "Job started at $(date)"
echo "SLURM_JOB_ID=${SLURM_JOB_ID:-local} PARTITION=${SLURM_JOB_PARTITION:-unknown} GPUS=${SLURM_GPUS:-unknown}"

python -m hpsv3.sae.train_sae \
  --activation_dir="${ACTIVATION_DIR}" \
  --output_dir="${SAE_OUTPUT_DIR}" \
  --n_features="${N_FEATURES}" \
  --k="${K}" \
  --batch_size="${BATCH_SIZE}" \
  --epochs="${EPOCHS}" \
  --device=cuda
