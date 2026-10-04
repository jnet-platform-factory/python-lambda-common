"""The legacy identity rules, pinned case by case against what the retired handler did."""

import pytest
from aws_lambda_powertools.event_handler.exceptions import UnauthorizedError

from lambda_app_common.config import configure
from lambda_app_common.http import legacy_user_context, resolve_legacy_user_context

from .helpers import SECRET, hs256, lambda_context, proxy_event
from .test_http_middlewares import Service, build_app

BEARER_IDENTITY = {"organization": "ACME", "username": "platform"}


@pytest.fixture(autouse=True)
def config():
    configure(stage="dev", application="App", jwt_secret=SECRET)


def test_a_webhook_token_reads_user_groups_and_defaults_to_member():
    token = hs256({"organization": "ACME", "username": "hook", "email": "h@example.com"})
    user, auth = resolve_legacy_user_context(proxy_event(headers={"X-Webhook-Token": token}))
    assert (user.organization, user.username, user.user_email) == ("ACME", "hook", "h@example.com")
    assert user.user_groups == ["Member"]
    assert user.is_organization_member is True
    assert auth.mode == "webhook"


def test_a_webhook_token_with_groups_keeps_them():
    token = hs256({"organization": "ACME", "username": "hook", "user_groups": ["PlatformAdmin"],
                   "user_applications": ["udm"]})
    user, _ = resolve_legacy_user_context(proxy_event(headers={"x-webhook-token": token}))
    assert user.is_platform_admin is True
    assert user.user_applications == ["udm"]


def test_an_expired_or_forged_webhook_token_is_a_401():
    with pytest.raises(UnauthorizedError, match="expired"):
        resolve_legacy_user_context(proxy_event(headers={"X-Webhook-Token": hs256({}, expires_in=-10)}))
    with pytest.raises(UnauthorizedError, match="Invalid"):
        resolve_legacy_user_context(proxy_event(headers={"X-Webhook-Token": hs256({}, secret="nope")}))


def test_an_unverified_bearer_gets_the_configured_identity():
    user, auth = resolve_legacy_user_context(proxy_event(headers={"Authorization": "Bearer opaque"}),
                                             BEARER_IDENTITY)
    assert (user.organization, user.username, user.user_email) == ("ACME", "platform", "")
    assert user.user_groups == ["Member"]
    assert auth.auth_token == "opaque"
    assert auth.mode == "bearer"


def test_an_unverified_bearer_is_refused_when_no_identity_is_configured():
    with pytest.raises(UnauthorizedError):
        resolve_legacy_user_context(proxy_event(headers={"Authorization": "Bearer opaque"}))


def test_no_credentials_at_all_is_a_401():
    with pytest.raises(UnauthorizedError):
        resolve_legacy_user_context(proxy_event(), BEARER_IDENTITY)


def test_cognito_claims_follow_the_old_keys():
    claims = {"cognito:username": "jane", "cognito:email": "j@example.com", "cognito:groups": "[Member Seller]",
              "custom:seller": "S-1"}
    user, auth = resolve_legacy_user_context(proxy_event(claims=claims))
    assert user.organization == "unknown"
    assert user.user_email == "j@example.com"
    assert user.user_groups == ["Member", "Seller"]
    assert user.is_organization_seller is True
    assert user.seller_id == "S-1" and user.user_seller == "S-1"
    assert auth.mode == "cognito"


def test_a_client_credentials_context_is_a_401_instead_of_a_crash():
    with pytest.raises(UnauthorizedError):
        resolve_legacy_user_context(proxy_event(authorizer={"principalId": "client"}))


def test_the_middleware_slots_into_the_standard_chain():
    service = Service()
    app = build_app(service, identity=legacy_user_context(bearer_identity=BEARER_IDENTITY))
    response = app.resolve(proxy_event(headers={"Authorization": "Bearer opaque"}), lambda_context())
    assert response["statusCode"] == 200
    assert service.context["organization"] == "ACME"
    assert service.context["username"] == "platform"
    assert service.context["user_env"] == "dev"


def test_the_middleware_exposes_the_raw_token_for_endpoints_that_validate_it():
    seen = {}

    def route(app):
        seen["token"] = app.context["legacy_auth"].auth_token
        return {}

    app = build_app(Service(), identity=legacy_user_context(bearer_identity=BEARER_IDENTITY), route=route)
    app.resolve(proxy_event(headers={"Authorization": "Bearer opaque"}), lambda_context())
    assert seen["token"] == "opaque"
