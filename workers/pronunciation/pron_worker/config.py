import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

SAMPLE_RATE = 16_000
ALIGNER_ID = "Qwen/Qwen3-ForcedAligner-0.6B"
ALIGNER_REVISION = "c7cbfc2048c462b0d63a45797104fc9db3ad62b7"
ALIGNER_FILES = ("config.json", "model.safetensors", "preprocessor_config.json", "vocab.json", "merges.txt")
LANGUAGE = "English"
# Phone recogniser for /assess (GOP). Optional: without these files /assess answers 501.
PHONES_ID = "facebook/wav2vec2-lv-60-espeak-cv-ft"
PHONES_REVISION = "ae45363bf3413b374fecd9dc8bc1df0e24c3b7f4"
PHONES_FILES = ("config.json", "pytorch_model.bin", "vocab.json")
LEXICON_SOURCE = "cmusphinx/cmudict@74790861f652b15e4ac49015a90074ad62a27690 cmudict.dict"
CALIBRATION_VERSION = "so762-ctcgop-2026-09-28"  # workers/pronunciation/calibration/<version>.json

MAX_AUDIO_BYTES = 120 * SAMPLE_RATE * 2  # 3 840 000 bytes of PCM16 = 120 s
# base64 grows 4/3; a little slack for padding. Checked before decoding.
MAX_BODY_BYTES = MAX_AUDIO_BYTES * 4 // 3 + 64 * 1024
MAX_TEXT_CHARS = 4000  # a 120 s unscripted answer transcript stays well below this


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    token: str
    port: int
    model_dir: Path
    device: str  # "auto" | "mps" | "cpu"
    experimental: bool  # VR_PRON_EXPERIMENTAL=1: /assess may return bands (only with a calibration file)
    exit_on_load_failure: bool = True
    phones_dir: Path | None = None  # None / missing files = /assess not available (501)
    lexicon_path: Path | None = None
    calibration_path: Path | None = None  # None / missing file = no bands

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("VR_WORKER_TOKEN", "")
        if not token:
            raise ConfigError(
                "VR_WORKER_TOKEN is not set. The gateway passes it when it starts the worker; "
                "for manual runs export any random value, e.g. VR_WORKER_TOKEN=$(openssl rand -hex 16)."
            )
        device = os.environ.get("PRON_DEVICE", "auto")
        if device not in ("auto", "mps", "cpu"):
            raise ConfigError(f"Unknown PRON_DEVICE={device!r} (expected 'auto', 'mps' or 'cpu').")
        return cls(
            token=token,
            port=int(os.environ.get("VR_PRON_PORT", "8714")),
            model_dir=Path(os.environ.get("PRON_ALIGNER_DIR", REPO_ROOT / "models" / "Qwen3-ForcedAligner-0.6B")),
            device=device,
            experimental=os.environ.get("VR_PRON_EXPERIMENTAL", "") == "1",
            phones_dir=Path(os.environ.get("PRON_PHONES_DIR", REPO_ROOT / "models" / "wav2vec2-lv-60-espeak-cv-ft")),
            lexicon_path=Path(os.environ.get("PRON_LEXICON", REPO_ROOT / "models" / "cmudict" / "cmudict.dict")),
            calibration_path=Path(
                os.environ.get("PRON_CALIBRATION", Path(__file__).resolve().parents[1] / "calibration" / f"{CALIBRATION_VERSION}.json")
            ),
        )


def check_model_files(model_dir: Path) -> None:
    missing = [n for n in ALIGNER_FILES if not (model_dir / n).is_file()]
    if missing:
        raise ConfigError(
            f"Forced-aligner model files missing in {model_dir} ({', '.join(missing)}). "
            f"Download {ALIGNER_ID} at revision {ALIGNER_REVISION} into that directory (see workers/pronunciation/README.md)."
        )
