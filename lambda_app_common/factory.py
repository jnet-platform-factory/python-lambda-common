"""Resolve a service's factory from a name, by dotted path.

Services keep a `FACTORIES_MAP = {"Documents": "src.factories.DocumentsFactory", ...}` and
build their factory at module import, so a missing or broken entry fails the Lambda at cold
start -- loudly, on the first invocation after a deploy -- rather than on some request later.
That timing is deliberate and callers should keep it: call this at module level.
"""

from importlib import import_module
from typing import Any, Mapping


def build_service(service: str, factories_map: Mapping[str, str], **kwargs: Any):
    """Import `factories_map[service]` and return an instance built with the service name."""
    if service not in factories_map:
        raise ValueError(f"Unknown service: {service}")

    module_name, class_name = factories_map[service].rsplit(".", 1)
    factory_class = getattr(import_module(module_name), class_name)
    return factory_class(service, **kwargs)
