#!/usr/bin/env bash
#SBATCH --partition=thin
#SBATCH --job-name=hpdv3_download
#SBATCH --ntasks=1
#SBATCH --time=04:00:00
#SBATCH --output=logs/download_hpdv3_%j.out

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="/home/scur0077/HPSv3_xai"

export HF_HOME="${HF_HOME:-/gpfs/scratch1/shared/scur0077/hf_cache}"
export DATASET_ROOT="${DATASET_ROOT:-/gpfs/scratch1/shared/scur0077/datasets/HPDv3}"
export TARGET_IMAGES="${TARGET_IMAGES:-150000}"

source "${REPO_ROOT}/.venv/bin/activate"
mkdir -p "${REPO_ROOT}/logs"

python "${SCRIPT_DIR}/download_hpdv3.py" \
  --dest="${DATASET_ROOT}" \
  --target_images="${TARGET_IMAGES}"
