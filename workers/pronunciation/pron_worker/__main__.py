import os
import sys

# Offline before any Hugging Face import (PROTOCOL §1).
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

from .config import ConfigError  # noqa: E402

try:
    from .app import main

    main()
except ConfigError as exc:
    print(f"pron-worker: {exc}", file=sys.stderr)
    sys.exit(2)
