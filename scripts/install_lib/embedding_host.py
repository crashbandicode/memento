"""Install the BGE-M3 embedding server as a host-side background service.

Runs on host (not in Docker) because:
- macOS Docker Desktop can't expose MPS GPU
- Linux with NVIDIA works fine on host too
- Windows: same story

Cross-platform service install follows the same patterns as
collector/collector/cli.py `_install_launchd` / `_install_systemd` / `_install_windows_task`.

Runtime flags (also rendered into service templates):
  MEMENTO_EMBEDDING_BACKEND=torch|onnx   (default torch)
  MEMENTO_EMBEDDING_DEVICE=auto|cpu|cuda|mps
  MEMENTO_EMBEDDING_ONNX_PROVIDER=...    (onnx only; default CPUExecutionProvider)
  MEMENTO_EMBEDDING_ONNX_FILE=...        (optional quantized/optimized weight file)
  MEMENTO_EMBEDDING_ONNX_QUANTIZE=1      (optional: export dynamic int8 ONNX at install)
  MEMENTO_EMBEDDING_ONNX_QUANTIZATION_CONFIG=avx2|avx512|avx512_vnni|arm64
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import venv
from pathlib import Path

from .platform_utils import (
    IS_LINUX, IS_MAC, IS_WINDOWS, REPO_ROOT, detect_accelerator, find_python,
    info, ok, warn,
)

VENV_DIR = REPO_ROOT / ".venv-embedding"
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

_VALID_BACKENDS = frozenset({"torch", "onnx"})
_VALID_DEVICES = frozenset({"auto", "cpu", "cuda", "mps"})
# Reject shell / XML / template metacharacters so service files cannot be injected.
_UNSAFE_SERVICE_VALUE = re.compile(r'[;"&|<>`$(){}\n\r%]')


# ── venv + torch + sentence-transformers ──────────────────────
def _venv_python() -> Path:
    if IS_WINDOWS:
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _venv_pythonw() -> Path:
    if IS_WINDOWS:
        return VENV_DIR / "Scripts" / "pythonw.exe"
    return _venv_python()


def create_venv() -> Path:
    if _venv_python().exists():
        ok(f"Embedding venv already present at {VENV_DIR}")
        return _venv_python()
    info(f"Creating venv at {VENV_DIR}…")
    base_py = find_python()
    subprocess.run([base_py, "-m", "venv", str(VENV_DIR)], check=True)
    # upgrade pip so wheel installs don't warn
    subprocess.run([str(_venv_python()), "-m", "pip", "install", "-U",
                    "pip", "wheel", "setuptools"], check=True)
    ok("Venv created.")
    return _venv_python()


def _safe_service_value(name: str, value: str) -> str:
    text = value if value is not None else ""
    if _UNSAFE_SERVICE_VALUE.search(text):
        raise RuntimeError(
            f"Refusing to render {name}={text!r} into embedding service templates: "
            "value contains shell/XML metacharacters"
        )
    return text


def runtime_env_from_environ() -> dict[str, str]:
    """Read/validate backend+device settings for dependency install + templates."""
    backend = (os.environ.get("MEMENTO_EMBEDDING_BACKEND", "torch") or "torch").strip().lower()
    device = (os.environ.get("MEMENTO_EMBEDDING_DEVICE", "auto") or "auto").strip().lower()
    model_revision = (os.environ.get("MEMENTO_EMBEDDING_MODEL_REVISION", "") or "").strip()
    query_prefix = os.environ.get("MEMENTO_EMBEDDING_QUERY_PREFIX", "") or ""
    document_prefix = os.environ.get("MEMENTO_EMBEDDING_DOCUMENT_PREFIX", "") or ""
    max_sequence_length = (
        os.environ.get("MEMENTO_EMBEDDING_MAX_SEQUENCE_LENGTH", "0") or "0"
    ).strip()
    cpu_threads = (
        os.environ.get("MEMENTO_EMBEDDING_CPU_THREADS", "1") or "1"
    ).strip()
    onnx_provider = (os.environ.get("MEMENTO_EMBEDDING_ONNX_PROVIDER", "") or "").strip()
    onnx_file = (os.environ.get("MEMENTO_EMBEDDING_ONNX_FILE", "") or "").strip()
    artifact_sha256 = (
        os.environ.get("MEMENTO_EMBEDDING_ARTIFACT_SHA256", "") or ""
    ).strip().lower()
    if backend not in _VALID_BACKENDS:
        raise RuntimeError(
            f"Invalid MEMENTO_EMBEDDING_BACKEND={backend!r}; expected torch|onnx"
        )
    if device not in _VALID_DEVICES:
        raise RuntimeError(
            f"Invalid MEMENTO_EMBEDDING_DEVICE={device!r}; expected auto|cpu|cuda|mps"
        )
    if backend == "onnx" and device in {"cuda", "mps"}:
        raise RuntimeError(
            "MEMENTO_EMBEDDING_BACKEND=onnx only supports CPU "
            f"(got MEMENTO_EMBEDDING_DEVICE={device})"
        )
    if backend == "onnx" and not onnx_provider:
        onnx_provider = "CPUExecutionProvider"
    if backend != "onnx":
        onnx_provider = ""
        # Keep stale ONNX-only settings out of torch service units.
        onnx_file = ""
        artifact_sha256 = ""
    try:
        if int(max_sequence_length) < 0:
            raise ValueError
    except ValueError as exc:
        raise RuntimeError(
            "MEMENTO_EMBEDDING_MAX_SEQUENCE_LENGTH must be a non-negative integer"
        ) from exc
    try:
        if not 1 <= int(cpu_threads) <= 64:
            raise ValueError
    except ValueError as exc:
        raise RuntimeError(
            "MEMENTO_EMBEDDING_CPU_THREADS must be an integer from 1 through 64"
        ) from exc
    if artifact_sha256 and (
        len(artifact_sha256) != 64
        or any(char not in "0123456789abcdef" for char in artifact_sha256)
    ):
        raise RuntimeError(
            "MEMENTO_EMBEDDING_ARTIFACT_SHA256 must be 64 lowercase hex characters"
        )
    return {
        "backend": _safe_service_value("MEMENTO_EMBEDDING_BACKEND", backend),
        "device": _safe_service_value("MEMENTO_EMBEDDING_DEVICE", device),
        "model_revision": _safe_service_value(
            "MEMENTO_EMBEDDING_MODEL_REVISION", model_revision
        ),
        "query_prefix": _safe_service_value(
            "MEMENTO_EMBEDDING_QUERY_PREFIX", query_prefix
        ),
        "document_prefix": _safe_service_value(
            "MEMENTO_EMBEDDING_DOCUMENT_PREFIX", document_prefix
        ),
        "max_sequence_length": _safe_service_value(
            "MEMENTO_EMBEDDING_MAX_SEQUENCE_LENGTH", max_sequence_length
        ),
        "cpu_threads": _safe_service_value(
            "MEMENTO_EMBEDDING_CPU_THREADS", cpu_threads
        ),
        "onnx_provider": _safe_service_value(
            "MEMENTO_EMBEDDING_ONNX_PROVIDER", onnx_provider
        ),
        "onnx_file": _safe_service_value("MEMENTO_EMBEDDING_ONNX_FILE", onnx_file),
        "artifact_sha256": _safe_service_value(
            "MEMENTO_EMBEDDING_ARTIFACT_SHA256", artifact_sha256
        ),
    }


def install_torch_and_transformers() -> None:
    py = _venv_python()
    runtime = runtime_env_from_environ()
    backend = runtime["backend"]
    accel = detect_accelerator()
    info(f"Detected accelerator: {accel}; embedding backend={backend}")

    # torch>=2.6 — transformers 5.x refuses to call torch.load on anything
    # older after CVE-2025-32434 (a deserialization gadget that bypasses
    # weights_only=True). BGE-M3's pytorch_model.bin only loads through
    # torch.load, so without this pin you can download 2.27 GB and then
    # eat a ValueError on the very last step.
    torch_spec = "torch>=2.6"
    want_cuda_torch = backend == "torch" and (
        runtime["device"] == "cuda"
        or (runtime["device"] == "auto" and accel == "cuda")
    )

    if want_cuda_torch:
        # Default PyPI ships CUDA-enabled torch wheels from 2.6 onwards
        # (CUDA libs come via nvidia-cuda-* sub-packages auto-resolved
        # as deps). The old cu121 download.pytorch.org index caps at
        # torch 2.5.1, which trips CVE-2025-32434's torch.load guard.
        info("Installing torch with CUDA support (default PyPI)…")
        subprocess.run(
            [str(py), "-m", "pip", "install", torch_spec],
            check=True,
        )
    elif backend == "torch" and accel == "mps" and runtime["device"] in {"auto", "mps"}:
        info("Installing torch (MPS is built into the standard wheel on arm64 macOS)…")
        subprocess.run([str(py), "-m", "pip", "install", torch_spec], check=True)
    else:
        if backend == "torch" and runtime["device"] == "auto" and accel == "cpu":
            warn("No GPU detected — embedding will run on CPU and will be slow.")
        info("Installing torch (CPU wheel index)…")
        subprocess.run(
            [
                str(py), "-m", "pip", "install",
                "--extra-index-url", "https://download.pytorch.org/whl/cpu",
                torch_spec,
            ],
            check=True,
        )

    if backend == "onnx":
        info("Installing sentence-transformers[onnx] + onnxruntime + modelscope…")
        pkgs = [
            "sentence-transformers[onnx]>=3.0",
            "onnxruntime>=1.16",
            "modelscope",
        ]
        if (os.environ.get("MEMENTO_EMBEDDING_ONNX_QUANTIZE", "") or "").strip() == "1":
            pkgs.append("optimum[onnxruntime]>=1.17")
        subprocess.run([str(py), "-m", "pip", "install", *pkgs], check=True)
    else:
        info("Installing sentence-transformers + modelscope…")
        subprocess.run(
            [str(py), "-m", "pip", "install",
             "sentence-transformers>=3.0", "modelscope"],
            check=True,
        )
    ok("Python dependencies installed.")


def predownload_model(model: str = "BAAI/bge-m3") -> str:
    """Pre-fetch the embedding model. Returns the identifier the embedding
    server should load (either an absolute local path when ModelScope cached
    it, or the original HF id when the HF code path succeeded).

    Download source priority — picks the first one that works:
      1. ModelScope (best from inside China, no network shenanigans)
      2. HuggingFace direct
      3. hf-mirror.com (HK-based HF mirror)

    When MEMENTO_EMBEDDING_BACKEND=onnx and MEMENTO_EMBEDDING_ONNX_QUANTIZE=1,
    exports a dynamic int8 ONNX weight via sentence-transformers'
    export_dynamic_quantized_onnx_model (real API; not a fake flag).
    """
    py = _venv_python()
    runtime = runtime_env_from_environ()
    backend = runtime["backend"]
    info(f"Pre-downloading {model} (~1.3GB, may take minutes)…")

    load_kwargs = ""
    if backend == "onnx":
        load_kwargs = (
            ", backend='onnx', device='cpu', "
            "model_kwargs={'provider': 'CPUExecutionProvider'}"
        )

    # 1) ModelScope — returns absolute path on success.
    ms_code = (
        "from modelscope import snapshot_download;"
        f"p = snapshot_download({model!r});"
        "print(p, end='')"
    )
    local_path = None
    try:
        r = subprocess.run(
            [str(py), "-c", ms_code],
            check=True, capture_output=True, text=True, timeout=1800,
        )
        local_path = r.stdout.strip()
        if local_path and Path(local_path).exists():
            # Smoke test: sentence-transformers must be able to load it.
            verify = (
                "from sentence_transformers import SentenceTransformer;"
                f"SentenceTransformer({local_path!r}{load_kwargs})"
            )
            subprocess.run([str(py), "-c", verify], check=True)
            ok(f"Model downloaded via ModelScope → {local_path}")
        else:
            local_path = None
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "")[:300]
        warn(f"ModelScope download failed ({stderr.strip()}) — falling back to HuggingFace…")
        local_path = None
    except subprocess.TimeoutExpired:
        warn("ModelScope download timed out — falling back to HuggingFace…")
        local_path = None

    if local_path is None:
        # 2) HuggingFace direct.
        hf_code = (
            "from sentence_transformers import SentenceTransformer;"
            f"SentenceTransformer({model!r}{load_kwargs})"
        )
        try:
            subprocess.run([str(py), "-c", hf_code], check=True)
            ok("Model downloaded via HuggingFace.")
            local_path = model
        except subprocess.CalledProcessError:
            warn("HuggingFace direct download failed — retrying via hf-mirror.com…")
            # 3) hf-mirror.com.
            env = os.environ.copy()
            env["HF_ENDPOINT"] = "https://hf-mirror.com"
            subprocess.run([str(py), "-c", hf_code], env=env, check=True)
            ok("Model downloaded via hf-mirror.com.")
            local_path = model

    quantize = (os.environ.get("MEMENTO_EMBEDDING_ONNX_QUANTIZE", "") or "").strip() == "1"
    if backend == "onnx" and quantize:
        qconfig = (
            os.environ.get("MEMENTO_EMBEDDING_ONNX_QUANTIZATION_CONFIG", "avx2") or "avx2"
        ).strip()
        out_dir = VENV_DIR / "onnx-model"
        out_dir.mkdir(parents=True, exist_ok=True)
        info(f"Exporting dynamic int8 ONNX ({qconfig}) → {out_dir}…")
        quant_code = f"""
import shutil
from pathlib import Path
from sentence_transformers import SentenceTransformer, export_dynamic_quantized_onnx_model
out = Path({str(out_dir)!r})
model = SentenceTransformer(
    {local_path!r},
    backend='onnx',
    device='cpu',
    model_kwargs={{'provider': 'CPUExecutionProvider'}},
)
model.save_pretrained(str(out))
for artifact in out.rglob('*'):
    if artifact.is_symlink():
        target = artifact.resolve()
        artifact.unlink()
        shutil.copy2(target, artifact)
model = SentenceTransformer(
    str(out),
    backend='onnx',
    device='cpu',
    model_kwargs={{'provider': 'CPUExecutionProvider'}},
)
export_dynamic_quantized_onnx_model(
    model=model,
    quantization_config={qconfig!r},
    model_name_or_path=str(out),
)
candidates = sorted(out.rglob('model_qint8_{qconfig}*.onnx'))
candidates += sorted(out.rglob('model_quint8_{qconfig}*.onnx'))
if not candidates:
    candidates = sorted(out.rglob('model_qint8_*.onnx'))
    candidates += sorted(out.rglob('model_quint8_*.onnx'))
if not candidates:
    raise SystemExit(
        'quantize produced no model_qint8_*.onnx or model_quint8_*.onnx'
    )
print(candidates[0].relative_to(out).as_posix(), end='')
"""
        r = subprocess.run(
            [str(py), "-c", quant_code],
            check=True, capture_output=True, text=True, timeout=3600,
        )
        onnx_name = r.stdout.strip()
        os.environ["MEMENTO_EMBEDDING_ONNX_FILE"] = onnx_name
        ok(f"Quantized ONNX ready: {out_dir / onnx_name}")
        return str(out_dir)

    return local_path


# ── platform-specific service install ─────────────────────────
def _render(template: str, **vars: str) -> str:
    # Literal {name} replacement (not str.format) so values never reinterpret braces.
    out = template
    for key, value in vars.items():
        out = out.replace("{" + key + "}", value)
    return out


def _template_vars(model: str, *, wanted_by: str | None = None) -> dict[str, str]:
    runtime = runtime_env_from_environ()
    path = _safe_service_value(
        "PATH",
        os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    )
    vars: dict[str, str] = {
        "python": _safe_service_value("python", str(_venv_python())),
        "pythonw": _safe_service_value("pythonw", str(_venv_pythonw())),
        "repo": _safe_service_value("repo", str(REPO_ROOT)),
        "model": _safe_service_value("MEMENTO_EMBEDDING_MODEL_NAME", model),
        "path": path,
        "backend": runtime["backend"],
        "device": runtime["device"],
        "model_revision": runtime["model_revision"],
        "query_prefix": runtime["query_prefix"],
        "document_prefix": runtime["document_prefix"],
        "max_sequence_length": runtime["max_sequence_length"],
        "cpu_threads": runtime["cpu_threads"],
        "onnx_provider": runtime["onnx_provider"],
        "onnx_file": runtime["onnx_file"],
        "artifact_sha256": runtime["artifact_sha256"],
    }
    if wanted_by is not None:
        vars["wanted_by"] = _safe_service_value("wanted_by", wanted_by)
    return vars


def install_macos(model: str = "BAAI/bge-m3") -> None:
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    # Migrate: unload + remove legacy com.dailyreport.embedding plist.
    uid = os.getuid()
    legacy = agents / "com.dailyreport.embedding.plist"
    if legacy.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/com.dailyreport.embedding"],
                       capture_output=True)
        subprocess.run(["launchctl", "unload", str(legacy)], capture_output=True)
        legacy.unlink()
        info(f"Migrated: removed legacy {legacy.name}")

    plist_path = agents / "com.memento.embedding.plist"
    logdir = Path.home() / "Library" / "Logs" / "memento"
    logdir.mkdir(parents=True, exist_ok=True)

    vars = _template_vars(model)
    vars["logdir"] = _safe_service_value("logdir", str(logdir))
    body = _render(
        (TEMPLATE_DIR / "memento-embedding.plist.tmpl").read_text(),
        **vars,
    )
    plist_path.write_text(body)

    label = "com.memento.embedding"
    # Try `bootout` to cleanly unload if already present; ignore errors.
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{label}"],
        capture_output=True,
    )
    # Bootstrap (newer), fall back to load (older macOS).
    r = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
        capture_output=True,
    )
    if r.returncode != 0:
        subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    ok(f"launchd service installed: {plist_path.name}")


def install_linux(model: str = "BAAI/bge-m3") -> None:
    # Two install modes — picked by effective uid:
    #   - non-root → user-scope unit in ~/.config/systemd/user/.
    #     Standard for desktops / multi-user boxes.
    #   - root    → system-scope unit in /etc/systemd/system/.
    #     Standard for headless servers, where root typically has no user
    #     dbus session and `systemctl --user daemon-reload` fails with
    #     "Failed to connect to bus: $DBUS_SESSION_BUS_ADDRESS … not defined".
    is_system = os.geteuid() == 0

    if is_system:
        unit_dir = Path("/etc/systemd/system")
        ctl = ["systemctl"]
        wanted_by = "multi-user.target"
        logdir = Path("/var/log/memento")
        scope_label = "system"
    else:
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        ctl = ["systemctl", "--user"]
        wanted_by = "default.target"
        logdir = Path.home() / ".local" / "share" / "memento" / "logs"
        scope_label = "user"

    unit_dir.mkdir(parents=True, exist_ok=True)
    logdir.mkdir(parents=True, exist_ok=True)

    # Migrate: disable + remove legacy dr-embedding.service from BOTH
    # locations — installs may have switched scope across upgrades.
    for legacy_dir, legacy_ctl in (
        (Path.home() / ".config" / "systemd" / "user", ["systemctl", "--user"]),
        (Path("/etc/systemd/system"), ["systemctl"]),
    ):
        legacy = legacy_dir / "dr-embedding.service"
        if legacy.exists():
            subprocess.run(
                [*legacy_ctl, "disable", "--now", "dr-embedding"],
                capture_output=True,
            )
            try:
                legacy.unlink()
                info(f"Migrated: removed legacy {legacy}")
            except (PermissionError, OSError):
                pass

    unit_path = unit_dir / "memento-embedding.service"

    vars = _template_vars(model, wanted_by=wanted_by)
    vars["logdir"] = _safe_service_value("logdir", str(logdir))
    body = _render(
        (TEMPLATE_DIR / "memento-embedding.service.tmpl").read_text(),
        **vars,
    )
    unit_path.write_text(body)

    subprocess.run([*ctl, "daemon-reload"], check=True)
    subprocess.run([*ctl, "enable", "--now", "memento-embedding"], check=True)
    ok(f"systemd {scope_label} service installed: {unit_path}")

    if not is_system:
        # Lingering so service survives logout (headless server use case).
        # System-scope services don't need this — they start at boot.
        r = subprocess.run(
            ["loginctl", "show-user", os.environ.get("USER", ""), "--property=Linger"],
            capture_output=True, text=True,
        )
        if "Linger=no" in r.stdout:
            warn(
                "Service will stop when you log out. To keep it running headless, run:\n"
                f"    sudo loginctl enable-linger {os.environ.get('USER', '$USER')}"
            )


def install_windows(model: str = "BAAI/bge-m3") -> None:
    import tempfile
    # Migrate: delete legacy DailyReportEmbedding task.
    subprocess.run(
        ["schtasks", "/Delete", "/TN", "DailyReportEmbedding", "/F"],
        capture_output=True,
    )
    logdir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "memento" / "logs"
    logdir.mkdir(parents=True, exist_ok=True)

    vars = _template_vars(model)
    body = _render(
        (TEMPLATE_DIR / "memento-embedding-task.xml.tmpl").read_text(),
        **vars,
    )
    # schtasks requires UTF-16 encoding on disk.
    tmp = Path(tempfile.gettempdir()) / "memento-embedding-task.xml"
    tmp.write_text(body, encoding="utf-16")

    # Remove any existing task, then create fresh.
    subprocess.run(
        ["schtasks", "/Delete", "/TN", "MementoEmbedding", "/F"],
        capture_output=True,
    )
    subprocess.run(
        ["schtasks", "/Create", "/TN", "MementoEmbedding",
         "/XML", str(tmp), "/F"],
        check=True,
    )
    subprocess.run(
        ["schtasks", "/Run", "/TN", "MementoEmbedding"],
        check=False,
    )
    ok("Task Scheduler task 'MementoEmbedding' installed and started.")


def install() -> None:
    """Full install flow: venv → deps → model → platform service."""
    create_venv()
    install_torch_and_transformers()
    model_id = predownload_model()
    # Re-read after optional quantize mutates MEMENTO_EMBEDDING_ONNX_FILE.
    _ = runtime_env_from_environ()
    if IS_MAC:
        install_macos(model=model_id)
    elif IS_LINUX:
        install_linux(model=model_id)
    elif IS_WINDOWS:
        install_windows(model=model_id)
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")
    print()
    ok("Embedding service running on http://localhost:8002")


# ── uninstall ────────────────────────────────────────────────
def uninstall(remove_model_cache: bool = False, remove_venv: bool = False) -> None:
    """Remove the platform service, optionally the venv and model cache."""
    if IS_MAC:
        uid = os.getuid()
        agents = Path.home() / "Library" / "LaunchAgents"
        for label in ("com.memento.embedding", "com.dailyreport.embedding"):
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                           capture_output=True)
            plist = agents / f"{label}.plist"
            if plist.exists():
                plist.unlink()
                ok(f"Removed launchd plist: {plist.name}")
        # Clean launchd-managed logs from either legacy or new location
        for logdir_name in ("memento", "daily_report"):
            for name in ("embedding_stdout.log", "embedding_stderr.log"):
                p = Path.home() / "Library" / "Logs" / logdir_name / name
                if p.exists():
                    p.unlink()
    elif IS_LINUX:
        # Clean both scopes — across upgrades the install path may have
        # switched between user and system depending on whether the
        # installer was run as root.
        for unit_dir, ctl in (
            (Path.home() / ".config" / "systemd" / "user", ["systemctl", "--user"]),
            (Path("/etc/systemd/system"), ["systemctl"]),
        ):
            for name in ("memento-embedding", "dr-embedding"):
                subprocess.run([*ctl, "disable", "--now", name], capture_output=True)
                unit = unit_dir / f"{name}.service"
                if unit.exists():
                    try:
                        unit.unlink()
                        ok(f"Removed systemd unit: {unit}")
                    except (PermissionError, OSError) as e:
                        warn(f"Couldn't remove {unit}: {e}")
    elif IS_WINDOWS:
        for task in ("MementoEmbedding", "DailyReportEmbedding"):
            subprocess.run(
                ["schtasks", "/Delete", "/TN", task, "/F"],
                capture_output=True,
            )
        ok("Removed Scheduled Task.")

    if remove_venv and VENV_DIR.exists():
        import shutil
        shutil.rmtree(VENV_DIR, ignore_errors=True)
        ok(f"Removed embedding venv {VENV_DIR.name}/")

    if remove_model_cache:
        # BGE-M3 may live in either cache depending on which source the
        # installer fell through to.
        import shutil
        # HuggingFace cache layout
        for cache_root in (
            Path.home() / ".cache" / "huggingface" / "hub",
            Path(os.environ.get("HF_HOME", "")) / "hub" if os.environ.get("HF_HOME") else None,
        ):
            if not cache_root:
                continue
            model_dir = cache_root / "models--BAAI--bge-m3"
            if model_dir.exists():
                shutil.rmtree(model_dir, ignore_errors=True)
                ok(f"Removed model cache {model_dir}")
        # ModelScope cache layout
        ms_dir = Path.home() / ".cache" / "modelscope" / "hub" / "BAAI" / "bge-m3"
        if ms_dir.exists():
            shutil.rmtree(ms_dir, ignore_errors=True)
            ok(f"Removed model cache {ms_dir}")
