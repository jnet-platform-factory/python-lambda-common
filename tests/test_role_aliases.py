"""`role_aliases`: an older role name behaves as the field it was renamed to.

Services keep code, feature-flag rules and API consumers that read the old name. With
`role_aliases={"is_admin": "is_platform_admin"}` none of them has to change at once.
"""

import pytest

from lambda_app_common.config import DEFAULT_ROLE_GROUPS, configure
from lambda_app_common.http import inject_organization_user_context
from lambda_app_common.models import OrganizationUserContext

from .helpers import lambda_context, proxy_event
from .test_http_middlewares import Service, build_app


@pytest.fixture
def aliased():
    configure(stage="dev", cors_origins=["https://a.example.com"],
              role_groups={**DEFAULT_ROLE_GROUPS, "is_platform_admin": "AcmeAdmin"},
              role_aliases={"is_admin": "is_platform_admin"})


def admin():
    return OrganizationUserContext(organization="ACME", username="ana", user_groups=["AcmeAdmin"]).derive_roles()


def test_the_default_platform_admin_group_is_neutral_and_fails_closed():
    assert DEFAULT_ROLE_GROUPS["is_platform_admin"] == "PlatformAdmin"
    ctx = OrganizationUserContext(user_groups=["AcmeAdmin"]).derive_roles()
    assert ctx.is_platform_admin is False


def test_the_group_name_is_configurable(aliased):
    assert admin().is_platform_admin is True


def test_an_alias_reads_and_writes_the_field(aliased):
    ctx = admin()
    assert ctx.is_admin is True
    ctx.is_admin = False
    assert ctx.is_platform_admin is False


def test_an_alias_is_accepted_on_construction(aliased):
    assert OrganizationUserContext(is_admin=True).is_platform_admin is True
    assert OrganizationUserContext.model_validate({"is_admin": True}).is_platform_admin is True


def test_an_alias_is_serialised_beside_the_field(aliased):
    dumped = admin().model_dump()
    assert dumped["is_platform_admin"] is True and dumped["is_admin"] is True
    assert admin().to_dict()["is_admin"] is True


def test_an_alias_is_in_the_service_context(aliased):
    ctx = admin().as_service_context()
    assert ctx["is_platform_admin"] is True and ctx["is_admin"] is True


def test_without_aliases_nothing_extra_appears():
    ctx = OrganizationUserContext(user_groups=["PlatformAdmin"]).derive_roles()
    assert "is_admin" not in ctx.model_dump()
    assert "is_admin" not in ctx.as_service_context()
    with pytest.raises(AttributeError):
        ctx.is_admin


def test_feature_flag_rules_see_the_alias(aliased):
    seen = []
    configure(feature_flag_evaluator=lambda context: seen.append(context) or {})
    claims = {"sub": "u", "cognito:username": "ana", "custom:organization": "ACME", "cognito:groups": "AcmeAdmin"}

    build_app(Service(), identity=inject_organization_user_context).resolve(proxy_event(claims=claims),
                                                                            lambda_context())

    assert seen[0]["is_platform_admin"] is True and seen[0]["is_admin"] is True
