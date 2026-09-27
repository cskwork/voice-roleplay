import contextlib
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_DIR = Path(__file__).resolve().parents[3]
AUDIO_DIR = REPO_DIR / "tests" / "fixtures" / "audio"
TOKEN = "test-token-123"


@contextlib.contextmanager
def serve(app):
    """Run an ASGI app with uvicorn (wsproto, like production) on a free loopback port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, ws="wsproto", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def audio_dir():
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return AUDIO_DIR
