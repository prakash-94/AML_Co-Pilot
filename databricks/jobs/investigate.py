"""Databricks job task: investigate one account. All logic lives in aml_copilot.dbx.jobs (tested on a laptop)."""

import sys

from aml_copilot.dbx.jobs import investigate_main

sys.exit(investigate_main(sys.argv[1:]))
