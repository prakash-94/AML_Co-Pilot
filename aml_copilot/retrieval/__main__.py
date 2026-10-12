"""Usage:
    python -m aml_copilot.retrieval build
    python -m aml_copilot.retrieval query "some evidence text" [top_k]
"""

import sys

from aml_copilot.logging_config import configure_logging_from_settings
from aml_copilot.retrieval.store import build_index, search

if __name__ == "__main__":
    configure_logging_from_settings()
    if len(sys.argv) < 2 or sys.argv[1] not in ("build", "query"):
        sys.exit("usage: python -m aml_copilot.retrieval build | query <text> [top_k]")

    if sys.argv[1] == "build":
        count = build_index()
        print(f"Indexed {count} chunks.")
    else:
        if len(sys.argv) < 3:
            sys.exit("usage: python -m aml_copilot.retrieval query <text> [top_k]")
        top_k = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        for r in search(sys.argv[2], top_k=top_k):
            where = f"p.{r.chunk.page}" if r.chunk.page else (r.chunk.heading or "")
            print(f"[{r.distance:.3f}] {r.chunk.source} {where}")
            print("   ", r.chunk.text[:200].replace("\n", " "))
