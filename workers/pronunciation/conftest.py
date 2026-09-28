import os

# Tests must never reach the network for model files (PROTOCOL §1).
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"


def pytest_terminal_summary(terminalreporter):
    import json

    from tests.test_integration import METRICS

    if METRICS:
        terminalreporter.section("Pronunciation worker measurements")
        terminalreporter.write_line(json.dumps(METRICS, indent=2))
