"""Entry point of the Databricks App (the platform runs `python main.py` and routes browser traffic to this port)."""

import os
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))  # the build step copies the aml_copilot package next to this file

from aml_copilot.dbx.appmain import build_from_environment

app = build_from_environment(os.environ)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("DATABRICKS_APP_PORT", "8000")))
