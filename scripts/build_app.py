"""Copy the aml_copilot package next to the web app's main.py so the Databricks App can import it.

    python scripts/build_app.py

A Databricks App is deployed from one folder (`databricks/app`) and installs only that folder's requirements.txt, so the
shared code is copied in (and git-ignored) rather than duplicated in the repository. The bundle runs this before every
deploy. Python caches and tests never travel.
"""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "aml_copilot"
TARGET = ROOT / "databricks" / "app" / "aml_copilot"


def main() -> None:
    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.copytree(SOURCE, TARGET, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"copied {sum(1 for _ in TARGET.rglob('*.py'))} modules to {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
