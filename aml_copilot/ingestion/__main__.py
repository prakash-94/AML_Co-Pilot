"""Usage: python -m aml_copilot.ingestion ACC-S-00001"""

import sys

from aml_copilot.ingestion import ingest
from aml_copilot.logging_config import configure_logging_from_settings

if __name__ == "__main__":
    configure_logging_from_settings()
    if len(sys.argv) != 2:
        sys.exit("usage: python -m aml_copilot.ingestion <account_id>")
    print(ingest(sys.argv[1]).model_dump_json(indent=2))
