"""End-to-end check of the Private HACS import during Home Assistant setup.

The test harness mocks Home Assistant's storage for every test. The migration
writes real files and depends on the store reading them, so this module replaces
that mock with real storage under the test's temporary config directory.
"""
import json

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.private_integrations.const import CONF_GITHUB_TOKEN, DOMAIN

TOKEN = "ghp_test_token"
USER_URL = "https://api.github.com/user"
SHA = "a" * 40


@pytest.fixture
def hass_storage():
    """Disable the storage mock so the store reads the migrated file."""
    return {}


@pytest.fixture
def isolated_config(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    return tmp_path


def _entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_GITHUB_TOKEN: TOKEN}, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_imports_a_previous_private_hacs_installation(
    hass, aioclient_mock, isolated_config
):
    """The old registry and marker are imported before the store is loaded."""
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})
    storage = isolated_config / ".storage"
    storage.mkdir(parents=True)
    (storage / "private_hacs.repositories").write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 1,
                "key": "private_hacs.repositories",
                "data": {
                    "repositories": [
                        {
                            "full_name": "owner/foo",
                            "ref_type": "branch",
                            "ref": "main",
                            "integration_name": "foo",
                            "installed_version": "main",
                            "installed_sha": SHA,
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    folder = isolated_config / "custom_components" / "foo"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text('{"domain": "foo"}', encoding="utf-8")
    (folder / ".private_hacs.json").write_text(
        json.dumps({"full_name": "owner/foo"}), encoding="utf-8"
    )

    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    store = hass.data[DOMAIN][entry.entry_id]["store"]
    repo = store.get("owner/foo")
    assert repo is not None
    assert repo.installed_sha == SHA
    assert (folder / ".private_integrations.json").is_file()
    assert not (folder / ".private_hacs.json").exists()
