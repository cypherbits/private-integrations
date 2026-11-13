"""Shared pytest fixtures for the Private Integrations test suite."""
import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load the integration under test."""
    yield


@pytest.fixture(autouse=True)
def verify_cleanup():
    """Disable PHACC's thread-leak check.

    The aiohttp client session used by the integration creates a pycares
    resolver thread (``_run_safe_shutdown_loop``) that outlives the test and
    is not a leak of the code under test.
    """
    yield
