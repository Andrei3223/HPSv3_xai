#!/usr/bin/env bash
#SBATCH --partition=gpu_a100
#SBATCH --gpus=1
#SBATCH --job-name=sae_steer
#SBATCH --ntasks=1
#SBATCH --time=01:00:00
#SBATCH --output=logs/steer_sae_%j.out

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="/home/scur0077/HPSv3_xai"

export ACTIVATION_DIR="${ACTIVATION_DIR:-/gpfs/scratch1/shared/scur0077/sae/activations}"
export SAE_PATH="${SAE_PATH:-/gpfs/scratch1/shared/scur0077/sae/checkpoints/sae_epoch_006.pt}"
export ANALYSIS_DIR="${ANALYSIS_DIR:-/gpfs/scratch1/shared/scur0077/sae/analysis}"
export STEERING_DIR="${STEERING_DIR:-/gpfs/scratch1/shared/scur0077/sae/steering}"
export CHECKPOINT="${CHECKPOINT:-/gpfs/scratch1/shared/scur0077/models/HPSv3/HPSv3.safetensors}"
export N_EXAMPLES="${N_EXAMPLES:-3}"
export N_FEATURES="${N_FEATURES:-6}"
export FEATURE_INDICES="${FEATURE_INDICES:-}"
export PYTHONUNBUFFERED=1

source "${REPO_ROOT}/.venv/bin/activate"
mkdir -p "${REPO_ROOT}/logs"

echo "Job started at $(date)"
echo "SLURM_JOB_ID=${SLURM_JOB_ID:-local} PARTITION=${SLURM_JOB_PARTITION:-unknown} GPUS=${SLURM_GPUS:-unknown}"
echo "FEATURE_INDICES=${FEATURE_INDICES:-<default>}"
echo "STEERING_DIR=${STEERING_DIR}"

EXTRA_ARGS=()
if [[ -n "${FEATURE_INDICES}" ]]; then
  EXTRA_ARGS+=(--feature_indices="${FEATURE_INDICES}")
fi

python -m hpsv3.sae.steer \
  --sae_path="${SAE_PATH}" \
  --activation_dir="${ACTIVATION_DIR}" \
  --hpsv3_checkpoint="${CHECKPOINT}" \
  --analysis_dir="${ANALYSIS_DIR}" \
  --output_dir="${STEERING_DIR}" \
  --n_examples="${N_EXAMPLES}" \
  --n_features="${N_FEATURES}" \
  "${EXTRA_ARGS[@]}" \
  --device=cuda
