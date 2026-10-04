from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from .config import get_config


class FeatureFlagsMap(BaseModel):
    """
    Holds evaluated feature flags for the current user.

    Supports attribute access (flags.my_flag), dict-style get, and to_dict().
    Extra fields are allowed so any flag name can be stored without schema changes.
    """

    model_config = ConfigDict(extra="allow")

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def to_dict(self) -> dict:
        return dict(self)


class OrganizationUserContext(BaseModel):
    organization: Optional[str] = Field(None, description="Organization the user belongs to")
    user_id: Optional[str] = Field(None, description="Unique identifier for the user (Cognito sub)")
    username: Optional[str] = Field(None, description="Cognito username")
    user_email: Optional[str] = Field(None, description="User email address")
    user_groups: List[str] = Field(default_factory=list, description="Groups the user belongs to")
    branches: List[str] = Field(default_factory=list, description="Branches accessible to the user")
    user_applications: List[str] = Field(default_factory=list, description="Applications the user has access to")
    user_env: str = Field(default="dev", description="User environment (e.g., dev, prod)")
    application: Optional[str] = Field(None, description="Application context")
    environment: Optional[str] = Field(None, description="Environment context")

    # Optional or derived attributes
    user_customer: Optional[str] = None
    user_seller: Optional[str] = None
    is_platform_admin: Optional[bool] = None
    is_organization_admin: Optional[bool] = None
    is_organization_member: Optional[bool] = None
    is_organization_seller: Optional[bool] = None
    is_organization_customer: Optional[bool] = None
    seller_id: Optional[str] = None
    customer_id: Optional[str] = None

    # Feature flags evaluated for this user's session/role/org
    feature_flags: Optional[FeatureFlagsMap] = Field(default=None, description="Feature flags relevant to this user")

    # --- role aliases (config.role_aliases) -------------------------------------------------
    # A service whose code, flag rules or API consumers still use an older role name configures
    # e.g. `role_aliases={"is_admin": "is_platform_admin"}`; the alias then behaves as the field.

    @model_validator(mode="before")
    @classmethod
    def _accept_aliases(cls, data):
        aliases = get_config().role_aliases
        if isinstance(data, dict) and aliases:
            data = dict(data)
            for alias, canonical in aliases.items():
                if alias in data:
                    value = data.pop(alias)
                    data.setdefault(canonical, value)
        return data

    def __getattr__(self, name):
        canonical = get_config().role_aliases.get(name)
        if canonical is not None:
            return getattr(self, canonical)
        return super().__getattr__(name)

    def __setattr__(self, name, value):
        super().__setattr__(get_config().role_aliases.get(name, name), value)

    @model_serializer(mode="wrap")
    def _serialise_with_aliases(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            for alias, canonical in get_config().role_aliases.items():
                if canonical in data:
                    data[alias] = data[canonical]
        return data

    @property
    def feature_flags_dict(self) -> dict:
        return self.feature_flags.to_dict() if self.feature_flags else {}

    def derive_roles(self):
        """Populate the role booleans from user_groups, using the configured group names."""
        groups = set(self.user_groups or [])
        for attribute, group in get_config().role_groups.items():
            if attribute in type(self).model_fields:
                setattr(self, attribute, group in groups)
        return self

    def with_feature_flags(self, flags) -> "OrganizationUserContext":
        """Attach evaluated feature flags and return self for chaining."""
        if isinstance(flags, dict):
            flags = FeatureFlagsMap(**flags)
        self.feature_flags = flags
        return self

    @property
    def flags(self) -> SimpleNamespace:
        """Object-style access to feature flags, e.g. ``ctx.flags.my_feature``."""
        data = self.feature_flags.to_dict() if self.feature_flags else {}
        return SimpleNamespace(**data)

    def to_dict(self):
        """Convert to a dictionary, excluding None values."""
        return {k: v for k, v in self.model_dump().items() if v is not None}

    def as_service_context(self) -> Dict[str, Any]:
        """The user as the plain dict services have always been handed as `service.context`.

        Services written against the old handler read these exact keys
        (`service.context['is_platform_admin']`, `['organization']`, ...), so the shape is a
        contract: keys are always present, with None where nothing is known, and the role
        booleans are never None. Configured role aliases are included beside their field.
        """
        groups = list(self.user_groups or [])
        role_groups = get_config().role_groups
        context = {
            "organization": self.organization,
            "username": self.username,
            "user_email": self.user_email,
            "user_groups": groups,
            "user_env": self.environment or self.user_env,
            "user_applications": list(self.user_applications or []),
            "user_customer": self.user_customer,
            "user_seller": self.user_seller,
            **{attribute: group in groups for attribute, group in role_groups.items()},
            "seller_id": self.seller_id,
            "customer_id": self.customer_id,
        }
        for alias, canonical in get_config().role_aliases.items():
            if canonical in context:
                context[alias] = context[canonical]
        return context
