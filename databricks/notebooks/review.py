# Databricks notebook source
# MAGIC %md
# MAGIC # AML Copilot - review an investigation
# MAGIC Pick an **action**, optionally an **investigation id**, write a **comment**, then run all cells.
# MAGIC The notebook runs as *you*: your real Databricks login is recorded as the reviewer, and your role comes from
# MAGIC the `reviewers` table. Only a supervisor can **approve**; everyone listed can reject or escalate.
# MAGIC Nothing here files a report with a regulator - an approval means "approved as a draft for manual filing".

# COMMAND ----------

# MAGIC %pip install -q "pydantic==2.9.2" "pydantic-settings==2.6.1" structlog httpx tenacity python-dotenv

# COMMAND ----------

import sys
from datetime import UTC, datetime

# The bundle syncs the code next to this notebook: <workspace folder>/files/{aml_copilot, databricks/notebooks, ...}
here = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
root = "/Workspace" + here.split("/databricks/notebooks/")[0]
sys.path.insert(0, root)

from aml_copilot.dbx.review import ACTIONS, run_action
from aml_copilot.dbx.runtime import DEFAULT_SCHEMA, spark_services

dbutils.widgets.dropdown("action", "list", list(ACTIONS), "Action")
dbutils.widgets.text("investigation_id", "", "Investigation id")
dbutils.widgets.text("comment", "", "Comment (required to decide)")

# COMMAND ----------

store, audit, roles = spark_services(spark, DEFAULT_SCHEMA)
user = spark.sql("SELECT current_user() AS u").collect()[0]["u"]

try:
    text = run_action(
        dbutils.widgets.get("action"), dbutils.widgets.get("investigation_id"), dbutils.widgets.get("comment"),
        user, datetime.now(UTC), store=store, audit=audit, roles=roles,
    )
except Exception as error:  # show the reason in plain words rather than a stack trace
    text = f"NOT DONE: {error}"
print(text)

# COMMAND ----------

# Shows the same text as the result of a scheduled / API run, so the outcome can be read without opening the notebook.
dbutils.notebook.exit(text[:4000])
