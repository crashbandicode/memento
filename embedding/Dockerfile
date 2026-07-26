# Embedding server image (BGE-M3 by default).
#
# Why Docker is now the default install path (vs. a host-side venv):
#   - No host Python version sniffing — image pins python:3.12-slim.
#   - torch>=2.6 + sentence-transformers wheels resolve once at build
#     time; no flaky pip dependency resolution on the user's machine.
#   - Model is baked into the image, so first start is fast and the
#     1.3 GB download fails fast at build time (visible) instead of
#     silently mid-run.
#   - Restart policy + healthcheck replace launchd / systemd plumbing.
#
# Trade-off: CPU only (Docker Desktop on macOS can't expose MPS; GPU
# users wanting CUDA should run the host install via
# `./install.sh embedding --native`).
#
# Optional ONNX / int8 CPU path (does NOT inflate the default torch image):
#   docker compose build embedding \
#     --build-arg EMBEDDING_BACKEND=onnx \
#     --build-arg ONNX_QUANTIZE=1 \
#     --build-arg ONNX_QUANTIZATION_CONFIG=avx2
#   Then set MEMENTO_EMBEDDING_BACKEND=onnx (and optionally
#   MEMENTO_EMBEDDING_ONNX_FILE) at runtime. Quantization uses the real
#   sentence-transformers `export_dynamic_quantized_onnx_model` API —
#   not a fake flag. Do not claim parity vs torch without a benchmark.

FROM python:3.12-slim

ARG MODEL_NAME=BAAI/bge-m3
ARG PIP_INDEX_URL=
ARG EMBEDDING_BACKEND=torch
ARG ONNX_QUANTIZE=0
ARG ONNX_QUANTIZATION_CONFIG=avx2
ENV MODEL_NAME=${MODEL_NAME} \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_NO_CACHE_DIR=1 \
    MEMENTO_EMBEDDING_BACKEND=${EMBEDDING_BACKEND} \
    MEMENTO_EMBEDDING_DEVICE=cpu \
    ONNX_QUANTIZE=${ONNX_QUANTIZE} \
    ONNX_QUANTIZATION_CONFIG=${ONNX_QUANTIZATION_CONFIG}

WORKDIR /app

# curl: container healthcheck via /health.
# build-essential intentionally absent — torch CPU wheels are
# precompiled and sentence-transformers is pure Python.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Default (torch): keep the existing CPU torch + sentence-transformers stack.
# ONNX profile: install sentence-transformers[onnx] + onnxruntime (+ optimum
# when building a dynamically quantized int8 artifact). Torch remains a
# transitive ST dependency but the runtime backend is onnx.
RUN if [ "$EMBEDDING_BACKEND" = "onnx" ]; then \
      pip install ${PIP_INDEX_URL:+--index-url $PIP_INDEX_URL} \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.6" "sentence-transformers[onnx]>=3.0" "onnxruntime>=1.16" \
        "modelscope>=1.20" \
        $([ "$ONNX_QUANTIZE" = "1" ] && echo "optimum[onnxruntime]>=1.17"); \
    else \
      pip install ${PIP_INDEX_URL:+--index-url $PIP_INDEX_URL} \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.6" "sentence-transformers>=3.0" "modelscope>=1.20"; \
    fi

# Multi-source model fetch. Prefer HuggingFace here because
# SentenceTransformer downloads only the files required to load the model.
# ModelScope snapshot_download otherwise fetches every published weight format
# (PyTorch, ONNX, and OpenVINO), multiplying this image by several gigabytes.
# The ModelScope fallback excludes non-PyTorch exports for the torch default
# image; the onnx backend keeps onnx/** instead.
# Writes the resolved load path to /app/.model_path; runtime reads it.
# Optional ONNX int8 export writes MEMENTO_EMBEDDING_ONNX_FILE into
# /app/.onnx_file when ONNX_QUANTIZE=1.
RUN python - <<'PY'
import os, sys
from pathlib import Path

model = os.environ['MODEL_NAME']
backend = os.environ.get('MEMENTO_EMBEDDING_BACKEND', 'torch').strip().lower() or 'torch'
quantize = os.environ.get('ONNX_QUANTIZE', '0').strip() == '1'
qconfig = os.environ.get('ONNX_QUANTIZATION_CONFIG', 'avx2').strip() or 'avx2'
load_path = None

try:
    from sentence_transformers import SentenceTransformer
    kwargs = {}
    if backend == 'onnx':
        kwargs = {
            'backend': 'onnx',
            'device': 'cpu',
            'model_kwargs': {'provider': 'CPUExecutionProvider'},
        }
    SentenceTransformer(model, **kwargs)
    load_path = model
    print('OK: huggingface direct')
except Exception as e:
    print(f'huggingface direct failed: {e}', file=sys.stderr)

if load_path is None:
    try:
        from modelscope import snapshot_download
        ignore = [
            'openvino/**', '*.xml', '*.ot',
        ]
        if backend != 'onnx':
            ignore = [
                '*.onnx', '*.onnx_data', 'onnx/**',
                'openvino/**', '*.xml', '*.ot',
            ]
        p = snapshot_download(model, ignore_patterns=ignore)
        from sentence_transformers import SentenceTransformer
        kwargs = {}
        if backend == 'onnx':
            kwargs = {
                'backend': 'onnx',
                'device': 'cpu',
                'model_kwargs': {'provider': 'CPUExecutionProvider'},
            }
        SentenceTransformer(p, **kwargs)
        load_path = p
        print(f'OK: modelscope -> {p}')
    except Exception as e:
        print(f'modelscope failed: {e}', file=sys.stderr)

if load_path is None:
    os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'
    from sentence_transformers import SentenceTransformer
    kwargs = {}
    if backend == 'onnx':
        kwargs = {
            'backend': 'onnx',
            'device': 'cpu',
            'model_kwargs': {'provider': 'CPUExecutionProvider'},
        }
    SentenceTransformer(model, **kwargs)
    load_path = model
    print('OK: hf-mirror.com')

onnx_file = ''
if backend == 'onnx' and quantize:
    import shutil
    from sentence_transformers import SentenceTransformer, export_dynamic_quantized_onnx_model
    out = Path('/app/onnx-model')
    out.mkdir(parents=True, exist_ok=True)
    st = SentenceTransformer(
        load_path,
        backend='onnx',
        device='cpu',
        model_kwargs={'provider': 'CPUExecutionProvider'},
    )
    st.save_pretrained(str(out))
    # Hugging Face snapshots use symlinks for BGE-M3's external
    # model.onnx_data. Optimum/onnx deliberately refuses to quantize external
    # tensor data through a symlink, so make the exported artifact standalone
    # and reload from it before constructing the quantizer.
    for artifact in out.rglob('*'):
        if artifact.is_symlink():
            target = artifact.resolve()
            artifact.unlink()
            shutil.copy2(target, artifact)
    st = SentenceTransformer(
        str(out),
        backend='onnx',
        device='cpu',
        model_kwargs={'provider': 'CPUExecutionProvider'},
    )
    export_dynamic_quantized_onnx_model(
        model=st,
        quantization_config=qconfig,
        model_name_or_path=str(out),
    )
    # Sentence Transformers normally writes under onnx/, but keep this
    # resilient to backend layout changes while preserving the relative path
    # expected by model_kwargs.file_name.
    candidates = sorted(out.rglob(f'model_qint8_{qconfig}*.onnx'))
    candidates += sorted(out.rglob(f'model_quint8_{qconfig}*.onnx'))
    if not candidates:
        candidates = sorted(out.rglob('model_qint8_*.onnx'))
        candidates += sorted(out.rglob('model_quint8_*.onnx'))
    if not candidates:
        raise SystemExit(
            f'ONNX quantize produced no model_qint8_*.onnx or '
            f'model_quint8_*.onnx under {out}'
        )
    onnx_file = candidates[0].relative_to(out).as_posix()
    load_path = str(out)
    print(f'OK: quantized onnx -> {candidates[0]}')

Path('/app/.model_path').write_text(load_path)
Path('/app/.onnx_file').write_text(onnx_file)
PY

# Keep application code after the heavyweight dependency/model layers so a
# server-only fix does not force another model verification/download.
COPY embedding_server.py .

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=4 \
  CMD curl -fsS http://localhost:8002/health || exit 1

EXPOSE 8002

# Server reads the absolute model path that the build step settled on.
# MEMENTO_EMBEDDING_MODEL_NAME at runtime overrides for users wanting
# a different model (triggers a re-download on first start).
# ONNX file name is optional; empty means ST default model.onnx discovery.
CMD ["sh", "-c", "export MEMENTO_EMBEDDING_ONNX_FILE=\"${MEMENTO_EMBEDDING_ONNX_FILE:-$(cat /app/.onnx_file)}\"; exec python embedding_server.py --port \"${MEMENTO_EMBEDDING_PORT:-8002}\" --model \"${MEMENTO_EMBEDDING_MODEL_NAME:-$(cat /app/.model_path)}\""]
