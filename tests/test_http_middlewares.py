import json
from unittest.mock import MagicMock

import pytest
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.event_handler.exceptions import NotFoundError

from lambda_app_common.config import configure
from lambda_app_common.http import (
    api_error_handler_for,
    body_data,
    get_cors_config,
    inject_organization_user_context,
    inject_services,
    normalize_to_list,
    print_request_info,
    response_data,
)
from lambda_app_common.http.middlewares import api_error_handler

from .helpers import SECRET, body_of, hs256, lambda_context, proxy_event, rs256_shaped

COGNITO_CLAIMS = {
    "sub": "u-1",
    "cognito:username": "jane",
    "custom:organization": "ACME",
    "email": "jane@example.com",
    "cognito:groups": "Member,OrganizationAdmin",
    "custom:applications": "udm",
    "custom:customer": "C-9",
}


class Service:
    def __init__(self):
        self.context = None
        self.event_bus = MagicMock()
        self.logger = MagicMock()
        self.metrics = MagicMock()


def build_app(service, identity=inject_organization_user_context, route=None):
    app = APIGatewayRestResolver(cors=get_cors_config(allow_origin="https://portal.example.com"))
    app.use(middlewares=[
        print_request_info,
        api_error_handler_for("things.api"),
        identity,
        inject_services({"things_service": service}, logger=service.logger, metrics=service.metrics),
        response_data,
    ])

    @app.get("/things")
    def get_things():
        if route:
            return route(app)
        ctx = app.context["organization_user_context"]
        return {"organization": ctx.organization, "service_context": app.context["services"]["things_service"].context}

    return app


@pytest.fixture(autouse=True)
def config():
    configure(stage="dev", application="App", jwt_secret=SECRET, cors_origins=["https://a.example.com",
                                                                                 "https://b.example.com"])


def test_cognito_claims_become_the_user_and_the_service_context():
    service = Service()
    response = build_app(service).resolve(proxy_event(claims=COGNITO_CLAIMS), lambda_context())

    assert response["statusCode"] == 200
    ctx = service.context
    assert ctx["organization"] == "ACME"
    assert ctx["username"] == "jane"
    assert ctx["user_email"] == "jane@example.com"
    assert ctx["user_groups"] == ["Member", "OrganizationAdmin"]
    assert ctx["is_organization_admin"] is True
    assert ctx["is_platform_admin"] is False
    assert ctx["user_env"] == "dev"
    assert ctx["user_customer"] == "C-9" and ctx["customer_id"] == "C-9"
    # the legacy dict shape: every key present
    assert set(ctx) == {"organization", "username", "user_email", "user_groups", "user_env",
                        "user_applications", "user_customer", "user_seller", "is_platform_admin",
                        "is_organization_admin", "is_organization_member", "is_organization_seller",
                        "is_organization_customer", "seller_id", "customer_id"}


def test_logger_keys_and_the_api_request_metric_are_recorded():
    service = Service()
    build_app(service).resolve(proxy_event(claims=COGNITO_CLAIMS, query={"q": "1"}), lambda_context())

    keys = service.logger.append_keys.call_args.kwargs
    assert keys["organization"] == "ACME"
    assert keys["method"] == "GET" and keys["resource"] == "/things"
    assert keys["query_params"] == {"q": "1"}
    assert service.metrics.add_metric.call_args.kwargs["name"] == "api_request"
    dims = {c.kwargs["name"]: c.kwargs["value"] for c in service.metrics.add_dimension.call_args_list}
    assert dims == {"env": "dev", "organization": "ACME", "read_only": "True"}


def test_a_service_token_is_verified_and_read():
    service = Service()
    token = hs256({"organization": "ACME", "username": "bot", "cognito:groups": ["Member"]})
    response = build_app(service).resolve(proxy_event(headers={"Authorization": f"Bearer {token}"}),
                                          lambda_context())
    assert response["statusCode"] == 200
    assert service.context["username"] == "bot"
    assert service.context["user_groups"] == ["Member"]


def test_a_forged_service_token_is_a_401():
    token = hs256({"organization": "ACME", "username": "bot"}, secret="wrong")
    response = build_app(Service()).resolve(proxy_event(headers={"Authorization": f"Bearer {token}"}),
                                            lambda_context())
    assert response["statusCode"] == 401


def test_an_unsigned_rs256_token_is_a_401():
    service = Service()
    token = rs256_shaped(COGNITO_CLAIMS)
    response = build_app(service).resolve(proxy_event(headers={"authorization": f"Bearer {token}"}),
                                          lambda_context())
    assert response["statusCode"] == 401
    assert service.context is None


def test_no_credentials_is_a_401_and_the_route_never_runs():
    route = MagicMock()
    response = build_app(Service(), route=route).resolve(proxy_event(), lambda_context())
    assert response["statusCode"] == 401
    route.assert_not_called()


def test_a_deliberate_service_error_keeps_its_status():
    def route(app):
        raise NotFoundError("no such thing")

    response = build_app(Service(), route=route).resolve(proxy_event(claims=COGNITO_CLAIMS), lambda_context())
    assert response["statusCode"] == 404


def test_bad_input_is_a_400_and_is_published_under_the_named_source():
    service = Service()

    def route(app):
        raise ValueError("bad id")

    response = build_app(service, route=route).resolve(proxy_event(claims=COGNITO_CLAIMS), lambda_context())
    assert response["statusCode"] == 400
    published = service.event_bus.publish.call_args.kwargs
    assert published["source"] == "things.api"
    assert published["detail_type"] == "BadRequestError"
    assert published["payload"]["location"] == "route"
    assert published["payload"]["error"] == "ValueError: bad id"


def test_anything_else_is_a_500():
    def route(app):
        raise RuntimeError("db down")

    response = build_app(Service(), route=route).resolve(proxy_event(claims=COGNITO_CLAIMS), lambda_context())
    assert response["statusCode"] == 500


def test_the_bare_error_handler_uses_the_default_source():
    service = Service()
    app = APIGatewayRestResolver()
    app.use(middlewares=[api_error_handler, inject_services({"s": service})])

    @app.get("/boom")
    def boom():
        raise KeyError("k")

    assert app.resolve(proxy_event(path="/boom"), lambda_context())["statusCode"] == 400
    assert service.event_bus.publish.call_args.kwargs["source"] == "workflow.api_error"


def test_start_hooks_run_exactly_once_per_request():
    seen = []
    configure(on_invocation_start=[seen.append])
    event = proxy_event(claims=COGNITO_CLAIMS)
    build_app(Service()).resolve(event, lambda_context())
    assert len(seen) == 1
    assert seen[0]["httpMethod"] == "GET"


def test_feature_flags_come_from_the_configured_evaluator():
    calls = []

    def evaluator(context):
        calls.append(context)
        return {"new_export": True}

    configure(feature_flag_evaluator=evaluator)

    def route(app):
        return {"flag": app.context["organization_user_context"].flags.new_export}

    response = build_app(Service(), route=route).resolve(proxy_event(claims=COGNITO_CLAIMS), lambda_context())
    assert body_of(response) == {"flag": True}
    assert calls[0]["organization"] == "ACME"


def test_a_failing_flag_evaluator_does_not_fail_the_request():
    def evaluator(context):
        raise RuntimeError("appconfig down")

    configure(feature_flag_evaluator=evaluator)
    assert build_app(Service()).resolve(proxy_event(claims=COGNITO_CLAIMS), lambda_context())["statusCode"] == 200


def test_credentials_never_reach_the_logs(capsys):
    token = hs256({"organization": "ACME", "username": "bot"})
    build_app(Service()).resolve(proxy_event(headers={"Authorization": f"Bearer {token}"}), lambda_context())
    assert token not in capsys.readouterr().out


def test_credentials_never_reach_the_logs_under_debug(capsys, monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    token = hs256({"organization": "ACME", "username": "bot"})
    build_app(Service()).resolve(proxy_event(headers={"Authorization": f"Bearer {token}"}, body={"a": 1}),
                                 lambda_context())
    out = capsys.readouterr().out
    assert token not in out
    assert "Bearer sha256:" in out


def test_cors_origins_come_from_configuration():
    cors = get_cors_config()
    assert cors._allowed_origins == ["https://a.example.com", "https://b.example.com"]
    assert cors.allow_credentials is True and cors.max_age == 86400
    configure(cors_origins=lambda stage: [f"https://{stage}.example.com"])
    assert get_cors_config()._allowed_origins == ["https://dev.example.com"]


def test_cors_without_origins_refuses_to_guess():
    configure(cors_origins=None)
    with pytest.raises(ValueError):
        get_cors_config()


def test_body_helpers_keep_their_shapes():
    assert json.loads(body_data([1])) == {"data": [1]}


@pytest.mark.parametrize("raw, expected", [
    (None, []),
    ("", []),
    ("a,b", ["a", "b"]),
    ("[a b]", ["a", "b"]),
    ("Main Branch,Second", ["Main Branch", "Second"]),
    (["a", "", "b"], ["a", "b"]),
])
def test_list_claims_are_normalised(raw, expected):
    assert normalize_to_list(raw) == expected
