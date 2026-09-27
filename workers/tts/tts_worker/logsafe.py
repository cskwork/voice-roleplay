"""Logging setup that keeps synthesized text out of every log (PROTOCOL §2).

CosyVoice logs the text it synthesizes through the root logger (cosyvoice/cli/cosyvoice.py,
``logging.info('synthesis text {}')``) and calls ``logging.basicConfig(level=DEBUG)`` on import.
We drop every record that originates in the vendored tree and log only our own event records.
"""

import logging
import sys
from pathlib import Path

VENDOR_DIR = str(Path(__file__).resolve().parents[3] / "vendor")


class DropVendorRecords(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.pathname.startswith(VENDOR_DIR)


def configure_logging(level: int = logging.INFO) -> None:
    """(Re)install the only root handler. Call again after importing CosyVoice."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(DropVendorRecords())
    root.addHandler(handler)
    if not any(isinstance(f, DropVendorRecords) for f in root.filters):
        root.addFilter(DropVendorRecords())  # records logged directly on the root logger
    root.setLevel(logging.WARNING)
    logging.getLogger("vr").setLevel(level)
    logging.getLogger("uvicorn").setLevel(level)  # access log lines carry only method/path/status
