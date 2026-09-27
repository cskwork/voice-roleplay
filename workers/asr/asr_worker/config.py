import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

SAMPLE_RATE = 16_000
MODEL_ID = "Qwen/Qwen3-ASR-0.6B"
MODEL_REVISION = "5eb144179a02acc5e5ba31e748d22b0cf3e303b0"  # pinned in PROTOCOL §1
LANGUAGE = "English"

MAX_TRANSCRIBE_BYTES = 120 * SAMPLE_RATE * 2  # 3 840 000
# Gateway auto-commits at 45 s; the extra 0.5 s absorbs the VAD pre-roll and in-flight frames.
MAX_STREAM_SAMPLES = int(45.5 * SAMPLE_RATE)
MAX_CONTEXT_CHARS = 300


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    token: str
    port: int
    model_dir: Path
    backend: str  # "transformers" | "vllm"
    device: str  # "auto" | "mps" | "cpu" | "cuda"
    partial_interval_ms: int
    partial_min_new_ms: int
    partial_window_s: float
    silence_dbfs: float
    exit_on_load_failure: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("VR_WORKER_TOKEN", "")
        if not token:
            raise ConfigError(
                "VR_WORKER_TOKEN is not set. The gateway passes it when it starts the worker; "
                "for manual runs export any random value, e.g. VR_WORKER_TOKEN=$(openssl rand -hex 16)."
            )
        backend = os.environ.get("ASR_BACKEND", "transformers")
        if backend not in ("transformers", "vllm"):
            raise ConfigError(f"Unknown ASR_BACKEND={backend!r} (expected 'transformers' or 'vllm').")
        if backend == "vllm" and sys.platform == "darwin":
            raise ConfigError("ASR_BACKEND=vllm needs Linux + NVIDIA CUDA; use ASR_BACKEND=transformers on macOS.")
        return cls(
            token=token,
            port=int(os.environ.get("ASR_PORT", "8711")),
            model_dir=Path(os.environ.get("ASR_MODEL_DIR", REPO_ROOT / "models" / "Qwen3-ASR-0.6B")),
            backend=backend,
            device=os.environ.get("ASR_DEVICE", "auto"),
            partial_interval_ms=int(os.environ.get("ASR_PARTIAL_INTERVAL_MS", "700")),
            partial_min_new_ms=int(os.environ.get("ASR_PARTIAL_MIN_NEW_MS", "300")),
            partial_window_s=float(os.environ.get("ASR_PARTIAL_WINDOW_S", "12")),
            silence_dbfs=float(os.environ.get("ASR_SILENCE_DBFS", "-40")),
        )


def check_model_files(model_dir: Path) -> None:
    missing = [n for n in ("config.json", "model.safetensors", "preprocessor_config.json") if not (model_dir / n).is_file()]
    if missing:
        raise ConfigError(
            f"ASR model files missing in {model_dir} ({', '.join(missing)}). "
            f"Download {MODEL_ID} at revision {MODEL_REVISION} into that directory (see models.lock.json)."
        )
