"""Databricks job task: investigate one account. All logic lives in aml_copilot.dbx.jobs (tested on a laptop)."""

import sys

from aml_copilot.dbx.jobs import investigate_main

code = investigate_main(sys.argv[1:])
if code:  # a clean sys.exit(0) is reported as a failure by Databricks, so only exit on a real error
    sys.exit(code)
