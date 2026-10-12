"""Usage: python -m aml_copilot.agents ACC-S-00001"""

import sys

from aml_copilot.agents.evidence import investigate
from aml_copilot.logging_config import configure_logging_from_settings

if __name__ == "__main__":
    configure_logging_from_settings()
    if len(sys.argv) != 2:
        sys.exit("usage: python -m aml_copilot.agents <account_id>")
    result = investigate(sys.argv[1])
    if result.get("error"):
        sys.exit(f"error: {result['error']}")
    print(result["summary"].model_dump_json(indent=2))
