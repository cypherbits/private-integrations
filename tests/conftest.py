"""Shared pytest fixtures for the Private Integrations test suite."""
import warnings

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load the integration under test."""
    yield


@pytest.fixture(autouse=True, scope="session")
def _start_the_pycares_thread():
    """Start pycares' process-wide thread before PHACC snapshots threads.

    Creating a resolver starts ``_run_safe_shutdown_loop``, which then lives
    for the whole process. The Home Assistant 2024.11 test harness
    (pytest-homeassistant-custom-component 0.13.183) reports it as a leaked
    thread; newer versions exempt it. Starting it once, before any test, keeps
    PHACC's task, timer and instance cleanup checks enabled.
    """
    try:
        import aiodns
    except ImportError:  # pragma: no cover - Home Assistant always ships aiodns
        yield
        return

    with warnings.catch_warnings():
        # aiodns 3.x asks for the current event loop, and a sync fixture has none.
        warnings.simplefilter("ignore", DeprecationWarning)
        _resolver = aiodns.DNSResolver()
    yield
