"""Run the gateway: `python -m vr_gateway [--manage-workers] [--config PATH]`."""

from __future__ import annotations

import argparse
import logging
import secrets
from pathlib import Path

import uvicorn

from .app import create_app
from .config import load_config
from .supervisor import Supervisor


def main() -> None:
    parser = argparse.ArgumentParser(prog="vr_gateway")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--manage-workers", action="store_true",
                        help="start ASR/TTS/LLM with a fresh worker token and stop them on exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs full request URLs (query strings carry the ASR scenario context); keep them out.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    config = load_config(args.config)
    supervisor = None
    if args.manage_workers:
        config.worker_token = secrets.token_urlsafe(32)
        supervisor = Supervisor(config)
    app = create_app(config, supervisor=supervisor)
    # Access logs would include URLs with ids; the app logs route templates itself.
    uvicorn.run(app, host=config.host, port=config.port, access_log=False, ws_max_size=128 * 1024,
                log_level="info", proxy_headers=False, server_header=False, timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
