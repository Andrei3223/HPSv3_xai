# SAE interpretability pipeline for HPSv3

Train a TopK sparse autoencoder on the last-layer hidden state at the `<|Reward|>` token to interpret HPSv3 preference scores.

## Prerequisites

- HPSv3 checkpoint at `/gpfs/scratch1/shared/scur0077/models/HPSv3/HPSv3.safetensors`
- `uv` for environment management
- GPU node for activation extraction and SAE training

## Setup

```bash
cd /home/scur0077/HPSv3_xai
bash scripts/setup_env.sh
source .venv/bin/activate
export HF_HOME=/gpfs/scratch1/shared/scur0077/hf_cache
```

Optional flash attention:

```bash
uv pip install flash-attn --no-build-isolation
```

Pre-cache the base Qwen model (login node, needs network):

```bash
python -c "from transformers import AutoProcessor; AutoProcessor.from_pretrained('Qwen/Qwen2-VL-7B-Instruct')"
```

## Run order

### 1. Download ~150k HPDv3 images

```bash
python scripts/download_hpdv3.py \
  --dest=/gpfs/scratch1/shared/scur0077/datasets/HPDv3 \
  --target_images=150000
```

Or submit to SLURM:

```bash
sbatch scripts/download_hpdv3.sh
```

### 2. Extract reward-token activations (4 GPUs, batch 8 per GPU)

```bash
sbatch scripts/extract_activations.sh
```

This launches **4 parallel workers** (one per GPU), each processing a disjoint shard of
`samples.parquet`, writing into the shared `activations.npy` memmap at non-overlapping
global indices. Per-shard logs go to `logs/extract_<jobid>_shard{0,1,2,3}.log`.
After all shards finish, metadata is merged to `metadata.parquet`.

Defaults: `NUM_SHARDS=4`, `BATCH_SIZE=16`, `METADATA_WRITE_EVERY=50`, `#SBATCH --gpus=4`.

Single-GPU fallback:

```bash
python -m hpsv3.sae.extract_activations \
  --batch_size=8 \
  --shard_id=0 --num_shards=1
```

Outputs:
- `activations.npy` — fp16 memmap `[N, 3584]`
- `metadata.parquet` — image paths, prompts, mu, sigma
- `samples.parquet` — fixed sample list for reproducible resume
- `norm_stats.json` — computed during SAE training

### 3. Train SAE

```bash
python -m hpsv3.sae.train_sae \
  --activation_dir=/gpfs/scratch1/shared/scur0077/sae/activations \
  --output_dir=/gpfs/scratch1/shared/scur0077/sae/checkpoints \
  --n_features=16384 \
  --k=32 \
  --batch_size=4096 \
  --epochs=15
```

Or:

```bash
sbatch scripts/train_sae.sh
```

### 4. Analyze features

```bash
python -m hpsv3.sae.analyze \
  --sae_path=/gpfs/scratch1/shared/scur0077/sae/checkpoints/sae.pt \
  --activation_dir=/gpfs/scratch1/shared/scur0077/sae/activations \
  --output_dir=/gpfs/scratch1/shared/scur0077/sae/analysis
```

## Tests

```bash
pytest tests/sae/test_model.py -v
```

## Defaults

See [configs/default.yaml](configs/default.yaml):
- **150k images** (~1.1 GB activations)
- **16384 features**, TopK **k=32**
- Activation site: `<|Reward|>` token hidden state (dim 3584)
