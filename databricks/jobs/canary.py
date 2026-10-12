"""Databricks job task: the scheduled quality check. All logic lives in aml_copilot.dbx.jobs."""

import sys

from aml_copilot.dbx.jobs import canary_main

sys.exit(canary_main(sys.argv[1:]))
