"""Process-wide configuration for the handler chain.

A Lambda module imports the chain at cold start and the chain reads its settings on
every invocation, so configuration is a module-level singleton, set once from the
service's own wiring module before any handler module is imported:

    configure(
        stage=env_vars.STAGE,
        application=env_vars.APPLICATION,
        jwt_secret=env_vars.JWT_SECRET,
        cors_origins=origins_for_stage,
        feature_flag_evaluator=flags.evaluate_all_for_user,
        on_invocation_start=[EventBridge.set_incoming_trail],
    )

Anything left unset falls back to the environment variable of the same name.
"""

import os
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Union

## The groups that grant each derived role on `OrganizationUserContext`. A service whose user
## pool names them differently passes its own `role_groups`, e.g.
## `{**DEFAULT_ROLE_GROUPS, "is_platform_admin": "AcmeAdmin"}`. An unconfigured platform-admin
## group simply never matches, so a missed setting fails closed.
DEFAULT_ROLE_GROUPS: Mapping[str, str] = {
    "is_platform_admin": "PlatformAdmin",
    "is_organization_admin": "OrganizationAdmin",
    "is_organization_member": "Member",
    "is_organization_seller": "Seller",
    "is_organization_customer": "Customer",
}

OriginsSource = Union[Sequence[str], Callable[[Optional[str]], Sequence[str]], None]


@dataclass
class PlatformConfig:
    stage: Optional[str] = None
    application: Optional[str] = None
    jwt_secret: Optional[str] = None
    ## A list of allowed origins, or a callable taking the stage and returning one. The
    ## first origin becomes CORSConfig.allow_origin and the rest extra_origins.
    cors_origins: OriginsSource = None
    ## `evaluator(context: dict) -> dict` returning the evaluated flags for one user.
    feature_flag_evaluator: Optional[Callable[[dict], Any]] = None
    ## Called with the raw event at the start of every invocation, whatever the trigger,
    ## before any handler code runs. Per-invocation state that a warm container would
    ## otherwise carry over (an EventBridge loop-detection trail, say) is reset here.
    on_invocation_start: Sequence[Callable[[Any], None]] = field(default_factory=tuple)
    role_groups: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_ROLE_GROUPS))
    ## Older names for the role fields, `{"old_name": "canonical_name"}`, for a service whose code,
    ## feature-flag rules or API consumers still use them. An alias reads and sets the canonical
    ## field on `OrganizationUserContext`, and is emitted beside it in `as_service_context()`,
    ## the feature-flag evaluation context and the model's serialised form.
    role_aliases: Mapping[str, str] = field(default_factory=dict)

    def resolved_stage(self) -> Optional[str]:
        return self.stage or os.environ.get("STAGE")

    def resolved_application(self) -> Optional[str]:
        return self.application or os.environ.get("APPLICATION")

    def resolved_jwt_secret(self) -> Optional[str]:
        return self.jwt_secret or os.environ.get("JWT_SECRET")


_config = PlatformConfig()
_FIELD_NAMES = {f.name for f in fields(PlatformConfig)}


def configure(**settings) -> PlatformConfig:
    """Set any of :class:`PlatformConfig`'s fields. An unknown name is an error, not a no-op."""
    unknown = set(settings) - _FIELD_NAMES
    if unknown:
        raise TypeError(f"Unknown configuration: {', '.join(sorted(unknown))}")
    for name, value in settings.items():
        if name == "on_invocation_start":
            value = tuple(value or ())
        setattr(_config, name, value)
    return _config


def get_config() -> PlatformConfig:
    return _config


def reset_config() -> None:
    """Restore the defaults. For tests."""
    global _config
    _config = PlatformConfig()


def run_invocation_start_hooks(event: Any, hooks: Optional[Iterable[Callable[[Any], None]]] = None) -> None:
    """Run the start-of-invocation hooks against the raw event.

    A hook that raises fails the invocation. These exist to reset state that would
    otherwise silently corrupt it, and carrying on with that state is the worse outcome.
    """
    for hook in (hooks if hooks is not None else get_config().on_invocation_start):
        hook(event)
