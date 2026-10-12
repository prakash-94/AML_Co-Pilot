"""Auth: bearer parsing, role claims, the dev-auth guard, and role checks."""

import pytest

from aml_copilot.service.auth import (
    AuthError,
    FirebaseTokenVerifier,
    ForbiddenError,
    Principal,
    StaticTokenVerifier,
    bearer_token,
    build_verifier,
    principal_from_claims,
    require_role,
)


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer   ", "Basic abc", "abc"])
def test_a_missing_or_malformed_header_is_refused(header):
    with pytest.raises(AuthError):
        bearer_token(header)


def test_the_token_is_taken_from_a_bearer_header_case_insensitively():
    assert bearer_token("Bearer abc.def") == "abc.def"
    assert bearer_token("bearer  abc.def ") == "abc.def"


def test_claims_become_a_principal_using_the_role_claim():
    p = principal_from_claims({"user_id": "u1", "email": "a@b.c", "role": "supervisor"})
    assert p == Principal("u1", "a@b.c", "supervisor")


def test_the_subject_is_used_when_there_is_no_user_id_claim():
    assert principal_from_claims({"sub": "u2", "role": "investigator"}).uid == "u2"


@pytest.mark.parametrize("claims", [
    {"user_id": "u1"},  # no role at all
    {"user_id": "u1", "role": "admin"},  # not a role we know
    {"user_id": "u1", "role": ["supervisor"]},
    {"role": "supervisor"},  # no user id
    {"user_id": "", "role": "supervisor"},
])
def test_a_token_without_a_valid_user_and_role_is_refused(claims):
    with pytest.raises(AuthError):
        principal_from_claims(claims)


def test_the_static_verifier_knows_only_its_own_tokens():
    verifier = StaticTokenVerifier({"t": Principal("u", "e", "investigator")})
    assert verifier.verify("t").uid == "u"
    with pytest.raises(AuthError):
        verifier.verify("other")


def test_dev_auth_is_allowed_locally():
    verifier = build_verifier("dev", environ={})
    assert verifier.verify("dev-supervisor").role == "supervisor"


def test_dev_auth_is_refused_on_cloud_run():
    with pytest.raises(AuthError, match="Cloud Run"):
        build_verifier("dev", environ={"K_SERVICE": "api"})


def test_an_unknown_mode_is_refused():
    with pytest.raises(AuthError):
        build_verifier("none", environ={})


def test_the_firebase_verifier_needs_a_project_id():
    with pytest.raises(AuthError):
        FirebaseTokenVerifier("")
    with pytest.raises(AuthError):
        build_verifier("firebase", firebase_project_id="", environ={})


def test_the_firebase_verifier_rejects_garbage_without_leaking_details(monkeypatch):
    pytest.importorskip("google.oauth2")
    with pytest.raises(AuthError, match="Invalid token"):
        FirebaseTokenVerifier("proj").verify("not-a-jwt")


def test_require_role():
    investigator = Principal("u", "e", "investigator")
    require_role(investigator, "investigator", "supervisor")
    with pytest.raises(ForbiddenError):
        require_role(investigator, "supervisor")
