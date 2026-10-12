"""The service rules and the review logic must stay light: importing them may not pull in torch, chromadb or the
embedder (the review notebook runs in a bare notebook environment)."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEAVY = ["torch", "chromadb", "sentence_transformers", "transformers", "langchain_anthropic", "langchain_google_vertexai"]
MODULES = [
    "aml_copilot.service.worker", "aml_copilot.service.auth", "aml_copilot.service.delta",
    "aml_copilot.service.pipeline", "aml_copilot.service.audit", "aml_copilot.service.jobs",
    "aml_copilot.service.models", "aml_copilot.dbx.review", "aml_copilot.dbx.runtime",
]


def test_importing_the_service_modules_does_not_load_the_heavy_libraries():
    imports = "".join(f"import {m}\n" for m in MODULES)
    code = f"import sys\n{imports}loaded = [m for m in {HEAVY!r} if m in sys.modules]\nprint(','.join(loaded))\n"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT)
    assert result.stdout.strip() == "", f"these heavy libraries were imported: {result.stdout.strip()}"
