"""Start/stop the ASR, TTS and LLM processes with the worker token and an offline environment.

Used in-process by `python -m vr_gateway --manage-workers` (the ./app launcher). Worker output goes to
var/log/<name>.log; workers themselves are responsible for not logging text or audio (PROTOCOL §2).
Each worker runs in its own process group; its pid is written to var/run/<name>.pid so `./app stop` can
clean up if the gateway itself died without stopping them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import time

import httpx

from .config import Config, WorkerProcess

log = logging.getLogger("vr_gateway.supervisor")

OFFLINE_ENV = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "DO_NOT_TRACK": "1",
    "TOKENIZERS_PARALLELISM": "false",
}


class Supervisor:
    def __init__(self, config: Config):
        self.config = config
        self.procs: dict[str, subprocess.Popen] = {}
        self.specs: dict[str, WorkerProcess] = {p.name: p for p in config.processes}

    def env_for(self, name: str) -> dict:
        token = self.config.worker_token
        env = {**os.environ, **OFFLINE_ENV, "VR_WORKER_TOKEN": token}
        for key, value in self.specs[name].env.items():
            env[key] = value.replace("${VR_WORKER_TOKEN}", token)
        if name == "llm":
            env.setdefault("LLAMA_API_KEY", token)  # llama-server --api-key
        return env

    def start(self, name: str) -> None:
        spec = self.specs[name]
        if name in self.procs and self.procs[name].poll() is None:
            return
        if ("/" in spec.cmd[0] and not os.path.exists(spec.cmd[0])) or not shutil.which(spec.cmd[0]):
            raise FileNotFoundError(f"{name}: executable missing: {spec.cmd[0]} (run ./app setup)")
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        logfile = open(self.config.log_dir / f"{name}.log", "ab")
        self.procs[name] = subprocess.Popen(
            spec.cmd, cwd=spec.cwd, env=self.env_for(name), stdout=logfile, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        logfile.close()
        self.config.run_dir.mkdir(parents=True, exist_ok=True)
        (self.config.run_dir / f"{name}.pid").write_text(f"{self.procs[name].pid}\n")
        log.info("worker_started name=%s pid=%d", name, self.procs[name].pid)

    def start_all(self) -> None:
        try:
            for name in self.specs:
                self.start(name)
        except Exception:
            self.stop_all()  # never leave half a stack running
            raise

    async def wait_ready(self, timeout_s: float = 600.0) -> dict[str, bool]:
        """Poll each worker's /health until ready, a process exits, or the timeout passes."""
        deadline = time.monotonic() + timeout_s
        ready = {name: False for name in self.procs}
        headers = {"X-Worker-Token": self.config.worker_token}
        async with httpx.AsyncClient(headers=headers, timeout=2) as http:
            while time.monotonic() < deadline and not all(ready.values()):
                for name, proc in self.procs.items():
                    if ready[name] or proc.poll() is not None:
                        continue
                    try:
                        resp = await http.get(self.specs[name].health_url)
                        body = resp.json() if resp.status_code == 200 else {}
                    except (httpx.HTTPError, ValueError):
                        body = {}
                    ready[name] = bool(body.get("ready") or body.get("status") == "ok")
                pending = [n for n, p in self.procs.items() if not ready[n] and p.poll() is None]
                if not pending:
                    break  # everything is ready or has exited
                await asyncio.sleep(1)
        for name, proc in self.procs.items():
            if proc.poll() is not None:
                log.error("worker_exited name=%s code=%s (see var/log/%s.log)", name, proc.returncode, name)
        log.info("workers_ready %s", " ".join(f"{k}={v}" for k, v in ready.items()))
        return ready

    def status(self) -> dict[str, dict]:
        return {
            name: {"pid": proc.pid, "running": proc.poll() is None, "exit_code": proc.poll()}
            for name, proc in self.procs.items()
        }

    def stop(self, name: str, grace_s: float = 10.0) -> None:
        proc = self.procs.pop(name, None)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(grace_s)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(5)
            except ProcessLookupError:
                pass
            log.info("worker_stopped name=%s exit=%s", name, proc.returncode)
        (self.config.run_dir / f"{name}.pid").unlink(missing_ok=True)

    def stop_all(self) -> None:
        for name in list(self.procs):
            self.stop(name)
