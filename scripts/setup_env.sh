#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_VERSION="${PYTHON_VERSION:-3.10}"

export HF_HOME="${HF_HOME:-/gpfs/scratch1/shared/scur0077/hf_cache}"
export TRANSFORMERS_CACHE="${HF_HOME}"
mkdir -p "${HF_HOME}"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi

cd "${REPO_ROOT}"
if [[ ! -d .venv ]]; then
  uv venv --python "${PYTHON_VERSION}" .venv
fi
source .venv/bin/activate

uv pip install -e ".[dev,sae]"

echo "Environment ready. Activate with: source ${REPO_ROOT}/.venv/bin/activate"
echo "Optional: uv pip install flash-attn --no-build-isolation"
