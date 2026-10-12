"""Databricks job task: the scheduled quality check. All logic lives in aml_copilot.dbx.jobs."""

import sys

from aml_copilot.dbx.jobs import canary_main

code = canary_main(sys.argv[1:])
if code:  # a clean sys.exit(0) is reported as a failure by Databricks, so only exit on a real error
    sys.exit(code)
