"""Bearer-token verification in inject_organization_user_context.

RS256 tokens are signed with a throwaway RSA key whose public half is served as the user
pool's JWKS through a patched `_fetch_jwks`, so nothing here touches the network.
"""
import base64
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import jwt
import pytest
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from cryptography.hazmat.primitives.asymmetric import rsa

from lambda_app_common.config import configure
from lambda_app_common.http import api_error_handler_for, groups_and_applications, inject_organization_user_context
from lambda_app_common.http import middlewares

from .helpers import SECRET, lambda_context, proxy_event

POOL_ID = "us-east-1_TestPool"
ISSUER = f"https://cognito-idp.us-east-1.amazonaws.com/{POOL_ID}"
KID = "test-kid"

SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(private_key, kid=KID):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    return {"keys": [{**jwk, "kid": kid, "alg": "RS256", "use": "sig"}]}


def _cognito_payload(**overrides):
    now = int(time.time())
    payload = {
        "sub": "u-1",
        "iss": ISSUER,
        "token_use": "id",
        "aud": "some-app-client",
        "cognito:username": "jane",
        "cognito:groups": ["OrganizationAdmin"],
        "custom:organization": "ACME",
        "custom:applications": "udm",
        "email": "jane@example.com",
        "iat": now,
        "exp": now + 600,
    }
    payload.update(overrides)
    return payload


def _rs256(payload, key=SIGNING_KEY, kid=KID):
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


def _unsigned(header, payload):
    def b64(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{b64(header)}.{b64(payload)}."


def _resolve(headers=None, claims=None):
    """Run the identity middleware behind the error handler; return (status, context)."""
    seen = {}
    app = APIGatewayRestResolver()
    app.use(middlewares=[api_error_handler_for("things.api"), inject_organization_user_context])

    @app.get("/things")
    def get_things():
        seen["ctx"] = app.context["organization_user_context"]
        return {}

    response = app.resolve(proxy_event(headers=headers, claims=claims), lambda_context())
    return response["statusCode"], seen.get("ctx")


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    configure(stage="dev", application="App", jwt_secret=SECRET, cognito_user_pool_id=POOL_ID)
    fetch = MagicMock(return_value=_jwks(SIGNING_KEY))
    monkeypatch.setattr(middlewares, "_fetch_jwks", fetch)
    monkeypatch.setattr(middlewares, "_jwks_cache", {})
    return fetch


# --- RS256 ------------------------------------------------------------------------------------

def test_a_valid_cognito_token_is_verified_and_read():
    status, ctx = _resolve(_bearer(_rs256(_cognito_payload())))
    assert status == 200
    assert (ctx.organization, ctx.username, ctx.user_groups, ctx.user_applications) == \
        ("ACME", "jane", ["OrganizationAdmin"], ["udm"])


def test_an_access_token_reads_the_username_claim():
    payload = _cognito_payload(token_use="access", username="machine-client")
    del payload["cognito:username"]
    status, ctx = _resolve(_bearer(_rs256(payload)))
    assert status == 200 and ctx.username == "machine-client"


@pytest.mark.parametrize("token", [
    pytest.param(_rs256(_cognito_payload(), key=OTHER_KEY), id="signed-by-another-key"),
    pytest.param(_rs256(_cognito_payload(), kid="not-in-the-jwks"), id="unknown-kid"),
    pytest.param(_rs256(_cognito_payload(iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Other")),
                 id="wrong-issuer"),
    pytest.param(_rs256(_cognito_payload(token_use="refresh")), id="wrong-token-use"),
    pytest.param(_rs256({k: v for k, v in _cognito_payload().items() if k != "token_use"}), id="no-token-use"),
    pytest.param(_rs256(_cognito_payload(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600)),
                 id="expired"),
])
def test_an_rs256_token_that_fails_verification_is_a_401(token):
    status, ctx = _resolve(_bearer(token))
    assert status == 401 and ctx is None


def test_a_tampered_payload_is_a_401():
    header, _, signature = _rs256(_cognito_payload()).split(".")
    payload = _unsigned({}, _cognito_payload(**{"custom:organization": "Other"})).split(".")[1]
    assert _resolve(_bearer(f"{header}.{payload}.{signature}"))[0] == 401


def test_without_a_configured_pool_rs256_is_refused(setup):
    configure(cognito_user_pool_id=None)
    assert _resolve(_bearer(_rs256(_cognito_payload())))[0] == 401
    setup.assert_not_called()


def test_the_jwks_is_fetched_once_per_container(setup):
    for _ in range(3):
        assert _resolve(_bearer(_rs256(_cognito_payload())))[0] == 200
    setup.assert_called_once_with(f"{ISSUER}/.well-known/jwks.json")


def test_unknown_kids_do_not_refetch_inside_the_window(setup):
    _resolve(_bearer(_rs256(_cognito_payload())))
    for _ in range(3):
        assert _resolve(_bearer(_rs256(_cognito_payload(), kid="made-up")))[0] == 401
    assert setup.call_count == 1


def test_a_jwks_fetch_failure_is_a_503(setup):
    setup.side_effect = OSError("network down")
    assert _resolve(_bearer(_rs256(_cognito_payload())))[0] == 503


def test_a_jwks_with_no_usable_keys_is_a_401(setup):
    setup.return_value = {"keys": []}
    assert _resolve(_bearer(_rs256(_cognito_payload())))[0] == 401


# --- Algorithm selection ----------------------------------------------------------------------

@pytest.mark.parametrize("header", [{"typ": "JWT"}, {"alg": "none", "typ": "JWT"}, {"alg": "ES256", "typ": "JWT"}],
                         ids=["no-alg", "alg-none", "unsupported-alg"])
def test_a_missing_or_unsupported_alg_is_a_401(header):
    assert _resolve(_bearer(_unsigned(header, {"organization": "ACME", "username": "x"})))[0] == 401


def test_an_rs256_body_relabelled_hs256_is_a_401():
    _, payload, signature = _rs256(_cognito_payload()).split(".")
    header = _unsigned({"alg": "HS256", "typ": "JWT"}, {}).split(".")[0]
    assert _resolve(_bearer(f"{header}.{payload}.{signature}"))[0] == 401


def test_a_garbage_bearer_is_a_401():
    assert _resolve(_bearer("garbage"))[0] == 401


# --- HS256 ------------------------------------------------------------------------------------

def _minted_token(groups, applications, expires_at=None):
    """The shape an admin portal mints: `groups` / `applications`, an `exp`."""
    exp = expires_at or (datetime.now(timezone.utc) + timedelta(days=365))
    payload = {"organization": "ACME", "username": "integration", "groups": groups,
               "applications": applications, "exp": exp}
    return jwt.encode(payload, SECRET, algorithm="HS256")


def test_a_minted_token_keeps_its_groups(setup):
    status, ctx = _resolve(_bearer(_minted_token(["OrganizationAdmin"], ["Organizer"])))
    assert status == 200
    assert ctx.user_groups == ["OrganizationAdmin"] and ctx.user_applications == ["Organizer"]
    assert ctx.is_organization_admin is True
    setup.assert_not_called()


def test_an_expired_hs256_token_is_a_401():
    token = _minted_token(["Member"], [], expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    assert _resolve(_bearer(token))[0] == 401


# --- Authorizer claims ------------------------------------------------------------------------

def test_authorizer_claims_are_trusted_without_a_network_call(setup):
    setup.side_effect = AssertionError("must not fetch the JWKS for authorizer claims")
    claims = {"sub": "u-1", "cognito:username": "jane", "custom:organization": "ACME",
              "cognito:groups": "OrganizationAdmin,Member"}
    status, ctx = _resolve(claims=claims)
    assert status == 200 and ctx.user_groups == ["OrganizationAdmin", "Member"]
    setup.assert_not_called()


# --- Claim shapes -----------------------------------------------------------------------------

@pytest.mark.parametrize("claims, expected", [
    ({"groups": ["OrganizationAdmin"], "applications": ["Organizer"]}, (["OrganizationAdmin"], ["Organizer"])),
    ({"cognito:groups": "Member,Seller", "custom:applications": "udm"}, (["Member", "Seller"], ["udm"])),
    ({"user_groups": ["Member"], "user_applications": ["app"]}, (["Member"], ["app"])),
    ({"groups": ["OrganizationAdmin"], "cognito:groups": ["Member"]}, (["OrganizationAdmin"], [])),
    ({}, ([], [])),
])
def test_groups_and_applications_reads_every_shape(claims, expected):
    assert groups_and_applications(claims) == expected
