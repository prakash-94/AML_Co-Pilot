"""Start and watch the investigate job through the Databricks Jobs API (what the web page uses instead of a queue)."""

from collections.abc import Callable
from typing import Any

import httpx

from aml_copilot.errors import SourceUnavailableError


class JobsApiLauncher:
    def __init__(self, host: str, token: Callable[[], str], job_id: int, *, http: Any = httpx, timeout: float = 30.0):
        self._base = f"https://{host.removeprefix('https://').rstrip('/')}"
        self._token = token
        self._job_id = int(job_id)
        self._http = http
        self._timeout = timeout

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._http.request(
                method, self._base + path, headers={"Authorization": f"Bearer {self._token()}"},
                timeout=self._timeout, **kwargs,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise SourceUnavailableError("databricks jobs", f"{type(exc).__name__}") from exc

    def start(self, account_id: str, requested_by: str, provider: str = "gemini") -> dict[str, Any]:
        params = {"account_id": account_id, "requested_by": requested_by, "provider": provider}
        body = {"job_id": self._job_id, "job_parameters": params}
        return {"run_id": self._call("POST", "/api/2.1/jobs/run-now", json=body)["run_id"]}

    def status(self, run_id: int) -> dict[str, Any]:
        run = self._call("GET", "/api/2.1/jobs/runs/get", params={"run_id": run_id})
        state = run.get("state", {})
        return {"state": state.get("life_cycle_state"), "result": state.get("result_state"),
                "message": state.get("state_message") or ""}
