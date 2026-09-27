"""Gateway configuration loaded from services/gateway/config.toml plus env overrides."""

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


@dataclass
class WorkerProcess:
    name: str
    cmd: list[str]
    cwd: Path
    health_url: str
    env: dict[str, str] = field(default_factory=dict)  # values may use ${VR_WORKER_TOKEN}


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
    vad_model: Path = ASSETS_DIR / "silero_vad.onnx"
    llm_model_revision: str = "unknown"
    asr_url: str = "http://127.0.0.1:8711"
    tts_url: str = "http://127.0.0.1:8712"
    llm_url: str = "http://127.0.0.1:8713"
    worker_token: str = ""
    processes: list[WorkerProcess] = field(default_factory=list)

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
        return {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}

    @property
    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}


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
    for key in ("web_dist", "scenarios_dir", "scenario_schema", "data_dir", "cache_dir", "log_dir"):
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
    cfg.worker_token = os.environ.get("VR_WORKER_TOKEN", "")
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
        cfg.processes.append(WorkerProcess(name=name, cmd=cmd, cwd=_repo_path(spec.get("cwd", ".")),
                                           health_url=spec["health_url"], env=env))
    return cfg
