import numpy as np
import pytest

from tts_worker.backend import create_engine, resolve_backend
from tts_worker.engine import Engine, SetupError
from tts_worker.engine_mlx import MlxEngine, _stretch


def test_explicit_backend_is_kept():
    assert resolve_backend("torch", "fp16") == "torch"
    assert resolve_backend("mlx", "fp16") == "mlx"


def test_unknown_backend_or_variant_is_rejected():
    with pytest.raises(SetupError):
        resolve_backend("cuda", "fp16")
    with pytest.raises(SetupError):
        create_engine({"VR_TTS_BACKEND": "mlx", "VR_TTS_MLX_VARIANT": "3bit"})


def test_auto_falls_back_to_torch_without_mlx_files(monkeypatch):
    monkeypatch.setattr("tts_worker.backend.mlx_files_present", lambda variant: False)
    assert resolve_backend("auto", "fp16") == "torch"
    assert isinstance(create_engine({}), Engine)


def test_create_engine_passes_mlx_settings():
    eng = create_engine({"VR_TTS_BACKEND": "mlx", "VR_TTS_FLOW_STEPS": "6", "VR_TTS_TOKEN_HOP": "50"})
    assert isinstance(eng, MlxEngine) and (eng.flow_steps, eng.token_hop) == (6, 50)
    assert eng.model_id == "mlx-community/Fun-CosyVoice3-0.5B-2512-fp16"


@pytest.mark.parametrize("speed", [0.5, 0.7, 1.3, 2.0])
def test_stretch_matches_torch_linear_interpolate(speed):
    torch = pytest.importorskip("torch")
    mel = np.random.default_rng(0).standard_normal((1, 80, 173)).astype(np.float32)
    ref = torch.nn.functional.interpolate(torch.from_numpy(mel), size=int(173 / speed), mode="linear").numpy()
    np.testing.assert_allclose(_stretch(mel, speed), ref, atol=1e-4)  # torch computes positions in float32
