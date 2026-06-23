#!/usr/bin/env bash
#SBATCH --partition=gpu_h100
#SBATCH --gpus=4
#SBATCH --job-name=hpsv3_extract
#SBATCH --ntasks=1
#SBATCH --time=08:00:00
#SBATCH --output=logs/extract_activations_%j.out

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="/home/scur0077/HPSv3_xai"

export HF_HOME="${HF_HOME:-/gpfs/scratch1/shared/scur0077/hf_cache}"
export TRANSFORMERS_CACHE="${HF_HOME}"
export PYTHONUNBUFFERED=1
export DATASET_ROOT="${DATASET_ROOT:-/gpfs/scratch1/shared/scur0077/datasets/HPDv3}"
export CHECKPOINT="${CHECKPOINT:-/gpfs/scratch1/shared/scur0077/models/HPSv3/HPSv3.safetensors}"
export ACTIVATION_DIR="${ACTIVATION_DIR:-/gpfs/scratch1/shared/scur0077/sae/activations}"
export TARGET_IMAGES="${TARGET_IMAGES:-150000}"
export BATCH_SIZE="${BATCH_SIZE:-16}"
export NUM_SHARDS="${NUM_SHARDS:-4}"
export METADATA_WRITE_EVERY="${METADATA_WRITE_EVERY:-50}"

source "${REPO_ROOT}/.venv/bin/activate"
mkdir -p "${REPO_ROOT}/logs"

echo "Job started at $(date)"
echo "SLURM_JOB_ID=${SLURM_JOB_ID:-local} PARTITION=${SLURM_JOB_PARTITION:-unknown} GPUS=${SLURM_GPUS:-unknown} TIMELIMIT=${SLURM_TIMELIMIT:-unknown}"
echo "Launching ${NUM_SHARDS} shards with batch_size=${BATCH_SIZE} per GPU"

pids=()
for SHARD_ID in $(seq 0 $((NUM_SHARDS - 1))); do
  CUDA_VISIBLE_DEVICES="${SHARD_ID}" python -m hpsv3.sae.extract_activations \
    --dataset_root="${DATASET_ROOT}" \
    --checkpoint_path="${CHECKPOINT}" \
    --output_dir="${ACTIVATION_DIR}" \
    --target_images="${TARGET_IMAGES}" \
    --batch_size="${BATCH_SIZE}" \
    --metadata_write_every="${METADATA_WRITE_EVERY}" \
    --prefetch=True \
    --shard_id="${SHARD_ID}" \
    --num_shards="${NUM_SHARDS}" \
    --device=cuda \
    > "${REPO_ROOT}/logs/extract_${SLURM_JOB_ID:-local}_shard${SHARD_ID}.log" 2>&1 &
  pids+=("$!")
  echo "Started shard ${SHARD_ID} (pid $!)"
done

fail=0
for pid in "${pids[@]}"; do
  if ! wait "${pid}"; then
    fail=1
  fi
done

if [[ "${fail}" -ne 0 ]]; then
  echo "One or more shards failed. Check logs/extract_${SLURM_JOB_ID:-local}_shard*.log"
  exit 1
fi

echo "All shards finished. Merging metadata..."
python -m hpsv3.sae.merge_activations \
  --output_dir="${ACTIVATION_DIR}" \
  --num_shards="${NUM_SHARDS}"

echo "Done at $(date)"
