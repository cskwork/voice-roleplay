"""Helpers for the ./app launcher (PRD §14.3). Standard library only, except `download` (huggingface_hub).

  verify   [--full]            check model files against models.lock.json (size always; sha256, cached by size+mtime)
  download                     fetch missing/mismatched model files at the pinned revisions (setup only)
  doctor   [--full]            hardware, runtimes, models, ports, voices, offline readiness as a table
  preflight                    fast checks before `./app start` (no hashing, no network)
  wait-ready PID [TIMEOUT_S]   poll the gateway's /api/health until realtime mode is available

Exit code 0 = ok, 1 = a check failed. Nothing here logs transcripts or other user data.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "models.lock.json"
HASH_CACHE = ROOT / "var" / "cache" / "model_hashes.json"
PORTS = {name: int(os.environ.get(f"VR_{name.upper()}_PORT", default))
         for name, default in (("gateway", 8710), ("asr", 8711), ("tts", 8712), ("llm", 8713))}
VENVS = {"gateway": "services/gateway/.venv", "asr": "workers/asr/.venv", "tts": "workers/tts/.venv"}
PIDFILES = ROOT / "var" / "run"


# ---------------------------------------------------------------- models


def lock_models() -> list[dict]:
    return [m for m in json.loads(LOCK.read_text())["models"] if m.get("files") and m.get("local_dir")]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def check_models(full: bool, hash_files: bool = True) -> list[tuple[str, str, str]]:
    """Rows (name, status, detail). status: ok | missing | size | hash | optional-missing.

    Hashes are cached in var/cache/model_hashes.json keyed by size and mtime; `full` re-hashes everything."""
    cache = {} if full or not HASH_CACHE.exists() else json.loads(HASH_CACHE.read_text())
    rows = []
    for model in lock_models():
        for f in model["files"]:
            rel = f"{model['local_dir']}/{f['path']}"
            path = ROOT / rel
            required = f.get("required", True)
            if not path.is_file():
                rows.append((rel, "missing" if required else "optional-missing", "not downloaded"))
                continue
            st = path.stat()
            if st.st_size != f["bytes"]:
                rows.append((rel, "size", f"{st.st_size} bytes, expected {f['bytes']}"))
                continue
            if not hash_files:
                rows.append((rel, "ok", "size ok (hash not checked)"))
                continue
            key = f"{st.st_size}:{st.st_mtime_ns}"
            cached = cache.get(rel)
            digest = cached["sha256"] if cached and cached.get("key") == key else sha256(path)
            cache[rel] = {"key": key, "sha256": digest}
            rows.append((rel, "ok" if digest == f["sha256"] else "hash", "sha256 ok" if digest == f["sha256"] else "sha256 mismatch"))
    if hash_files:
        HASH_CACHE.parent.mkdir(parents=True, exist_ok=True)
        HASH_CACHE.write_text(json.dumps(cache, indent=1))
    return rows


def cmd_verify(args: list[str]) -> int:
    rows = check_models(full="--full" in args)
    bad = [r for r in rows if r[1] not in ("ok", "optional-missing")]
    for name, status, detail in rows:
        if status != "ok" or "--verbose" in args:
            print(f"  {status:<16} {name}  ({detail})")
    print(f"models: {len(rows) - len(bad)}/{len(rows)} files ok" + (f", {len(bad)} problem(s)" if bad else ""))
    return 1 if bad else 0


def cmd_download(args: list[str]) -> int:
    from huggingface_hub import hf_hub_download  # available in workers/asr/.venv

    todo = {(r[0]) for r in check_models(full=False) if r[1] in ("missing", "size", "hash")}
    for model in lock_models():
        src = model["source"]
        for f in model["files"]:
            rel = f"{model['local_dir']}/{f['path']}"
            if rel not in todo:
                continue
            print(f"  downloading {src['repo']}@{src['revision'][:10]} {f['path']} ({f['bytes'] / 1e6:.0f} MB)", flush=True)
            (ROOT / rel).unlink(missing_ok=True)
            hf_hub_download(repo_id=src["repo"], filename=f["path"], revision=src["revision"],
                            local_dir=str(ROOT / model["local_dir"]))
    return cmd_verify(["--full"])


# ---------------------------------------------------------------- doctor


def run(cmd: list[str], cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (out.stdout + out.stderr).strip()


def port_owner(port: int) -> str | None:
    """None if free, else 'pid command' of the listener (best effort)."""
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return None
    out = run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"])
    pid = next((line[1:] for line in out.splitlines() if line.startswith("p")), "?")
    name = next((line[1:] for line in out.splitlines() if line.startswith("c")), "?")
    return f"pid {pid} ({name})"


def our_pids() -> dict[str, int]:
    pids = {}
    for f in PIDFILES.glob("*.pid") if PIDFILES.exists() else []:
        try:
            pid = int(f.read_text().split()[0])
            os.kill(pid, 0)
            pids[f.stem] = pid
        except (ValueError, IndexError, ProcessLookupError, PermissionError):
            continue
    return pids


def venv_python(name: str) -> Path:
    return ROOT / VENVS[name] / "bin" / "python"


def pkg_versions(name: str, pkgs: list[str]) -> str:
    py = venv_python(name)
    if not py.exists():
        return ""
    code = ("import importlib.metadata as m, sys\nout=[sys.version.split()[0]]\n"
            f"for p in {pkgs!r}:\n    try: out.append(p+' '+m.version(p))\n    except Exception: out.append(p+' -')\n"
            "print(', '.join(out))")
    return run([str(py), "-c", code])


def rows_doctor(full: bool) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []

    def add(check: str, ok: bool | None, detail: str) -> None:
        rows.append((check, "OK" if ok else ("WARN" if ok is None else "FAIL"), detail))

    # Hardware
    chip = run(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor()
    mem = int(run(["sysctl", "-n", "hw.memsize"]) or 0) / 2**30
    disk = shutil.disk_usage(ROOT).free / 2**30
    add("hardware", platform.machine() == "arm64" and platform.system() == "Darwin" and mem >= 16,
        f"{platform.system()} {platform.mac_ver()[0]} {platform.machine()}, {chip}, {mem:.0f} GB RAM "
        "(validated profile: Apple M3 Pro 36 GB)")
    add("disk free", disk >= 5 or None, f"{disk:.0f} GB free")
    gpu = run(["llama-server", "--list-devices"])
    add("metal device", "MTL" in gpu or None, next((line.strip() for line in gpu.splitlines() if "MTL" in line), "no Metal device listed"))

    # Runtimes
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    add("uv", Path(uv).exists(), run([uv, "--version"]) or "not found (https://docs.astral.sh/uv/)")
    add("python (launcher)", sys.version_info >= (3, 10), sys.version.split()[0])
    for name, pkgs in (("gateway", ["onnxruntime", "fastapi", "vr-feedback"]),
                       ("asr", ["torch", "transformers", "qwen-asr"]),
                       ("tts", ["torch", "torchaudio", "transformers", "onnxruntime", "mlx", "mlx-audio-plus"])):
        v = pkg_versions(name, pkgs)
        add(f"env {name}", bool(v) and not any(part.endswith(" -") for part in v.split(", ")[1:]),
            v or f"missing {VENVS[name]} (run ./app setup)")
    node = run(["node", "--version"])
    add("node", node.startswith("v") or None, node or "not on PATH (only needed for setup: npm ci + build)")
    llama = run(["llama-server", "--version"])
    ver = next((line for line in llama.splitlines() if line.startswith("version")), "")
    add("llama-server", bool(ver), ver or "not found (brew install llama.cpp)")
    server_json = json.loads((ROOT / "config/llm/server.json").read_text())
    add("llama-server flags", all(f in server_json["args"] for f in ("--no-kv-unified", "--cache-prompt"))
        and server_json.get("env", {}).get("LLAMA_API_KEY") == "${VR_WORKER_TOKEN}",
        "config/llm/server.json: -np 2 --no-kv-unified --cache-prompt, LLAMA_API_KEY=worker token")

    # Vendor + web build
    vendor = ROOT / "vendor/CosyVoice"
    want = json.loads(LOCK.read_text())["vendor"]["CosyVoice"]["commit"]
    have = run(["git", "-C", str(vendor), "rev-parse", "HEAD"]) if (vendor / ".git").exists() else ""
    add("vendor/CosyVoice", have == want, f"{have[:10] or 'missing'} (pinned {want[:10]})")
    add("web build", (ROOT / "apps/web/dist/index.html").exists(), "apps/web/dist" + ("" if (ROOT / "apps/web/dist/index.html").exists() else " missing"))

    # Models
    mrows = check_models(full=full)
    bad = [r for r in mrows if r[1] not in ("ok", "optional-missing")]
    optional = [r for r in mrows if r[1] == "optional-missing"]
    for model in lock_models():
        mine = [r for r in mrows if r[0].startswith(model["local_dir"] + "/")]
        problems = [f"{Path(r[0]).name}: {r[1]}" for r in mine if r[1] not in ("ok", "optional-missing")]
        absent = sum(r[1] == "optional-missing" for r in mine)
        detail = "; ".join(problems) if problems else f"{len(mine) - absent} files, sha256 ok"
        if absent and not problems:
            detail = "optional, not downloaded" if absent == len(mine) else f"{detail}; {absent} optional file(s) not downloaded"
        add(f"model {model['id']}", not problems, f"{model['source']['repo']}@{model['source']['revision'][:10]}: {detail}")
    placeholders = [m["id"] for m in json.loads(LOCK.read_text())["models"] if not m.get("files")]
    if placeholders:
        add("lock placeholders", None, f"not pinned yet: {', '.join(placeholders)}")

    # Voices
    voices = sorted(p.name for p in (ROOT / "content/voices").iterdir() if (p / "prompt.wav").exists()) \
        if (ROOT / "content/voices").exists() else []
    dev = [v for v in voices if "개발용" in (ROOT / "content/voices" / v / "SOURCE.md").read_text(errors="ignore")]
    add("voices", bool(voices), f"{', '.join(voices) or 'none'}" + (f" — development voices, replace before release: {', '.join(dev)}" if dev else ""))
    if dev:
        rows[-1] = (rows[-1][0], "WARN", rows[-1][2])

    # Ports
    running = our_pids()
    for name, port in PORTS.items():
        owner = port_owner(port)
        if owner is None:
            add(f"port {port} ({name})", True, "free")
        else:
            pid = owner.split()[1]
            ours = pid in {str(p) for p in running.values()}
            add(f"port {port} ({name})", True if ours else False, owner + (" — this app" if ours else " — used by another process"))

    # Offline readiness: everything needed at runtime is local, and runtime never downloads.
    offline = not bad and bool(have == want) and (ROOT / "apps/web/dist/index.html").exists() and bool(voices) \
        and all(venv_python(n).exists() for n in VENVS)
    add("offline readiness", offline,
        "all runtime files local; workers start with HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1"
        + (f"; optional files not downloaded: {len(optional)}" if optional else ""))
    return rows


def cmd_doctor(args: list[str]) -> int:
    rows = rows_doctor(full="--full" in args)
    w = max(len(r[0]) for r in rows)
    print(f"{'CHECK'.ljust(w)}  STATUS  DETAIL")
    print(f"{'-' * w}  ------  {'-' * 40}")
    for check, status, detail in rows:
        print(f"{check.ljust(w)}  {status:<6}  {detail}")
    fails = [r for r in rows if r[1] == "FAIL"]
    print(f"\n{len(fails)} failed, {sum(r[1] == 'WARN' for r in rows)} warning(s)")
    return 1 if fails else 0


def cmd_preflight(args: list[str]) -> int:
    problems = []
    for name in VENVS:
        if not venv_python(name).exists():
            problems.append(f"{VENVS[name]} missing")
    if not (ROOT / "vendor/CosyVoice/.git").exists():
        problems.append("vendor/CosyVoice missing")
    if not shutil.which("llama-server"):
        problems.append("llama-server not on PATH (brew install llama.cpp)")
    if not (ROOT / "apps/web/dist/index.html").exists():
        problems.append("web build missing (apps/web/dist)")
    for rel, status, detail in check_models(full=False, hash_files=False):
        if status in ("missing", "size", "hash"):
            problems.append(f"model file {rel}: {detail}")
    for name, port in PORTS.items():
        owner = port_owner(port)
        if owner:
            problems.append(f"port {port} ({name}) is in use by {owner}")
    for p in problems:
        print(f"  - {p}")
    return 1 if problems else 0


# ---------------------------------------------------------------- start


def cmd_wait_ready(args: list[str]) -> int:
    pid, timeout = int(args[0]), float(args[1]) if len(args) > 1 else 900.0
    url = f"http://127.0.0.1:{os.environ.get('VR_GATEWAY_PORT', PORTS['gateway'])}/api/health"
    deadline, last = time.monotonic() + timeout, ""
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print("  gateway exited during start-up (see var/log/gateway.log)")
            return 1
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                health = json.load(resp)
        except (OSError, ValueError):
            health = None
        if health:
            workers, cache = health["workers"], health.get("tts_cache") or {}
            state = " ".join(f"{k}={'ready' if workers[k]['ready'] else 'loading'}" for k in ("asr", "tts", "llm", "vad"))
            total = cache.get("openings_total") or 0
            finished = (cache.get("openings_ready") or 0) + (cache.get("openings_failed") or 0)
            if workers["tts"]["ready"]:
                state += f", opening lines cached {cache.get('openings_ready', 0)}/{total or '?'}"
            if state != last:
                print(f"  {state}", flush=True)
                last = state
            # Warm-up (PRD §14.3): models loaded and every scenario's opening line pre-synthesized.
            if health["modes"]["realtime"]["available"] and (cache.get("status") == "done" or (total and finished >= total)):
                if cache.get("openings_failed"):
                    print(f"  warning: {cache['openings_failed']} opening line(s) failed to synthesize (see var/log/gateway.log)")
                return 0
            dead = [n for n, p in our_worker_pids().items() if not alive(p)]
            if dead:
                print(f"  worker(s) exited: {', '.join(dead)} (see var/log/<name>.log)")
                return 1
        time.sleep(1)
    print(f"  not ready after {timeout:.0f} s")
    return 1


def our_worker_pids() -> dict[str, int]:
    out = {}
    for name in ("asr", "tts", "llm"):
        f = PIDFILES / f"{name}.pid"
        if f.exists():
            try:
                out[name] = int(f.read_text().split()[0])
            except (ValueError, IndexError):
                continue
    return out


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie child counts as gone.
    return "Z" not in run(["ps", "-o", "stat=", "-p", str(pid)])


COMMANDS = {"verify": cmd_verify, "download": cmd_download, "doctor": cmd_doctor, "preflight": cmd_preflight,
            "wait-ready": cmd_wait_ready}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    sys.exit(COMMANDS[sys.argv[1]](sys.argv[2:]))
