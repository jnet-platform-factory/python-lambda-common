"""The two version strings Create Release bumps must agree."""

import re
from pathlib import Path

import lambda_app_common

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_pyproject_and_package_carry_the_same_version():
    declared = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT.read_text(), re.M).group(1)
    assert lambda_app_common.__version__ == declared


def test_the_version_is_plain_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", lambda_app_common.__version__)
