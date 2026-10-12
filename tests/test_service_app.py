"""Choosing the right app from settings, including the refusals that keep a misconfigured deploy from starting."""

import time

import pytest
from fastapi.testclient import TestClient

import aml_copilot.service.app as app_module
from aml_copilot.config import Settings
from aml_copilot.errors import ConfigError
from aml_copilot.service.auth import AuthError


def settings(**overrides) -> Settings:
    base = {"_env_file": None, "service_backend": "memory", "service_role": "api", "auth_mode": "dev"}
    return Settings(**{**base, **overrides})


def test_the_default_local_app_serves_health_and_demands_a_token():
    client = TestClient(app_module.build_app(settings()))
    assert client.get("/status").json()["role"] == "api"
    assert client.get("/investigations").status_code == 401
    assert client.get("/investigations", headers={"Authorization": "Bearer dev-investigator"}).status_code == 200


def test_a_local_job_is_run_in_the_background_by_the_injected_pipeline(monkeypatch):
    def fake_run_fn(_settings):
        return lambda account_id: {"account_id": account_id, "status": "no_action", "status_detail": "Nothing to see."}

    monkeypatch.setattr(app_module, "make_run_fn", fake_run_fn)
    client = TestClient(app_module.build_app(settings()))
    headers = {"Authorization": "Bearer dev-investigator"}
    job_id = client.post("/investigations", json={"account_id": "ACC-S-00001"}, headers=headers).json()["job_id"]
    for _ in range(50):
        body = client.get(f"/investigations/{job_id}", headers=headers).json()
        if body["status"] == "COMPLETED":
            break
        time.sleep(0.05)
    assert body["status"] == "COMPLETED" and body["result"]["status"] == "no_action"
    assert body["result"]["provenance"]["llm_provider"] == "anthropic"


def test_the_worker_cannot_run_on_the_in_memory_backend():
    with pytest.raises(ConfigError, match="worker"):
        app_module.build_app(settings(service_role="worker"))


def test_unknown_role_or_backend_is_refused():
    with pytest.raises(ConfigError, match="SERVICE_ROLE"):
        app_module.build_app(settings(service_role="both"))
    with pytest.raises(ConfigError, match="SERVICE_BACKEND"):
        app_module.build_app(settings(service_backend="sqlite"))


def test_dev_auth_cannot_start_on_cloud_run(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "api")
    with pytest.raises(AuthError, match="Cloud Run"):
        app_module.build_app(settings())


def test_the_gcp_backend_needs_its_bucket_named():
    with pytest.raises(ConfigError, match="AUDIT_BUCKET"):
        app_module.build_app(settings(service_backend="gcp"))


def test_provenance_names_the_model_for_each_step():
    p = app_module.make_provenance(settings(anthropic_model="claude-haiku-4-5", narrative_model="claude-sonnet-5",
                                            llm_provider="vertex", build_sha="abc123"))
    assert p.models == {"evidence": "claude-haiku-4-5", "typology": "claude-haiku-4-5",
                        "narrative": "claude-sonnet-5", "faithfulness": "claude-haiku-4-5"}
    assert p.llm_provider == "vertex" and p.build == {"sha": "abc123"}


def test_routes_move_under_the_prefix_when_one_is_set():
    client = TestClient(app_module.build_app(settings(api_prefix="/api")))
    headers = {"Authorization": "Bearer dev-investigator"}
    assert client.get("/api/investigations", headers=headers).status_code == 200
    assert client.get("/investigations", headers=headers).status_code == 404
    assert client.get("/status").status_code == 200
