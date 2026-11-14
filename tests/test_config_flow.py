"""Config and options flow tests."""
import io
import json
import tarfile

import pytest
from homeassistant import config_entries, data_entry_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.private_integrations.const import (
    CONF_FULL_NAME,
    CONF_GITHUB_TOKEN,
    CONF_INTEGRATION_NAME,
    CONF_REF,
    CONF_REF_TYPE,
    DOMAIN,
)
from custom_components.private_integrations.repository import Repository

TOKEN = "ghp_test_token"
USER_URL = "https://api.github.com/user"
REPO_URL = "https://api.github.com/repos/owner/foo"
SHA = "a" * 40


def make_tarball(domain="foo", version="1.0.0"):
    """Build a minimal integration tarball."""
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
    """Point the Home Assistant config dir at a per-test temporary folder."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


@pytest.fixture
async def entry(hass, aioclient_mock, isolated_config):
    """Set up one configured Private Integrations entry with a working token."""
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_GITHUB_TOKEN: TOKEN}, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_user_flow_creates_entry(hass, aioclient_mock):
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GITHUB_TOKEN: TOKEN}
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Private Integrations (cypherbits)"
    assert result["data"] == {CONF_GITHUB_TOKEN: TOKEN}


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, "invalid_auth"), (500, "cannot_connect")],
)
async def test_user_flow_errors(hass, aioclient_mock, status, error):
    aioclient_mock.get(USER_URL, status=status)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GITHUB_TOKEN: TOKEN}
    )
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": error}


def _old_entry(hass, data=None, title="Private HACS (cypherbits)"):
    entry = MockConfigEntry(
        domain="private_hacs",
        data=data if data is not None else {CONF_GITHUB_TOKEN: "ghp_old_token"},
        title=title,
        unique_id="private_hacs",
    )
    entry.add_to_hass(hass)
    return entry


async def test_a_previous_private_hacs_is_imported(
    hass, aioclient_mock, isolated_config
):
    old_entry = _old_entry(hass)
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "migrate"
    assert result["description_placeholders"]["title"] == old_entry.title

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {}
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Private Integrations (cypherbits)"
    assert result["data"] == {CONF_GITHUB_TOKEN: "ghp_old_token"}
    assert hass.config_entries.async_entries("private_hacs") == [old_entry]


async def test_an_expired_private_hacs_token_asks_for_a_new_one(
    hass, aioclient_mock, isolated_config
):
    _old_entry(hass)
    aioclient_mock.get(USER_URL, status=401)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "migrate"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {}
    )
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_a_private_hacs_entry_without_a_token_asks_for_one(
    hass, isolated_config
):
    _old_entry(hass, data={})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "migrate"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {}
    )
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_options_manage_lists_repositories(hass, entry):
    store = hass.data[DOMAIN][entry.entry_id]["store"]
    store.upsert(Repository(full_name="owner/foo", ref="main"))
    store.get("owner/foo").installed_version = "main"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    assert result["step_id"] == "manage_repository"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    assert result["step_id"] == "repository_action"
    assert result["description_placeholders"]["repo"].startswith("owner/foo")


async def test_options_change_ref(hass, entry, aioclient_mock):
    aioclient_mock.get(f"{REPO_URL}/commits/v1.2.0", json={"sha": SHA})
    store = hass.data[DOMAIN][entry.entry_id]["store"]
    store.upsert(
        Repository(full_name="owner/foo", ref_type="branch", ref="main")
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "change_ref"}
    )
    assert result["step_id"] == "edit_repository"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_REF_TYPE: "tag", CONF_REF: "v1.2.0"}
    )
    # Back to the action screen, one click away from applying the new ref.
    assert result["step_id"] == "repository_action"
    assert "saved" in result["description_placeholders"]["notice"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "change_ref"}
    )
    assert result["step_id"] == "edit_repository"

    repo = store.get("owner/foo")
    assert repo.ref_type == "tag"
    assert repo.ref == "v1.2.0"


async def test_options_change_ref_uses_default_branch(hass, entry, aioclient_mock):
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo", json={"default_branch": "master"}
    )
    store = hass.data[DOMAIN][entry.entry_id]["store"]
    store.upsert(
        Repository(full_name="owner/foo", ref_type="branch", ref="main")
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "change_ref"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_REF_TYPE: "branch", CONF_REF: ""}
    )
    assert result["step_id"] == "repository_action"
    assert store.get("owner/foo").ref == "master"


async def test_options_remove_keeps_files_when_unchecked(hass, entry):
    store = hass.data[DOMAIN][entry.entry_id]["store"]
    repo = Repository(
        full_name="owner/foo",
        ref_type="branch",
        ref="main",
        integration_name="foo",
    )
    store.upsert(repo)
    target = hass.config.path("custom_components", "foo")
    await hass.async_add_executor_job(
        lambda: __import__("pathlib").Path(target).mkdir(parents=True, exist_ok=True)
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "remove"}
    )
    assert result["step_id"] == "confirm_remove"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"delete_files": False}
    )
    assert result["step_id"] == "removed_confirmation"
    assert result["description_placeholders"]["files"] == "were left on disk"
    assert store.get("owner/foo") is None

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert await hass.async_add_executor_job(
        lambda: __import__("pathlib").Path(target).exists()
    )


async def test_remove_reports_files_kept_when_there_was_nothing_to_delete(
    hass, entry
):
    """The confirmation describes what happened, not what was requested."""
    store = hass.data[DOMAIN][entry.entry_id]["store"]
    store.upsert(
        Repository(
            full_name="owner/foo",
            ref_type="branch",
            ref="main",
            integration_name="foo",
        )
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "remove"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"delete_files": True}
    )

    assert result["step_id"] == "removed_confirmation"
    assert result["description_placeholders"]["files"] == "were left on disk"
    assert store.get("owner/foo") is None


async def test_options_force_reinstall(hass, entry, monkeypatch):
    """The flow can ask for a reinstall of an unchanged commit."""
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.private_integrations.manager import (
        PrivateIntegrationsManager,
    )

    calls: list[tuple[str, bool]] = []

    async def fake_install(self, full_name, *, force=False):
        calls.append((full_name, force))
        raise HomeAssistantError("stop here")

    monkeypatch.setattr(PrivateIntegrationsManager, "install_or_update", fake_install)
    hass.data[DOMAIN][entry.entry_id]["store"].upsert(
        Repository(full_name="owner/foo", ref="main")
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "force_install"}
    )

    assert result["step_id"] == "repository_action"
    assert result["errors"] == {"base": "action_failed"}
    assert calls == [("owner/foo", True)]


async def test_options_update_without_force(hass, entry, monkeypatch):
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.private_integrations.manager import (
        PrivateIntegrationsManager,
    )

    calls: list[tuple[str, bool]] = []

    async def fake_install(self, full_name, *, force=False):
        calls.append((full_name, force))
        raise HomeAssistantError("stop here")

    monkeypatch.setattr(PrivateIntegrationsManager, "install_or_update", fake_install)
    hass.data[DOMAIN][entry.entry_id]["store"].upsert(
        Repository(full_name="owner/foo", ref="main")
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "install"}
    )

    assert calls == [("owner/foo", False)]


# --- Initial setup ----------------------------------------------------------


async def test_user_flow_aborts_when_already_configured(hass, entry, aioclient_mock):
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GITHUB_TOKEN: TOKEN}
    )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- Reauthentication -------------------------------------------------------


async def test_reauth_updates_the_token(hass, entry, aioclient_mock):
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
        },
        data=entry.data,
    )
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GITHUB_TOKEN: "ghp_fresh"}
    )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_GITHUB_TOKEN] == "ghp_fresh"


async def test_reauth_rejects_an_invalid_token(hass, entry, aioclient_mock):
    aioclient_mock.clear_requests()
    aioclient_mock.get(USER_URL, status=401)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
        },
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GITHUB_TOKEN: "bad"}
    )

    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_reports_a_connection_problem(hass, entry, aioclient_mock):
    aioclient_mock.clear_requests()
    aioclient_mock.get(USER_URL, status=500)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
        },
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GITHUB_TOKEN: "any"}
    )

    assert result["errors"] == {"base": "cannot_connect"}


# --- Adding a repository ----------------------------------------------------


def _add_input(**kwargs):
    data = {
        CONF_FULL_NAME: "https://github.com/owner/foo",
        CONF_REF_TYPE: "branch",
        CONF_REF: "",
        "integration_name": "",
        "install_now": False,
    }
    data.update(kwargs)
    return data


async def _open_add(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_repository"}
    )


async def test_add_repository_without_installing(hass, entry, aioclient_mock):
    aioclient_mock.get(REPO_URL, json={"default_branch": "main"})
    store = hass.data[DOMAIN][entry.entry_id]["store"]

    result = await _open_add(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _add_input()
    )

    assert result["step_id"] == "tracked_confirmation"
    assert result["description_placeholders"]["repo"] == "owner/foo"
    assert store.get("owner/foo").ref == "main"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY


async def test_add_repository_can_install_immediately(hass, entry, aioclient_mock):
    aioclient_mock.get(REPO_URL, json={"default_branch": "main"})
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())
    store = hass.data[DOMAIN][entry.entry_id]["store"]

    result = await _open_add(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _add_input(install_now=True)
    )

    assert result["step_id"] == "installed_confirmation"
    assert result["description_placeholders"]["integration"] == "foo"
    assert store.get("owner/foo").installed_sha == SHA

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY


async def test_add_repository_that_is_already_installed(
    hass, entry, aioclient_mock, monkeypatch
):
    from custom_components.private_integrations.manager import (
        InstallResult,
        PrivateIntegrationsManager,
    )

    async def fake_install(self, full_name, *, force=False):
        return InstallResult(self.store.get(full_name), changed=False)

    monkeypatch.setattr(PrivateIntegrationsManager, "install_or_update", fake_install)
    aioclient_mock.get(REPO_URL, json={"default_branch": "main"})

    result = await _open_add(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _add_input(install_now=True)
    )

    assert result["step_id"] == "up_to_date_confirmation"
    assert result["description_placeholders"]["repo"] == "owner/foo"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY


async def test_a_failed_install_keeps_the_repository_tracked(
    hass, entry, aioclient_mock
):
    """Only the download failed: the entry itself is valid and repairable."""
    aioclient_mock.get(REPO_URL, json={"default_branch": "main"})
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", status=500)
    store = hass.data[DOMAIN][entry.entry_id]["store"]

    result = await _open_add(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _add_input(install_now=True)
    )

    assert result["step_id"] == "install_failed"
    assert result["description_placeholders"]["repo"] == "owner/foo"
    assert "Download failed" in result["description_placeholders"]["reason"]
    assert store.get("owner/foo") is not None
    assert store.get("owner/foo").installed_sha is None

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY


async def test_add_repository_reports_a_taken_folder(hass, entry, aioclient_mock):
    store = hass.data[DOMAIN][entry.entry_id]["store"]
    store.upsert(Repository(full_name="other/repo", integration_name="foo"))
    aioclient_mock.get(REPO_URL, json={"default_branch": "main"})

    result = await _open_add(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _add_input(integration_name="foo")
    )

    assert result["errors"] == {"base": "add_failed"}
    detail = result["description_placeholders"]["detail"]
    assert "already managed by other/repo" in detail


async def test_add_repository_reports_bad_input(hass, entry, aioclient_mock):
    result = await _open_add(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _add_input(**{CONF_FULL_NAME: "not a repo"})
    )

    assert result["errors"] == {"base": "add_failed"}


# --- Managing a repository --------------------------------------------------


async def test_manage_aborts_without_repositories(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "no_repositories"


async def test_change_ref_rejects_an_invalid_commit(hass, entry):
    hass.data[DOMAIN][entry.entry_id]["store"].upsert(
        Repository(full_name="owner/foo", ref="main")
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "change_ref"}
    )
    assert result["step_id"] == "edit_repository"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_REF_TYPE: "commit", CONF_REF: "nope"}
    )

    assert result["errors"] == {"base": "action_failed"}
    assert "commit SHA" in result["description_placeholders"]["detail"]


async def test_remove_repository_deletes_its_files(hass, entry):
    from pathlib import Path

    store = hass.data[DOMAIN][entry.entry_id]["store"]
    store.upsert(
        Repository(
            full_name="owner/foo",
            ref="main",
            integration_name="foo",
        )
    )
    dest = Path(hass.config.path("custom_components", "foo"))
    dest.mkdir(parents=True)
    (dest / ".private_integrations.json").write_text('{"full_name": "owner/foo"}')
    (dest / "manifest.json").write_text('{"domain": "foo"}')

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "remove"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"delete_files": True}
    )

    assert result["step_id"] == "removed_confirmation"
    assert result["description_placeholders"]["files"] == (
        "were deleted from `custom_components/`"
    )
    assert not dest.exists()
    assert store.get("owner/foo") is None


async def test_remove_refuses_a_folder_it_did_not_install(hass, entry):
    from pathlib import Path

    store = hass.data[DOMAIN][entry.entry_id]["store"]
    store.upsert(
        Repository(
            full_name="owner/foo",
            ref="main",
            integration_name="foo",
        )
    )
    dest = Path(hass.config.path("custom_components", "foo"))
    dest.mkdir(parents=True)
    (dest / "keep.txt").write_text("keep")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "remove"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"delete_files": True}
    )

    assert result["step_id"] == "confirm_remove"
    assert result["errors"] == {"base": "action_failed"}
    detail = result["description_placeholders"]["detail"]
    assert "not installed by Private Integrations" in detail
    assert (dest / "keep.txt").is_file()
    assert store.get("owner/foo") is not None


# --- Token rotation ---------------------------------------------------------


async def _open_update_token(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "update_token"}
    )


async def test_update_token_replaces_the_stored_one(hass, entry, aioclient_mock):
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})

    result = await _open_update_token(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_GITHUB_TOKEN: "ghp_rotated"}
    )
    await hass.async_block_till_done()

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_GITHUB_TOKEN] == "ghp_rotated"


async def test_update_token_rejects_an_invalid_one(hass, entry, aioclient_mock):
    aioclient_mock.clear_requests()
    aioclient_mock.get(USER_URL, status=401)

    result = await _open_update_token(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_GITHUB_TOKEN: "bad"}
    )

    assert result["step_id"] == "update_token"
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_GITHUB_TOKEN] == TOKEN


async def test_update_token_reports_a_connection_problem(
    hass, entry, aioclient_mock
):
    aioclient_mock.clear_requests()
    aioclient_mock.get(USER_URL, status=500)

    result = await _open_update_token(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_GITHUB_TOKEN: "any"}
    )

    assert result["errors"] == {"base": "cannot_connect"}


async def test_install_from_the_manage_step_reaches_the_confirmation(
    hass, entry, aioclient_mock
):
    hass.data[DOMAIN][entry.entry_id]["store"].upsert(
        Repository(
            full_name="owner/foo",
            ref_type="branch",
            ref="main",
            integration_name="foo",
        )
    )
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "install"}
    )

    assert result["step_id"] == "installed_confirmation"


async def test_options_install_that_changes_nothing_says_so(
    hass, entry, monkeypatch
):
    from custom_components.private_integrations.manager import (
        InstallResult,
        PrivateIntegrationsManager,
    )

    async def fake_install(self, full_name, *, force=False):
        return InstallResult(self.store.get(full_name), changed=False)

    monkeypatch.setattr(PrivateIntegrationsManager, "install_or_update", fake_install)
    hass.data[DOMAIN][entry.entry_id]["store"].upsert(
        Repository(full_name="owner/foo", ref="main")
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "install"}
    )

    assert result["step_id"] == "up_to_date_confirmation"
    assert result["description_placeholders"]["repo"] == "owner/foo"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY


async def test_change_ref_aborts_when_the_repository_vanished(hass, entry):
    store = hass.data[DOMAIN][entry.entry_id]["store"]
    store.upsert(Repository(full_name="owner/foo", ref="main"))

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    store.remove("owner/foo")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "change_ref"}
    )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT


async def test_removal_aborts_when_the_repository_vanished(hass, entry):
    store = hass.data[DOMAIN][entry.entry_id]["store"]
    store.upsert(Repository(full_name="owner/foo", ref="main"))

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    store.remove("owner/foo")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "remove"}
    )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT


async def test_the_list_is_translated(hass, entry):
    """The row is built in Python, so its words have to come from the files."""
    hass.config.language = "es"
    hass.data[DOMAIN][entry.entry_id]["store"].upsert(
        Repository(full_name="owner/foo", ref_type="branch", integration_name="foo")
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    selector = next(iter(result["data_schema"].schema.values()))
    label = selector.config["options"][0]["label"]

    assert "no instalada" in label
    assert "rama por defecto" in label
    assert "not installed" not in label


async def test_an_error_detail_does_not_leak_into_the_next_step(
    hass, entry, monkeypatch
):
    """A failed action must not describe itself on the screen that follows."""
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.private_integrations.manager import (
        PrivateIntegrationsManager,
    )

    async def failing_install(self, full_name, *, force=False):
        raise HomeAssistantError("the download exploded")

    monkeypatch.setattr(
        PrivateIntegrationsManager, "install_or_update", failing_install
    )
    hass.data[DOMAIN][entry.entry_id]["store"].upsert(
        Repository(full_name="owner/foo", ref="main")
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "install"}
    )
    assert "the download exploded" in result["description_placeholders"]["detail"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "change_ref"}
    )

    assert result["step_id"] == "edit_repository"
    assert result["description_placeholders"]["detail"] == ""


async def test_a_wrong_integration_folder_can_be_fixed_without_readding(
    hass, entry, aioclient_mock
):
    """A repository with two integrations used to be unfixable from the UI."""
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    store = hass.data[DOMAIN][entry.entry_id]["store"]
    store.upsert(Repository(full_name="owner/foo", ref_type="branch", ref="main"))

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "change_ref"}
    )
    assert result["step_id"] == "edit_repository"
    assert CONF_INTEGRATION_NAME in result["data_schema"].schema
    # The form offers the current value, not a blank field.
    assert result["description_placeholders"]["repo"].startswith("owner/foo")

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_REF_TYPE: "branch",
            CONF_REF: "main",
            CONF_INTEGRATION_NAME: "foo",
        },
    )

    assert result["step_id"] == "repository_action"
    assert store.get("owner/foo").integration_name == "foo"


async def test_a_repository_with_two_integrations_can_be_completed_later(
    hass, entry, aioclient_mock
):
    """The whole point of keeping it tracked: fix the folder, then install."""
    aioclient_mock.get(REPO_URL, json={"default_branch": "main"})
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(
        f"{REPO_URL}/tarball/{SHA}",
        content=make_two_integrations_tarball(),
    )
    store = hass.data[DOMAIN][entry.entry_id]["store"]

    # Adding without a folder name: the add succeeds, the install cannot choose.
    result = await _open_add(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _add_input(install_now=True)
    )
    assert result["step_id"] == "install_failed"
    assert "2 integrations" in result["description_placeholders"]["reason"]
    assert store.get("owner/foo") is not None
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )

    # Fix the folder from the manage screen…
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "change_ref"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_REF_TYPE: "branch",
            CONF_REF: "main",
            CONF_INTEGRATION_NAME: "foo",
        },
    )
    assert result["step_id"] == "repository_action"

    # …and install the one that was chosen.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "install"}
    )

    assert result["step_id"] == "installed_confirmation"
    assert result["description_placeholders"]["integration"] == "foo"
    assert store.get("owner/foo").integration_name == "foo"


def make_two_integrations_tarball():
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for domain in ("foo", "bar"):
            content = json.dumps({"domain": domain, "version": "1.0.0"}).encode()
            info = tarfile.TarInfo(f"top/custom_components/{domain}/manifest.json")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


async def test_a_bad_token_can_be_corrected_in_the_same_flow(hass, aioclient_mock):
    """Typing it wrong and then right must not abort with "already in progress"."""
    aioclient_mock.get(USER_URL, status=401)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GITHUB_TOKEN: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GITHUB_TOKEN: "right"}
    )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Private Integrations (cypherbits)"


# --- Discovering integrations in the account -------------------------------


def _candidate(**overrides):
    from custom_components.private_integrations.catalog import Candidate

    values = {
        "full_name": "owner/foo",
        "integration_name": "foo",
        "version": "1.0.0",
        "default_branch": "main",
    }
    values.update(overrides)
    return Candidate(**values)


def _fake_detection(monkeypatch, result):
    """Replace the lookup: these tests are about the flow, not about GitHub."""
    from custom_components.private_integrations import config_flow

    calls = {}

    async def fake(client, *, tracked=(), installed=(), max_repositories=60):
        calls["tracked"] = set(tracked)
        calls["installed"] = set(installed)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(config_flow, "async_detect_integrations", fake)
    return calls


async def _open_discover(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "discover"}
    )


async def test_discovery_offers_what_it_found(hass, entry, monkeypatch):
    from custom_components.private_integrations.catalog import Discovery

    _fake_detection(monkeypatch, Discovery([_candidate()], inspected=3))

    result = await _open_discover(hass, entry)

    assert result["step_id"] == "proposals"
    assert result["description_placeholders"]["count"] == "1"
    assert result["description_placeholders"]["note"] == ""


async def test_a_capped_discovery_says_it_may_be_incomplete(
    hass, entry, monkeypatch
):
    """A partial list is useful, but it must not look complete."""
    from custom_components.private_integrations.catalog import Discovery

    _fake_detection(
        monkeypatch,
        Discovery([_candidate()], inspected=60, truncated=True, rate_limited=True),
    )

    result = await _open_discover(hass, entry)

    note = result["description_placeholders"]["note"]
    assert "60" in note
    assert "incomplete" in note


async def test_a_proposal_says_when_the_folder_is_already_on_disk(
    hass, entry, monkeypatch
):
    from custom_components.private_integrations.catalog import Discovery

    _fake_detection(
        monkeypatch, Discovery([_candidate(installed=True)], inspected=1)
    )

    result = await _open_discover(hass, entry)

    selector = next(iter(result["data_schema"].schema.values()))
    assert "already installed" in selector.config["options"][0]["label"]


async def test_a_discovered_integration_can_be_added_and_installed(
    hass, entry, aioclient_mock, monkeypatch
):
    from custom_components.private_integrations.catalog import Discovery

    _fake_detection(monkeypatch, Discovery([_candidate()], inspected=1))
    aioclient_mock.get(REPO_URL, json={"default_branch": "main"})
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())
    store = hass.data[DOMAIN][entry.entry_id]["store"]

    result = await _open_discover(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo::foo"}
    )

    assert result["step_id"] == "proposal"
    assert result["description_placeholders"]["repo"] == "owner/foo"
    assert result["description_placeholders"]["integration"] == "foo"
    assert result["description_placeholders"]["version"] == "1.0.0"
    assert result["description_placeholders"]["branch"] == "main"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_REF_TYPE: "branch", CONF_REF: "", "install_now": True},
    )

    assert result["step_id"] == "installed_confirmation"
    assert store.get("owner/foo").integration_name == "foo"
    assert store.get("owner/foo").installed_sha == SHA


async def test_discovery_does_not_offer_tracked_repositories(
    hass, entry, monkeypatch
):
    from custom_components.private_integrations.catalog import Discovery

    calls = _fake_detection(monkeypatch, Discovery([_candidate()], inspected=1))
    hass.data[DOMAIN][entry.entry_id]["store"].upsert(
        Repository(full_name="other/tracked", ref="main")
    )

    await _open_discover(hass, entry)

    assert "other/tracked" in calls["tracked"]


async def test_discovery_without_results_explains_it(hass, entry, monkeypatch):
    from custom_components.private_integrations.catalog import Discovery

    _fake_detection(monkeypatch, Discovery([], inspected=4, listed=4))

    result = await _open_discover(hass, entry)

    assert result["step_id"] == "discover"
    assert result["errors"] == {"base": "no_candidates"}
    assert result["description_placeholders"]["inspected"] == "4"


async def test_discovery_rate_limit_is_explained(hass, entry, monkeypatch):
    from custom_components.private_integrations.github import GitHubRateLimitError

    _fake_detection(monkeypatch, GitHubRateLimitError("slow down", 30))

    result = await _open_discover(hass, entry)

    assert result["errors"] == {"base": "rate_limited"}
    assert "30 seconds" in result["description_placeholders"]["detail"]


async def test_discovery_auth_error_is_explained(hass, entry, monkeypatch):
    from custom_components.private_integrations.github import GitHubAuthError

    _fake_detection(monkeypatch, GitHubAuthError("nope"))

    result = await _open_discover(hass, entry)

    assert result["errors"] == {"base": "invalid_auth"}


async def test_discovery_network_error_is_explained(hass, entry, monkeypatch):
    from custom_components.private_integrations.github import GitHubError

    _fake_detection(monkeypatch, GitHubError("down"))

    result = await _open_discover(hass, entry)

    assert result["errors"] == {"base": "cannot_connect"}


async def test_the_discovery_screen_goes_back_to_the_menu(hass, entry, monkeypatch):
    from custom_components.private_integrations.catalog import Discovery

    _fake_detection(monkeypatch, Discovery([], inspected=0))

    result = await _open_discover(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )

    assert result["step_id"] == "init"


async def test_a_candidate_that_cannot_be_added_is_reported(
    hass, entry, aioclient_mock, monkeypatch
):
    from custom_components.private_integrations.catalog import Discovery

    _fake_detection(monkeypatch, Discovery([_candidate()], inspected=1))
    aioclient_mock.get(REPO_URL, status=404)

    result = await _open_discover(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FULL_NAME: "owner/foo::foo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_REF_TYPE: "release", CONF_REF: "", "install_now": False},
    )

    assert result["errors"] == {"base": "add_failed"}
    assert "owner/foo" in result["description_placeholders"]["detail"]


async def test_the_list_says_how_many_integrations_it_does_not_manage(
    hass, entry
):
    from pathlib import Path

    hass.data[DOMAIN][entry.entry_id]["store"].upsert(
        Repository(full_name="owner/foo", ref="main", integration_name="foo")
    )
    components = Path(hass.config.path("custom_components"))
    for name in ("foo", "hacs", "other"):
        (components / name).mkdir(parents=True)
        (components / name / "manifest.json").write_text("{}")
    # "foo" belongs to the tracked repository; the marker is what says so.
    (components / "foo" / ".private_integrations.json").write_text(
        '{"full_name": "owner/foo"}'
    )
    # Not an integration, and not counted as one.
    (components / "scratch").mkdir()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )

    assert result["description_placeholders"]["count"] == "1"
    # "hacs" is protected and "scratch" has no manifest: only "other" is left.
    assert result["description_placeholders"]["other"] == "1"


async def test_a_tracked_folder_without_a_marker_is_still_unmanaged(
    hass, entry
):
    """Tracking a name does not make another tool's folder ours."""
    from pathlib import Path

    hass.data[DOMAIN][entry.entry_id]["store"].upsert(
        Repository(full_name="owner/foo", ref="main", integration_name="foo")
    )
    components = Path(hass.config.path("custom_components"))
    (components / "foo").mkdir(parents=True)
    (components / "foo" / "manifest.json").write_text("{}")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_repository"}
    )

    assert result["description_placeholders"]["other"] == "1"


async def test_discovery_with_no_visible_repositories_explains_it(
    hass, entry, monkeypatch
):
    from custom_components.private_integrations.catalog import Discovery

    _fake_detection(monkeypatch, Discovery([], inspected=0))

    result = await _open_discover(hass, entry)

    assert result["errors"] == {"base": "no_repositories_visible"}


async def test_discovery_that_could_not_read_anything_explains_it(
    hass, entry, monkeypatch
):
    from custom_components.private_integrations.catalog import Discovery

    _fake_detection(
        monkeypatch, Discovery([], inspected=3, listed=3, unreadable=3)
    )

    result = await _open_discover(hass, entry)

    assert result["errors"] == {"base": "not_readable"}
    assert result["description_placeholders"]["inspected"] == "3"


async def test_discovery_with_every_repository_already_tracked_says_so(
    hass, entry, monkeypatch
):
    """Not the same as "GitHub returned nothing": the token sees repositories."""
    from custom_components.private_integrations.catalog import Discovery

    _fake_detection(monkeypatch, Discovery([], inspected=0, listed=4))

    result = await _open_discover(hass, entry)

    assert result["errors"] == {"base": "no_candidates"}
    assert result["description_placeholders"]["inspected"] == "0"


async def test_both_lists_are_rendered_as_lists_not_dropdowns(hass, entry, monkeypatch):
    """The point of the screen is reading a list, not opening a dropdown."""
    from homeassistant.helpers.selector import SelectSelectorMode

    from custom_components.private_integrations.catalog import Discovery

    hass.data[DOMAIN][entry.entry_id]["store"].upsert(
        Repository(full_name="owner/foo", ref="main", integration_name="foo")
    )
    _fake_detection(monkeypatch, Discovery([_candidate()], inspected=1, listed=1))

    manage = await hass.config_entries.options.async_init(entry.entry_id)
    manage = await hass.config_entries.options.async_configure(
        manage["flow_id"], {"next_step_id": "manage_repository"}
    )
    proposals = await _open_discover(hass, entry)

    for result in (manage, proposals):
        schema = result["data_schema"].schema
        selector = next(iter(schema.values()))
        assert selector.config["mode"] == SelectSelectorMode.LIST.value


async def test_discovery_end_to_end_with_the_real_github_client(
    hass, entry, aioclient_mock
):
    """No patched lookup: the real client, its URLs and the flow together."""
    import base64

    list_url = (
        "https://api.github.com/user/repos?per_page=100&page=1&sort=updated"
        "&affiliation=owner%2Ccollaborator%2Corganization_member"
    )
    aioclient_mock.get(
        list_url,
        json=[
            {
                "full_name": "owner/foo",
                "default_branch": "main",
                "private": True,
            },
            {"full_name": "owner/no_integration", "default_branch": "main"},
        ],
    )
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/contents/custom_components?ref=main",
        json=[{"name": "foo", "type": "dir"}],
    )
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/contents/custom_components/foo"
        "/manifest.json?ref=main",
        json={
            "encoding": "base64",
            "content": base64.b64encode(
                json.dumps({"domain": "foo", "version": "0.9.0"}).encode()
            ).decode(),
        },
    )
    aioclient_mock.get(
        "https://api.github.com/repos/owner/no_integration"
        "/contents/custom_components?ref=main",
        status=404,
    )

    result = await _open_discover(hass, entry)

    assert result["step_id"] == "proposals"
    assert result["description_placeholders"]["count"] == "1"
    selector = next(iter(result["data_schema"].schema.values()))
    assert selector.config["options"][0]["label"] == (
        "owner/foo — custom_components/foo (0.9.0) · private"
    )


async def test_discovery_end_to_end_when_nothing_is_installable(
    hass, entry, aioclient_mock
):
    list_url = (
        "https://api.github.com/user/repos?per_page=100&page=1&sort=updated"
        "&affiliation=owner%2Ccollaborator%2Corganization_member"
    )
    aioclient_mock.get(
        list_url, json=[{"full_name": "owner/plain", "default_branch": "main"}]
    )
    aioclient_mock.get(
        "https://api.github.com/repos/owner/plain/contents/custom_components?ref=main",
        status=404,
    )

    result = await _open_discover(hass, entry)

    assert result["step_id"] == "discover"
    assert result["errors"] == {"base": "no_candidates"}
    assert result["description_placeholders"]["inspected"] == "1"
