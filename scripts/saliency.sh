#!/usr/bin/env bash
#SBATCH --partition=gpu_a100
#SBATCH --gpus=1
#SBATCH --job-name=hpsv3_saliency
#SBATCH --ntasks=1
#SBATCH --time=02:00:00
#SBATCH --output=logs/saliency_%j.out

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="/home/scur0077/HPSv3_xai"

export HF_HOME="${HF_HOME:-/gpfs/scratch1/shared/scur0077/hf_cache}"
export TRANSFORMERS_CACHE="${HF_HOME}"
export CHECKPOINT="${CHECKPOINT:-/gpfs/scratch1/shared/scur0077/models/HPSv3/HPSv3.safetensors}"
export OUTPUT_DIR="${OUTPUT_DIR:-/gpfs/scratch1/shared/scur0077/saliency}"
export TARGET="${TARGET:-both}"
export METHODS="${METHODS:-vanilla,integrated_gradients,grad_cam,attention_rollout}"
export IG_STEPS="${IG_STEPS:-32}"
export PYTHONUNBUFFERED=1

source "${REPO_ROOT}/.venv/bin/activate"
mkdir -p "${REPO_ROOT}/logs"

echo "Job started at $(date)"
echo "SLURM_JOB_ID=${SLURM_JOB_ID:-local} PARTITION=${SLURM_JOB_PARTITION:-unknown} GPUS=${SLURM_GPUS:-unknown}"
echo "TARGET=${TARGET} METHODS=${METHODS} IG_STEPS=${IG_STEPS} OUTPUT_DIR=${OUTPUT_DIR}"

python -m hpsv3.saliency.gradient_saliency \
  --checkpoint_path="${CHECKPOINT}" \
  --output_dir="${OUTPUT_DIR}" \
  --methods="${METHODS}" \
  --target="${TARGET}" \
  --ig_steps="${IG_STEPS}" \
  --device=cuda

echo "Done at $(date)"
