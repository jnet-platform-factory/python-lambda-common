import sys
import types

import pytest

from lambda_app_common.config import configure, get_config, run_invocation_start_hooks
from lambda_app_common.factory import build_service


class ExampleFactory:
    def __init__(self, service, **kwargs):
        self.service = service
        self.kwargs = kwargs

    def build(self):
        return f"built {self.service}"


@pytest.fixture
def factories_module(monkeypatch):
    module = types.ModuleType("example_factories")
    module.ExampleFactory = ExampleFactory
    monkeypatch.setitem(sys.modules, "example_factories", module)
    return {"Example": "example_factories.ExampleFactory", "Broken": "example_factories.Missing"}


def test_build_service_imports_the_mapped_factory_with_the_service_name(factories_module):
    factory = build_service("Example", factories_module)
    assert isinstance(factory, ExampleFactory)
    assert factory.service == "Example"
    assert factory.build() == "built Example"


def test_an_unknown_service_fails_with_the_same_message_as_before(factories_module):
    with pytest.raises(ValueError, match="Unknown service: Nope"):
        build_service("Nope", factories_module)


def test_a_broken_entry_fails_loudly(factories_module):
    with pytest.raises(AttributeError):
        build_service("Broken", factories_module)


def test_configure_rejects_unknown_names():
    with pytest.raises(TypeError, match="Unknown configuration: stagee"):
        configure(stagee="dev")


def test_settings_fall_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("STAGE", "from-env")
    monkeypatch.setenv("APPLICATION", "App")
    assert get_config().resolved_stage() == "from-env"
    configure(stage="explicit")
    assert get_config().resolved_stage() == "explicit"
    assert get_config().resolved_application() == "App"


def test_start_hooks_run_in_order_and_may_fail_the_invocation():
    seen = []
    configure(on_invocation_start=[lambda e: seen.append(("a", e)), lambda e: seen.append(("b", e))])
    run_invocation_start_hooks({"x": 1})
    assert seen == [("a", {"x": 1}), ("b", {"x": 1})]

    def boom(_):
        raise RuntimeError("reset failed")

    configure(on_invocation_start=[boom])
    with pytest.raises(RuntimeError):
        run_invocation_start_hooks({})
