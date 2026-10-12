"""The API role must start fast and small: importing it may not pull in torch, chromadb or the embedder."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEAVY = ["torch", "chromadb", "sentence_transformers", "transformers", "langchain_anthropic", "langchain_google_vertexai"]


def test_importing_the_service_modules_does_not_load_the_heavy_libraries():
    code = (
        "import sys\n"
        "import aml_copilot.service.api, aml_copilot.service.worker, aml_copilot.service.auth\n"
        "import aml_copilot.service.audit, aml_copilot.service.jobs, aml_copilot.service.models\n"
        f"loaded = [m for m in {HEAVY!r} if m in sys.modules]\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT)
    assert result.stdout.strip() == "", f"these heavy libraries were imported: {result.stdout.strip()}"
