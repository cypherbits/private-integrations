"""Tests for integration setup, reconfiguration and removal."""
from pathlib import Path

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.private_integrations import persistent_notification
from custom_components.private_integrations.const import CONF_GITHUB_TOKEN, DOMAIN
from custom_components.private_integrations.repository import Repository
from custom_components.private_integrations.store import RepositoryStore

TOKEN = "ghp_test_token"
USER_URL = "https://api.github.com/user"
REPO_URL = "https://api.github.com/repos/owner/foo"
SHA = "a" * 40


def make_tarball(domain="foo", version="1.0.0"):
    """Build a minimal integration tarball."""
    import io
    import json
    import tarfile

    members = [
        ("top", None),
        (
            f"top/custom_components/{domain}/manifest.json",
            json.dumps({"domain": domain, "version": version}).encode(),
        ),
    ]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in members:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.fixture
def isolated_config(hass, tmp_path):
    """Keep everything this integration writes inside the test's tmp_path."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


@pytest.fixture
def at_startup(hass):
    """Set the real state Home Assistant has while it sets up config entries."""
    hass.set_state(CoreState.starting)
    yield
    hass.set_state(CoreState.running)


@pytest.fixture
def while_running(hass):
    """Set the state Home Assistant has when an entry is reloaded."""
    hass.set_state(CoreState.running)
    yield


async def _store_with_repo(hass, **kwargs):
    data = {
        "full_name": "owner/foo",
        "ref_type": "branch",
        "ref": "main",
        "integration_name": "foo",
        "installed_version": "main",
        "installed_sha": SHA,
    }
    data.update(kwargs)
    store = RepositoryStore(hass)
    store.upsert(Repository(**data))
    await store.async_save()
    return store


def _entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_GITHUB_TOKEN: TOKEN}, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)
    return entry


def _record_dismissals(monkeypatch) -> list[str]:
    dismissed: list[str] = []
    monkeypatch.setattr(
        persistent_notification,
        "async_dismiss",
        lambda _hass, notification_id: dismissed.append(notification_id),
    )
    return dismissed


async def test_setup_survives_a_network_error(hass, aioclient_mock, isolated_config):
    """A reachability problem must not fail the entry."""
    aioclient_mock.get(USER_URL, exc=aiohttp.ClientError("boom"))
    entry = _entry(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN][entry.entry_id]["manager"] is not None


async def test_setup_restores_an_interrupted_install(
    hass, aioclient_mock, isolated_config, at_startup
):
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})
    await _store_with_repo(hass)
    custom_components = Path(hass.config.path("custom_components"))
    backup = custom_components / ".foo.private_integrations-backup"
    backup.mkdir(parents=True)
    (backup / "manifest.json").write_text('{"domain": "foo"}')

    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert (custom_components / "foo" / "manifest.json").is_file()
    assert not backup.exists()


async def test_setup_removes_stale_staging_folders(
    hass, aioclient_mock, isolated_config, at_startup
):
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})
    custom_components = Path(hass.config.path("custom_components"))
    staging = custom_components / ".private_integrations-staging-abcd"
    staging.mkdir(parents=True)
    (staging / "leftover.txt").write_text("junk")

    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert not staging.exists()


async def test_setup_dismisses_the_pending_restart_reminder(
    hass, aioclient_mock, isolated_config, at_startup, monkeypatch
):
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})
    await _store_with_repo(hass)
    dismissed = _record_dismissals(monkeypatch)

    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert dismissed == ["private_integrations_pending_restart_foo"]


async def test_a_reload_keeps_the_pending_restart_reminder(
    hass, aioclient_mock, isolated_config, while_running, monkeypatch
):
    """Reloading the entry does not mean the installed integration is loaded."""
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})
    await _store_with_repo(hass)
    custom_components = Path(hass.config.path("custom_components"))
    staging = custom_components / ".private_integrations-staging-abcd"
    staging.mkdir(parents=True)
    dismissed = _record_dismissals(monkeypatch)

    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert dismissed == []
    assert staging.is_dir()


async def test_removing_the_entry_drops_the_storage(
    hass, aioclient_mock, isolated_config, hass_storage, at_startup
):
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})
    await _store_with_repo(hass)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.repositories" in hass_storage

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert f"{DOMAIN}.repositories" not in hass_storage


@pytest.mark.parametrize("exc", [aiohttp.ClientError, TimeoutError])
async def test_network_errors_become_github_errors(hass, aioclient_mock, exc):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.private_integrations.github import GitHubClient, GitHubError

    aioclient_mock.get(USER_URL, exc=exc("boom"))
    client = GitHubClient(async_get_clientsession(hass), TOKEN)

    with pytest.raises(GitHubError, match="Could not reach GitHub"):
        await client.verify_token()


async def test_recovery_only_runs_at_startup(
    hass, aioclient_mock, isolated_config, monkeypatch
):
    """A reload must not touch staging folders: an install may be writing them."""
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})
    await _store_with_repo(hass)
    custom_components = Path(hass.config.path("custom_components"))
    staging = custom_components / ".private_integrations-staging-abcd"
    staging.mkdir(parents=True)
    dismissed = _record_dismissals(monkeypatch)

    hass.set_state(CoreState.running)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert staging.is_dir()
    assert dismissed == []


async def test_setup_fails_with_an_auth_error(hass, aioclient_mock, isolated_config):
    from homeassistant.exceptions import ConfigEntryAuthFailed

    from custom_components.private_integrations import async_setup_entry

    aioclient_mock.get(USER_URL, status=401)
    entry = _entry(hass)

    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)


async def test_setup_retries_on_a_rate_limit(hass, aioclient_mock, isolated_config):
    from homeassistant.exceptions import ConfigEntryNotReady

    from custom_components.private_integrations import async_setup_entry

    aioclient_mock.get(USER_URL, status=429)
    entry = _entry(hass)

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)


async def test_a_broken_recovery_does_not_break_setup(
    hass, aioclient_mock, isolated_config, at_startup, monkeypatch
):
    from custom_components import private_integrations

    def failing_recovery(path):
        raise OSError("boom")

    monkeypatch.setattr(
        private_integrations, "recover_stale_installs", failing_recovery
    )
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})
    entry = _entry(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED


async def test_services_are_registered_once_and_removed_with_the_entry(
    hass, aioclient_mock, isolated_config
):
    from custom_components.private_integrations.const import (
        SERVICE_ADD_REPOSITORY,
        SERVICE_INSTALL,
        SERVICE_REMOVE_REPOSITORY,
        SERVICE_UPDATE,
    )

    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    services = (
        SERVICE_ADD_REPOSITORY,
        SERVICE_REMOVE_REPOSITORY,
        SERVICE_INSTALL,
        SERVICE_UPDATE,
    )
    assert all(hass.services.has_service(DOMAIN, name) for name in services)

    # A second setup of the same entry must not register them twice.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not any(hass.services.has_service(DOMAIN, name) for name in services)


async def test_register_services_is_idempotent(hass, aioclient_mock, isolated_config):
    from custom_components.private_integrations import _register_services

    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Registering again must be a no-op, not a duplicate registration.
    _register_services(hass)

    assert hass.services.has_service(DOMAIN, "add_repository")


async def test_the_add_and_install_services_work(hass, aioclient_mock, isolated_config):
    from custom_components.private_integrations.const import (
        SERVICE_ADD_REPOSITORY,
        SERVICE_INSTALL,
    )

    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    store = hass.data[DOMAIN][entry.entry_id]["store"]

    aioclient_mock.get(REPO_URL, json={"default_branch": "main"})
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_REPOSITORY,
        {"full_name": "owner/foo", "ref_type": "branch", "ref": ""},
        blocking=True,
    )
    assert store.get("owner/foo").ref == "main"

    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())
    await hass.services.async_call(
        DOMAIN,
        SERVICE_INSTALL,
        {"full_name": "owner/foo", "force": True},
        blocking=True,
    )
