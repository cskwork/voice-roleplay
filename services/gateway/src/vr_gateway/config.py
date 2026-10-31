"""Gateway configuration loaded from services/gateway/config.toml plus env overrides.

Two independent switches (PROTOCOL §1, §7):
- `mode`: "local" (default, the four local model workers) or "api" (hosted providers through adapters in
  `vr_gateway.adapters`; only Silero VAD runs locally). API keys come only from environment variables and are never
  written to disk or logged.
- `shared`: many learners on one gateway (sessions per `vr_sid` cookie, a concurrency cap, a maximum session length,
  history and review storage off). Independent of `mode`.
`public_origins` and `path_prefix` let a reverse proxy on another origin (e.g. Content Forge at `/voice/`) forward
HTTP and WebSocket requests with its own Host/Origin headers.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = GATEWAY_DIR.parents[1]
ASSETS_DIR = GATEWAY_DIR / "assets"

MIB = 1024 * 1024
MODES = ("local", "api")
# Environment variables holding provider keys in API mode (PROTOCOL §1). Only presence is ever reported.
API_KEY_ENV = {"openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_API_KEY", "deepgram": "DEEPGRAM_API_KEY"}
# Speech in API mode: Deepgram Aura-2 through OpenRouter (default) or Deepgram directly (opt-in fallback), each with
# its own voice list.
TTS_VOICES = {"openrouter": "config/voices.openrouter.json", "deepgram": "config/voices.api.json"}
# Port overrides for a machine where a default port is taken: gateway env var -> (worker env var it sets).
WORKER_PORT_ENV = {"asr": ("VR_ASR_PORT", "ASR_PORT"), "tts": ("VR_TTS_PORT", "VR_TTS_PORT"), "llm": ("VR_LLM_PORT", None),
                   "pron": ("VR_PRON_PORT", "VR_PRON_PORT")}


@dataclass
class WorkerProcess:
    name: str
    cmd: list[str]
    cwd: Path
    health_url: str
    env: dict[str, str] = field(default_factory=dict)  # values may use ${VR_WORKER_TOKEN}
    optional: bool = False  # not installed -> skipped at start instead of failing the stack (pronunciation worker)


@dataclass
class ApiConfig:
    """Hosted providers used in API mode (PROTOCOL §1). Keys are read from the environment, never from files."""

    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "openai/gpt-4.1-mini"
    # OpenRouter provider routing: only these upstreams, no fallbacks, zero data retention, no data collection.
    llm_providers: list[str] = field(default_factory=lambda: ["openai", "azure"])
    llm_timeout_s: float = 60.0
    asr_url: str = "wss://api.openai.com/v1/realtime?intent=transcription"
    asr_model: str = "gpt-live-transcribe"
    tts_provider: str = "openrouter"  # openrouter | deepgram
    # tts_provider "openrouter" (default): POST {tts_base_url}/audio/speech with tts_model, zero data retention.
    tts_base_url: str = "https://openrouter.ai/api/v1"
    tts_model: str = "deepgram/aura-2"
    # tts_provider "deepgram" (opt-in fallback): Deepgram's own endpoints, needs DEEPGRAM_API_KEY.
    tts_url: str = "wss://api.deepgram.com/v1/speak"
    tts_rest_url: str = "https://api.deepgram.com/v1/speak"
    voices: Path | None = None  # None = the provider's default voice list (TTS_VOICES)
    # The optional local pronunciation worker stays off in API mode unless turned on here (needs its local models).
    pronunciation: bool = False
    keys: dict[str, str] = field(default_factory=dict, repr=False)

    def key(self, provider: str) -> str:
        return self.keys.get(provider, "")

    def providers(self) -> list[str]:
        """Providers this configuration calls: OpenRouter (LLM, and TTS by default) and OpenAI (ASR); Deepgram only
        when it is the TTS provider."""
        return ["openrouter", "openai", *(["deepgram"] if self.tts_provider == "deepgram" else [])]

    def key_presence(self) -> dict[str, bool]:
        return {name: bool(self.keys.get(name)) for name in self.providers()}

    def voices_path(self) -> Path:
        return self.voices or REPO_ROOT / TTS_VOICES[self.tts_provider]


def read_api_keys(environ=os.environ) -> dict[str, str]:
    return {name: environ.get(var, "").strip() for name, var in API_KEY_ENV.items()}


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8710
    web_dist: Path = REPO_ROOT / "apps/web/dist"
    scenarios_dir: Path = REPO_ROOT / "content/scenarios"
    scenario_schema: Path = REPO_ROOT / "contracts/scenario.schema.json"
    data_dir: Path = REPO_ROOT / "var/data"
    cache_dir: Path = REPO_ROOT / "var/cache"
    log_dir: Path = REPO_ROOT / "var/log"
    run_dir: Path = REPO_ROOT / "var/run"  # worker pidfiles, used by `./app stop` to clean up leftovers
    vad_model: Path = ASSETS_DIR / "silero_vad.onnx"
    llm_model_revision: str = "unknown"
    asr_url: str = "http://127.0.0.1:8711"
    tts_url: str = "http://127.0.0.1:8712"
    llm_url: str = "http://127.0.0.1:8713"
    # Optional pronunciation worker (PROTOCOL §12). "" = not configured: attempts get pronunciation.status "unavailable".
    pron_url: str = ""
    # Guide content (PA-8) and the CMUdict file used to link a word to guide entries in timing_only mode.
    pron_guide: Path = REPO_ROOT / "content/pronunciation/guide.json"
    pron_lexicon: Path = REPO_ROOT / "models/cmudict/cmudict.dict"
    worker_token: str = field(default="", repr=False)
    processes: list[WorkerProcess] = field(default_factory=list)
    mode: str = "local"  # local | api
    api: ApiConfig = field(default_factory=ApiConfig)

    # Reverse proxy support: extra allowed origins (scheme://host[:port]) whose hosts are also allowed as Host, and a
    # path prefix (e.g. "/voice") that is stripped before routing.
    public_origins: list[str] = field(default_factory=list)
    path_prefix: str = ""

    # Shared mode (several learners on one gateway).
    shared: bool = False
    max_sessions: int = 35  # live realtime sessions across all learners
    max_session_s: float = 1800.0  # a realtime session is ended after this long (0 = no limit)
    job_concurrency: int = 1  # recorded-practice jobs processed at the same time

    # Limits (PROTOCOL §6-§7)
    max_wav_bytes: int = 32 * MIB
    max_json_bytes: int = 256 * 1024
    max_audio_s: float = 120.0
    job_queue_capacity: int = 2
    job_ttl_s: float = 300.0
    summary_ttl_s: float = 900.0
    realtime_idle_grace_s: float = 120.0
    health_cache_s: float = 2.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.sqlite3"

    @property
    def allowed_origins(self) -> set[str]:
        return {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}", *self.public_origins}

    @property
    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}", *(origin_host(o) for o in self.public_origins)}

    @property
    def history_enabled(self) -> bool:
        """History and spaced review are stored only for a single local learner (off in shared mode)."""
        return not self.shared


def origin_host(origin: str) -> str:
    """'https://example.com' -> 'example.com'; 'http://host:8080' -> 'host:8080' (the Host header form)."""
    return origin.split("://", 1)[-1].rstrip("/")


def normalize_prefix(prefix: str) -> str:
    """'' or '/' -> ''; 'voice/' -> '/voice'. Only [A-Za-z0-9_-] path segments are accepted."""
    parts = [p for p in prefix.strip().split("/") if p]
    for part in parts:
        if not all(c.isalnum() or c in "-_" for c in part):
            raise ValueError(f"invalid path_prefix: {prefix!r}")
    return "/" + "/".join(parts) if parts else ""


def normalize_origin(origin: str) -> str:
    origin = origin.strip().rstrip("/")
    scheme, sep, rest = origin.partition("://")
    if not sep or scheme not in ("http", "https") or not rest or "/" in rest:
        raise ValueError(f"invalid public origin (expected scheme://host[:port]): {origin!r}")
    return f"{scheme}://{rest.lower()}"


def _env_bool(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return None
    return value.strip().lower() in ("1", "true", "yes", "on")


def _repo_path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else REPO_ROOT / p


def load_config(path: Path | None = None) -> Config:
    path = path or GATEWAY_DIR / "config.toml"
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    gw = raw.get("gateway", {})
    wk = raw.get("workers", {})
    cfg = Config()
    cfg.host = gw.get("host", cfg.host)
    cfg.port = int(os.environ.get("VR_GATEWAY_PORT", gw.get("port", cfg.port)))
    for key in ("web_dist", "scenarios_dir", "scenario_schema", "data_dir", "cache_dir", "log_dir", "run_dir",
                "pron_guide", "pron_lexicon"):
        if key in gw:
            setattr(cfg, key, _repo_path(gw[key]))
    if os.environ.get("VR_DATA_DIR"):
        cfg.data_dir = Path(os.environ["VR_DATA_DIR"])
    if os.environ.get("VR_CACHE_DIR"):
        cfg.cache_dir = Path(os.environ["VR_CACHE_DIR"])
    cfg.llm_model_revision = gw.get("llm_model_revision", cfg.llm_model_revision)
    cfg.asr_url = wk.get("asr_url", cfg.asr_url)
    cfg.tts_url = wk.get("tts_url", cfg.tts_url)
    cfg.llm_url = wk.get("llm_url", cfg.llm_url)
    cfg.pron_url = wk.get("pron_url", cfg.pron_url)
    cfg.worker_token = os.environ.get("VR_WORKER_TOKEN", "")
    _load_modes(cfg, gw, raw.get("api", {}), raw.get("shared", {}))
    for name, spec in raw.get("supervisor", {}).items():
        env: dict[str, str] = {}
        if "server_json" in spec:
            server_path = _repo_path(spec["server_json"])
            if not server_path.exists():
                continue  # launcher config not installed; the supervisor cannot manage this process
            server = json.loads(server_path.read_text())
            cmd = [server["binary"], *server["args"]]
            env = dict(server.get("env", {}))
        else:
            cmd = list(spec["cmd"])
        # Relative executables that look like paths, and model paths, resolve against the repo root.
        if "/" in cmd[0] and not Path(cmd[0]).is_absolute():
            cmd[0] = str(REPO_ROOT / cmd[0])
        cmd = [str(REPO_ROOT / a) if a.startswith("models/") else a for a in cmd]
        health_url = spec["health_url"]
        gw_var, worker_var = WORKER_PORT_ENV.get(name, (None, None))
        if gw_var and os.environ.get(gw_var):
            port = str(int(os.environ[gw_var]))
            health_url = f"http://127.0.0.1:{port}/health"
            setattr(cfg, f"{name}_url", f"http://127.0.0.1:{port}")
            if worker_var:
                env[worker_var] = port
            elif "--port" in cmd:
                cmd[cmd.index("--port") + 1] = port
        cfg.processes.append(WorkerProcess(name=name, cmd=cmd, cwd=_repo_path(spec.get("cwd", ".")),
                                           health_url=health_url, env=env, optional=bool(spec.get("optional"))))
    for name, (gw_var, _) in WORKER_PORT_ENV.items():  # also without a supervisor entry
        if os.environ.get(gw_var) and (name != "pron" or cfg.pron_url):
            setattr(cfg, f"{name}_url", f"http://127.0.0.1:{int(os.environ[gw_var])}")
    if cfg.mode == "api":
        # No local ASR/TTS/LLM processes; the optional pronunciation worker only when turned on.
        cfg.processes = [p for p in cfg.processes if p.name == "pron" and cfg.api.pronunciation]
        if not cfg.api.pronunciation:
            cfg.pron_url = ""
    return cfg


def _load_modes(cfg: Config, gw: dict, api: dict, shared: dict) -> None:
    """mode / api / shared / proxy settings: config.toml first, then VR_* environment overrides."""
    cfg.mode = os.environ.get("VR_MODE", "").strip().lower() or gw.get("mode", cfg.mode)
    if cfg.mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {cfg.mode!r}")
    a = cfg.api
    for key in ("llm_base_url", "llm_model", "asr_url", "asr_model", "tts_provider", "tts_base_url", "tts_model",
                "tts_url", "tts_rest_url"):
        if key in api:
            setattr(a, key, str(api[key]))
    a.tts_provider = (os.environ.get("VR_API_TTS_PROVIDER", "").strip() or a.tts_provider).lower()
    if a.tts_provider not in TTS_VOICES:
        raise ValueError(f"tts_provider must be one of {tuple(TTS_VOICES)}, got {a.tts_provider!r}")
    if "llm_providers" in api:
        a.llm_providers = [str(p) for p in api["llm_providers"]]
    if "llm_timeout_s" in api:
        a.llm_timeout_s = float(api["llm_timeout_s"])
    if "voices" in api:
        a.voices = _repo_path(api["voices"])
    a.pronunciation = bool(api.get("pronunciation", a.pronunciation))
    a.llm_model = os.environ.get("VR_API_LLM_MODEL", "").strip() or a.llm_model
    a.keys = read_api_keys()
    if cfg.mode == "api":
        cfg.llm_model_revision = f"openrouter:{a.llm_model}"

    origins = os.environ.get("VR_PUBLIC_ORIGINS")
    raw_origins = [o for o in origins.split(",") if o.strip()] if origins is not None else gw.get("public_origins", [])
    cfg.public_origins = [normalize_origin(o) for o in raw_origins]
    prefix = os.environ.get("VR_PATH_PREFIX")
    cfg.path_prefix = normalize_prefix(prefix if prefix is not None else gw.get("path_prefix", ""))

    env_shared = _env_bool("VR_SHARED")
    cfg.shared = env_shared if env_shared is not None else bool(shared.get("enabled", cfg.shared))
    cfg.max_sessions = int(os.environ.get("VR_MAX_SESSIONS") or shared.get("max_sessions", cfg.max_sessions))
    cfg.max_session_s = float(os.environ.get("VR_MAX_SESSION_S") or shared.get("max_session_s", cfg.max_session_s))
    if cfg.shared:
        cfg.job_concurrency = int(os.environ.get("VR_JOB_CONCURRENCY") or shared.get("job_concurrency", 4))
        cfg.job_queue_capacity = int(shared.get("job_queue_capacity", max(cfg.job_queue_capacity, cfg.max_sessions)))
    if cfg.max_sessions < 1 or cfg.job_concurrency < 1:
        raise ValueError("max_sessions and job_concurrency must be at least 1")
