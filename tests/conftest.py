import os

import pytest

os.environ.setdefault("POWERTOOLS_METRICS_NAMESPACE", "Test")
os.environ.setdefault("POWERTOOLS_SERVICE_NAME", "test")

from lambda_app_common.config import reset_config  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_config():
    reset_config()
    yield
    reset_config()
