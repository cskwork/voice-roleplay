import os

import uvicorn

from .app import create_app
from .logsafe import configure_logging


def main() -> None:
    configure_logging()
    port = int(os.environ.get("VR_TTS_PORT", "8712"))
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_config=None, ws="wsproto")


if __name__ == "__main__":
    main()
