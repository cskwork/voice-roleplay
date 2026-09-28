"""Backend selection: ``VR_TTS_BACKEND=auto|mlx|torch`` (see workers/tts/README.md)."""

import importlib.util
import os
import platform
import sys

from .engine import Engine, SetupError
from .engine_mlx import MlxEngine, mlx_files_present

BACKENDS = ("auto", "mlx", "torch")


def apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def resolve_backend(requested: str, variant: str) -> str:
    """``auto`` = mlx on Apple Silicon when mlx is installed and the MLX model files are present, else torch."""
    if requested not in BACKENDS:
        raise SetupError(f"unknown VR_TTS_BACKEND {requested!r}; use {'|'.join(BACKENDS)}")
    if requested != "auto":
        return requested
    mlx_ok = apple_silicon() and importlib.util.find_spec("mlx") is not None and mlx_files_present(variant)
    return "mlx" if mlx_ok else "torch"


def create_engine(env=os.environ):
    variant = env.get("VR_TTS_MLX_VARIANT", "fp16")
    if resolve_backend(env.get("VR_TTS_BACKEND", "auto"), variant) == "mlx":
        return MlxEngine(variant=variant, flow_steps=int(env.get("VR_TTS_FLOW_STEPS", "5")),
                         token_hop=int(env.get("VR_TTS_TOKEN_HOP", "50")))
    return Engine(device=env.get("VR_TTS_DEVICE", "auto"), cpu_threads=int(env.get("VR_TTS_CPU_THREADS", "4")))
