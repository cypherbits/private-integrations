"""Tests for the repository manager (install path)."""
import io
import json
import tarfile

import pytest
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.private_integrations.const import CONF_GITHUB_TOKEN, DOMAIN
from custom_components.private_integrations.installer import is_managed, read_marker
from custom_components.private_integrations.repository import Repository

TOKEN = "ghp_test_token"
USER_URL = "https://api.github.com/user"
REPO_URL = "https://api.github.com/repos/owner/foo"
SHA = "a" * 40


def make_tarball(domain="foo", version="1.0.0", extra=(), extra_integration=False):
    members = [
        ("top", None),
        (
            f"top/custom_components/{domain}/manifest.json",
            json.dumps({"domain": domain, "version": version}).encode(),
        ),
        (f"top/custom_components/{domain}/__init__.py", b"# version 1\n"),
        *extra,
    ]
    if extra_integration:
        members.append(
            (
                "top/custom_components/other/manifest.json",
                json.dumps({"domain": "other", "version": "1.0.0"}).encode(),
            )
        )
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


def make_tarball_without_integrations():
    """A GitHub-style tarball of a repository that has no custom_components/."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("top/README.md")
        info.size = len(b"readme")
        archive.addfile(info, io.BytesIO(b"readme"))
    return buffer.getvalue()


@pytest.fixture
def isolated_config(hass, tmp_path):
    """Point the Home Assistant config dir at a per-test temporary folder."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


@pytest.fixture
async def manager(hass, aioclient_mock, isolated_config):
    """A configured entry with a working token, and its manager."""
    aioclient_mock.get(USER_URL, json={"login": "cypherbits"})
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_GITHUB_TOKEN: TOKEN}, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]["manager"]


def track(manager, **kwargs):
    data = {"full_name": "owner/foo", "ref_type": "branch", "ref": "main"}
    data.update(kwargs)
    repo = Repository(**data)
    manager.store.upsert(repo)
    return repo


async def test_install_writes_files_and_marker(hass, aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())

    result = await manager.install_or_update("owner/foo")
    repo = result.repository

    dest = manager._custom_components_dir / "foo"
    assert result.changed
    assert (dest / "manifest.json").is_file()
    assert (dest / "__init__.py").read_text() == "# version 1\n"
    assert is_managed(dest)
    assert read_marker(dest)["full_name"] == "owner/foo"
    assert repo.installed_sha == SHA
    assert repo.installed_version == "main"
    assert repo.integration_name == "foo"


async def test_install_downloads_the_resolved_sha(aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())

    await manager.install_or_update("owner/foo")

    calls = str(aioclient_mock.mock_calls)
    assert f"/tarball/{SHA}" in calls
    assert "/tarball/main" not in calls


async def test_install_refuses_foreign_folder(hass, aioclient_mock, manager):
    track(manager)
    foreign = manager._custom_components_dir / "foo"
    foreign.mkdir(parents=True, exist_ok=True)
    (foreign / "keep.txt").write_text("keep")

    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())

    with pytest.raises(HomeAssistantError, match="refusing to overwrite"):
        await manager.install_or_update("owner/foo")

    assert (foreign / "keep.txt").read_text() == "keep"


async def test_failed_upgrade_keeps_previous_version(hass, aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())
    await manager.install_or_update("owner/foo")

    dest = manager._custom_components_dir / "foo"
    original = (dest / "__init__.py").read_text()

    broken = make_tarball(
        extra=[("top/custom_components/foo/../../escaped.txt", b"pwn")]
    )
    manager.store.get("owner/foo").ref = "next"
    aioclient_mock.get(f"{REPO_URL}/commits/next", json={"sha": "b" * 40})
    aioclient_mock.get(f"{REPO_URL}/tarball/{'b' * 40}", content=broken)

    with pytest.raises(HomeAssistantError, match="Install failed"):
        await manager.install_or_update("owner/foo")

    assert (dest / "__init__.py").read_text() == original
    assert not (dest.parent / "escaped.txt").exists()


async def test_rate_limit_is_reported_as_retryable(aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(
        f"{REPO_URL}/commits/main",
        status=403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"},
    )

    with pytest.raises(HomeAssistantError, match="retry in \\d+ seconds"):
        await manager.install_or_update("owner/foo")


async def test_release_falls_back_to_prerelease(aioclient_mock, manager):
    track(manager, ref_type="release", ref=None)
    aioclient_mock.get(f"{REPO_URL}/releases/latest", status=404)
    aioclient_mock.get(
        f"{REPO_URL}/releases",
        json=[{"tag_name": "v2.0.0-beta.1", "prerelease": True, "draft": False}],
    )
    aioclient_mock.get(
        f"{REPO_URL}/commits/v2.0.0-beta.1", json={"sha": SHA}
    )
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())

    result = await manager.install_or_update("owner/foo")
    repo = result.repository

    assert result.changed
    assert repo.installed_version == "v2.0.0-beta.1"
    assert repo.installed_sha == SHA


async def test_missing_ref_is_reported(aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", status=422)

    with pytest.raises(HomeAssistantError, match="not found"):
        await manager.install_or_update("owner/foo")


async def test_add_repository_uses_default_branch(aioclient_mock, manager):
    aioclient_mock.get(REPO_URL, json={"default_branch": "master"})

    repo = await manager.add_repository("owner/foo")

    assert repo.ref == "master"


async def test_update_tracking_uses_default_branch(aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(REPO_URL, json={"default_branch": "develop"})
    aioclient_mock.get(f"{REPO_URL}/commits/develop", json={"sha": SHA})

    repo = await manager.update_tracking("owner/foo", "branch", "")

    assert repo.ref == "develop"


async def test_update_tracking_validates_commit_sha(manager):
    track(manager)

    with pytest.raises(HomeAssistantError, match="commit SHA"):
        await manager.update_tracking("owner/foo", "commit", "not-a-sha")


# --- Removal ---------------------------------------------------------------


async def test_remove_deletes_a_folder_it_installed(hass, aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())
    await manager.install_or_update("owner/foo")
    dest = manager._custom_components_dir / "foo"
    assert dest.is_dir()

    assert await manager.remove_repository("owner/foo") is True

    assert not dest.exists()
    assert manager.store.get("owner/foo") is None


async def test_remove_refuses_a_foreign_folder(hass, manager):
    foreign = manager._custom_components_dir / "foo"
    foreign.mkdir(parents=True)
    (foreign / "keep.txt").write_text("installed by someone else")
    track(manager, integration_name="foo")

    with pytest.raises(HomeAssistantError, match="was not installed by Private"):
        await manager.remove_repository("owner/foo")

    assert (foreign / "keep.txt").read_text() == "installed by someone else"
    assert manager.store.get("owner/foo") is not None


async def test_remove_refuses_a_folder_of_another_repository(hass, manager):
    other = manager._custom_components_dir / "foo"
    other.mkdir(parents=True)
    (other / "keep.txt").write_text("keep")
    (other / ".private_integrations.json").write_text(
        json.dumps({"full_name": "other/repo"})
    )
    track(manager, integration_name="foo")
    track(manager, full_name="other/repo", integration_name="foo")

    with pytest.raises(HomeAssistantError, match="is installed from other/repo"):
        await manager.remove_repository("owner/foo")

    assert (other / "keep.txt").is_file()
    assert manager.store.get("owner/foo") is not None


async def test_remove_without_deleting_files(hass, manager):
    dest = manager._custom_components_dir / "foo"
    dest.mkdir(parents=True)
    (dest / "keep.txt").write_text("keep")
    track(manager, integration_name="foo")

    await manager.remove_repository("owner/foo", delete_files=False)

    assert (dest / "keep.txt").read_text() == "keep"
    assert manager.store.get("owner/foo") is None


async def test_remove_reports_when_there_were_no_files(hass, manager):
    """Deleting a folder that is already gone must not claim it did anything."""
    track(manager, integration_name="foo")

    assert await manager.remove_repository("owner/foo") is False
    assert manager.store.get("owner/foo") is None


async def test_remove_keeps_tracking_when_files_cannot_be_deleted(hass, manager):
    protected = manager._custom_components_dir / "hacs"
    protected.mkdir(parents=True)
    (protected / ".private_integrations.json").write_text(
        json.dumps({"full_name": "owner/foo"})
    )
    track(manager, integration_name="hacs")

    with pytest.raises(HomeAssistantError, match="Could not finish deleting"):
        await manager.remove_repository("owner/foo")

    assert protected.is_dir()
    assert manager.store.get("owner/foo") is not None


async def test_remove_service_accepts_delete_files(hass, aioclient_mock, manager):
    dest = manager._custom_components_dir / "foo"
    dest.mkdir(parents=True)
    (dest / "keep.txt").write_text("keep")
    track(manager, integration_name="foo")

    await hass.services.async_call(
        DOMAIN,
        "remove_repository",
        {"full_name": "owner/foo", "delete_files": False},
        blocking=True,
    )

    assert (dest / "keep.txt").is_file()
    assert manager.store.get("owner/foo") is None


# --- Reinstalling ----------------------------------------------------------


def downloads(aioclient_mock) -> int:
    return str(aioclient_mock.mock_calls).count("/tarball/")


async def test_reinstalling_the_same_sha_changes_nothing(
    hass, aioclient_mock, manager
):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())
    first_install = await manager.install_or_update("owner/foo")
    target = manager._custom_components_dir / "foo" / "manifest.json"
    first = target.stat().st_ino

    second_install = await manager.install_or_update("owner/foo")

    assert first_install.changed
    assert not second_install.changed
    assert downloads(aioclient_mock) == 1
    assert target.stat().st_ino == first


async def test_force_reinstalls_the_same_sha(hass, aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())
    await manager.install_or_update("owner/foo")

    await manager.install_or_update("owner/foo", force=True)

    assert downloads(aioclient_mock) == 2


async def test_reinstall_after_the_folder_was_removed(hass, aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())
    await manager.install_or_update("owner/foo")
    dest = manager._custom_components_dir / "foo"
    await hass.async_add_executor_job(lambda: __import__("shutil").rmtree(dest))

    await manager.install_or_update("owner/foo")

    assert downloads(aioclient_mock) == 2
    assert (dest / "manifest.json").is_file()


async def test_concurrent_installs_run_once(hass, aioclient_mock, manager):
    import asyncio

    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())

    await asyncio.gather(
        manager.install_or_update("owner/foo"),
        manager.install_or_update("owner/foo"),
    )

    assert downloads(aioclient_mock) == 1
    dest = manager._custom_components_dir / "foo"
    assert (dest / "manifest.json").is_file()
    leftovers = [
        p.name
        for p in manager._custom_components_dir.iterdir()
        if p.name.startswith(".")
    ]
    assert leftovers == []


async def test_add_repository_validates_the_integration_name(aioclient_mock, manager):
    aioclient_mock.get(REPO_URL, json={"default_branch": "main"})

    with pytest.raises(HomeAssistantError, match="folder name"):
        await manager.add_repository(
            "owner/foo",
            ref_type="branch",
            ref="main",
            integration_name="my-int",
        )

    assert manager.store.get("owner/foo") is None


# --- One folder, one repository --------------------------------------------


async def test_a_second_repository_cannot_take_over_a_folder(
    hass, aioclient_mock, manager
):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball(version="1.0"))
    await manager.install_or_update("owner/foo")
    dest = manager._custom_components_dir / "foo"
    installed = (dest / "manifest.json").read_text()

    manager.store.upsert(
        Repository(
            full_name="owner/bar",
            ref_type="branch",
            ref="main",
            integration_name="foo",
        )
    )
    bar_url = "https://api.github.com/repos/owner/bar"
    aioclient_mock.get(f"{bar_url}/commits/main", json={"sha": SHA})
    aioclient_mock.get(
        f"{bar_url}/tarball/{SHA}", content=make_tarball(version="9.9")
    )

    with pytest.raises(HomeAssistantError, match="installed from owner/foo"):
        await manager.install_or_update("owner/bar")

    assert (dest / "manifest.json").read_text() == installed
    assert read_marker(dest)["full_name"] == "owner/foo"


async def test_same_sha_is_not_skipped_for_a_folder_of_another_repo(
    hass, aioclient_mock, manager
):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())
    await manager.install_or_update("owner/foo")

    manager.store.upsert(
        Repository(
            full_name="owner/bar",
            ref_type="branch",
            ref="main",
            integration_name="foo",
            installed_sha=SHA,
            installed_version="main",
        )
    )
    bar_url = "https://api.github.com/repos/owner/bar"
    aioclient_mock.get(f"{bar_url}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{bar_url}/tarball/{SHA}", content=make_tarball())

    with pytest.raises(HomeAssistantError, match="installed from owner/foo"):
        await manager.install_or_update("owner/bar")

    assert "/repos/owner/bar/tarball" in str(aioclient_mock.mock_calls)


async def test_add_repository_rejects_a_folder_taken_by_another(manager):
    track(manager, integration_name="foo")

    with pytest.raises(HomeAssistantError, match="already managed by owner/foo"):
        await manager.add_repository(
            "owner/bar",
            ref_type="branch",
            ref="main",
            integration_name="foo",
        )


async def test_installed_marker_lands_with_the_files(hass, aioclient_mock, manager):
    """No window where the folder exists without saying who installed it."""
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())

    await manager.install_or_update("owner/foo")

    marker = read_marker(manager._custom_components_dir / "foo")
    assert marker["full_name"] == "owner/foo"
    assert marker["installed_sha"] == SHA
    assert marker["integration_name"] == "foo"


async def test_a_folder_without_a_marker_is_never_replaced(
    hass, aioclient_mock, manager
):
    """Even a recorded install does not grant ownership: only the marker does."""
    dest = manager._custom_components_dir / "foo"
    dest.mkdir(parents=True)
    (dest / "manifest.json").write_text('{"domain": "foo"}')
    track(manager, integration_name="foo", installed_sha=SHA, installed_version="main")
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())

    with pytest.raises(HomeAssistantError, match="was not installed by Private"):
        await manager.install_or_update("owner/foo")

    assert dest.is_dir()
    assert read_marker(dest) is None


async def test_remove_reports_an_owner_that_is_no_longer_tracked(hass, manager):
    other = manager._custom_components_dir / "foo"
    other.mkdir(parents=True)
    (other / "keep.txt").write_text("keep")
    (other / ".private_integrations.json").write_text(
        json.dumps({"full_name": "gone/repo"})
    )
    track(manager, integration_name="foo")

    with pytest.raises(HomeAssistantError, match="no longer tracked"):
        await manager.remove_repository("owner/foo")

    assert (other / "keep.txt").is_file()
    assert manager.store.get("owner/foo") is not None


async def test_a_folder_of_an_untracked_repository_is_adopted(
    hass, aioclient_mock, manager
):
    """No dead end: the folder of a removed repository can be taken over."""
    track(manager, integration_name="foo")
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball(version="1.0"))
    await manager.install_or_update("owner/foo")
    dest = manager._custom_components_dir / "foo"

    await manager.remove_repository("owner/foo", delete_files=False)
    manager.store.upsert(
        Repository(
            full_name="owner/bar",
            ref_type="branch",
            ref="main",
            integration_name="foo",
        )
    )
    bar_url = "https://api.github.com/repos/owner/bar"
    aioclient_mock.get(f"{bar_url}/commits/main", json={"sha": SHA})
    aioclient_mock.get(
        f"{bar_url}/tarball/{SHA}", content=make_tarball(version="2.0")
    )

    await manager.install_or_update("owner/bar")

    assert read_marker(dest)["full_name"] == "owner/bar"
    assert json.loads((dest / "manifest.json").read_text())["version"] == "2.0"


async def test_a_folder_of_a_tracked_repository_is_still_refused(
    hass, aioclient_mock, manager
):
    track(manager, integration_name="foo")
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())
    await manager.install_or_update("owner/foo")

    track(manager, full_name="owner/bar", integration_name="foo")
    bar_url = "https://api.github.com/repos/owner/bar"
    aioclient_mock.get(f"{bar_url}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{bar_url}/tarball/{SHA}", content=make_tarball())

    with pytest.raises(HomeAssistantError, match="cannot share"):
        await manager.install_or_update("owner/bar")


async def test_a_queued_install_does_not_resurrect_a_removed_repository(
    hass, aioclient_mock, manager
):
    """The repository is read under the lock, not before waiting for it."""
    import asyncio

    track(manager, integration_name="foo")
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())

    await manager._lock.acquire()
    task = hass.async_create_task(manager.install_or_update("owner/foo"))
    for _ in range(3):
        await asyncio.sleep(0)  # let the task reach the lock, do not wait on it
    manager.store.remove("owner/foo")  # removed while the install was queued
    await manager.store.async_save()
    manager._lock.release()

    with pytest.raises(HomeAssistantError, match="not tracked"):
        await task

    assert manager.store.get("owner/foo") is None
    assert not (manager._custom_components_dir / "foo").exists()
    assert "/tarball/" not in str(aioclient_mock.mock_calls)


# --- Rejected input ---------------------------------------------------------


async def test_parse_repo_input_rejects_garbage(manager):
    with pytest.raises(HomeAssistantError, match="Could not parse"):
        manager.parse_repo_input("not a repository")


async def test_add_repository_rejects_an_unknown_ref_type(aioclient_mock, manager):
    with pytest.raises(HomeAssistantError, match="Invalid ref_type"):
        await manager.add_repository("owner/foo", ref_type="banana", ref="main")


async def test_add_repository_rejects_a_duplicate(aioclient_mock, manager):
    track(manager)

    with pytest.raises(HomeAssistantError, match="already tracked"):
        await manager.add_repository("owner/foo")


async def test_add_repository_reports_a_missing_repository(aioclient_mock, manager):
    aioclient_mock.get(REPO_URL, status=404)

    with pytest.raises(HomeAssistantError, match="repository not found"):
        await manager.add_repository("owner/foo")


async def test_add_repository_reports_other_github_errors(aioclient_mock, manager):
    aioclient_mock.get(REPO_URL, status=500)

    with pytest.raises(HomeAssistantError, match="Cannot access owner/foo"):
        await manager.add_repository("owner/foo")


async def test_add_repository_reports_a_rate_limit(aioclient_mock, manager):
    aioclient_mock.get(REPO_URL, status=403, headers={"Retry-After": "12"})

    with pytest.raises(HomeAssistantError, match="retry in 12 seconds"):
        await manager.add_repository("owner/foo")


async def test_a_blank_branch_ref_uses_the_default_branch(aioclient_mock, manager):
    aioclient_mock.get(REPO_URL, json={"default_branch": "develop"})

    repo = await manager.add_repository("owner/foo", ref_type="branch", ref="")

    assert repo.ref == "develop"


async def test_add_repository_requires_a_ref_for_a_tag(aioclient_mock, manager):
    aioclient_mock.get(REPO_URL, json={})

    with pytest.raises(HomeAssistantError, match="A ref is required"):
        await manager.add_repository("owner/foo", ref_type="tag", ref="")


async def test_add_repository_validates_a_commit_sha(aioclient_mock, manager):
    aioclient_mock.get(REPO_URL, json={})

    with pytest.raises(HomeAssistantError, match="commit SHA"):
        await manager.add_repository("owner/foo", ref_type="commit", ref="nope")


async def test_add_repository_accepts_a_release_without_a_ref(
    aioclient_mock, manager
):
    aioclient_mock.get(REPO_URL, json={})

    repo = await manager.add_repository("owner/foo", ref_type="release", ref="")

    assert repo.ref is None


async def test_update_tracking_requires_a_tracked_repository(manager):
    with pytest.raises(HomeAssistantError, match="not tracked"):
        await manager.update_tracking("owner/foo", "branch", "main")


async def test_update_tracking_reports_github_errors(aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(REPO_URL, status=404)

    with pytest.raises(HomeAssistantError, match="Cannot read owner/foo"):
        await manager.update_tracking("owner/foo", "branch", "")


async def test_update_tracking_reports_a_rate_limit(aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(REPO_URL, status=429)

    with pytest.raises(HomeAssistantError, match="rate limit"):
        await manager.update_tracking("owner/foo", "branch", "")


async def test_remove_repository_requires_a_tracked_repository(manager):
    with pytest.raises(HomeAssistantError, match="not tracked"):
        await manager.remove_repository("owner/foo")


async def test_install_requires_a_tracked_repository(manager):
    with pytest.raises(HomeAssistantError, match="not tracked"):
        await manager.install_or_update("owner/foo")


async def test_install_needs_a_published_release(aioclient_mock, manager):
    track(manager, ref_type="release", ref=None)
    aioclient_mock.get(f"{REPO_URL}/releases/latest", status=404)
    aioclient_mock.get(f"{REPO_URL}/releases", json=[])

    with pytest.raises(HomeAssistantError, match="no published release"):
        await manager.install_or_update("owner/foo")


async def test_install_needs_a_ref_when_tracking_one(aioclient_mock, manager):
    track(manager, ref_type="branch", ref=None)

    with pytest.raises(HomeAssistantError, match="no ref configured"):
        await manager.install_or_update("owner/foo")


async def test_install_reports_a_bad_archive(aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=b"not a tarball")

    with pytest.raises(HomeAssistantError, match="Install failed"):
        await manager.install_or_update("owner/foo")


async def test_install_reports_multiple_integrations(aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(
        f"{REPO_URL}/tarball/{SHA}", content=make_tarball(extra_integration=True)
    )

    with pytest.raises(HomeAssistantError, match="integrations"):
        await manager.install_or_update("owner/foo")


async def test_install_reports_a_missing_named_integration(aioclient_mock, manager):
    track(manager, integration_name="other")
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())

    with pytest.raises(HomeAssistantError, match="not found in owner/foo"):
        await manager.install_or_update("owner/foo")


async def test_install_rate_limit_without_a_hint(aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", status=429)

    with pytest.raises(HomeAssistantError, match="try again later"):
        await manager.install_or_update("owner/foo")


async def test_add_repository_without_an_integration_name(aioclient_mock, manager):
    aioclient_mock.get(REPO_URL, json={"default_branch": "main"})
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})

    repo = await manager.add_repository("owner/foo", integration_name=None)

    assert repo.integration_name is None


async def test_add_repository_treats_a_blank_integration_name_as_none(
    aioclient_mock, manager
):
    aioclient_mock.get(REPO_URL, json={"default_branch": "main"})

    repo = await manager.add_repository("owner/foo", integration_name="   ")

    assert repo.integration_name is None


async def test_remove_without_an_installed_folder(hass, manager):
    track(manager, integration_name="foo")

    await manager.remove_repository("owner/foo")

    assert manager.store.get("owner/foo") is None


async def test_remove_of_a_tracked_repository_without_files(hass, manager):
    track(manager)

    await manager.remove_repository("owner/foo")

    assert manager.store.get("owner/foo") is None


async def test_download_errors_are_reported(aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", status=500)

    with pytest.raises(HomeAssistantError, match="Download failed"):
        await manager.install_or_update("owner/foo")


async def test_ref_resolution_errors_are_reported(aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", status=500)

    with pytest.raises(HomeAssistantError, match="Download failed"):
        await manager.install_or_update("owner/foo")


async def test_ref_resolution_rate_limit_is_reported(aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", status=429)

    with pytest.raises(HomeAssistantError, match="rate limit"):
        await manager.install_or_update("owner/foo")


async def test_get_manager_without_a_config_entry(hass):
    from custom_components.private_integrations.manager import get_manager

    with pytest.raises(HomeAssistantError, match="not configured"):
        get_manager(hass)


async def test_update_tracking_rejects_an_unknown_ref_type(manager):
    track(manager)

    with pytest.raises(HomeAssistantError, match="Invalid ref_type"):
        await manager.update_tracking("owner/foo", "banana", "main")


async def test_remove_refuses_a_folder_without_a_marker(hass, manager):
    dest = manager._custom_components_dir / "foo"
    dest.mkdir(parents=True)
    (dest / "manifest.json").write_text('{"domain": "foo"}')
    track(manager, integration_name="foo", installed_sha=SHA, installed_version="main")

    with pytest.raises(HomeAssistantError, match="was not installed by Private"):
        await manager.remove_repository("owner/foo")

    assert dest.is_dir()
    assert manager.store.get("owner/foo") is not None


async def test_download_rate_limit_is_reported(aioclient_mock, manager):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(
        f"{REPO_URL}/tarball/{SHA}", status=403, headers={"Retry-After": "9"}
    )

    with pytest.raises(HomeAssistantError, match="retry in 9 seconds"):
        await manager.install_or_update("owner/foo")


# --- Tracking configuration is validated before it is applied ---------------


async def test_a_failed_ref_change_leaves_the_tracking_untouched(
    aioclient_mock, manager
):
    repo = track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/nope", status=422)

    with pytest.raises(HomeAssistantError, match="has no ref named 'nope'"):
        await manager.update_tracking("owner/foo", "tag", "nope")

    # The object in the store is the live one: a half-applied ref_type would be
    # shown by the UI and used by the next install.
    assert repo.ref_type == "branch"
    assert repo.ref == "main"


async def test_an_invalid_ref_type_does_not_change_the_tracking(manager):
    repo = track(manager)

    with pytest.raises(HomeAssistantError, match="Invalid ref_type"):
        await manager.update_tracking("owner/foo", "banana", "main")

    assert repo.ref_type == "branch"
    assert repo.ref == "main"


async def test_update_tracking_rejects_a_ref_that_does_not_exist(
    aioclient_mock, manager
):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/v9.9.9", status=422)

    with pytest.raises(HomeAssistantError, match="has no ref named 'v9.9.9'"):
        await manager.update_tracking("owner/foo", "tag", "v9.9.9")


async def test_update_tracking_accepts_a_ref_that_exists(aioclient_mock, manager):
    repo = track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/v2.0.0", json={"sha": SHA})

    await manager.update_tracking("owner/foo", "tag", "v2.0.0")

    assert repo.ref_type == "tag"
    assert repo.ref == "v2.0.0"


async def test_add_repository_rejects_a_ref_that_does_not_exist(
    aioclient_mock, manager
):
    aioclient_mock.get(REPO_URL, json={"default_branch": "main"})
    aioclient_mock.get(f"{REPO_URL}/commits/main", status=422)

    with pytest.raises(HomeAssistantError, match="has no ref named 'main'"):
        await manager.add_repository("owner/foo", ref_type="branch", ref="main")

    assert manager.store.get("owner/foo") is None


async def test_update_tracking_reports_a_rate_limit_while_checking_the_ref(
    aioclient_mock, manager
):
    track(manager)
    aioclient_mock.get(
        f"{REPO_URL}/commits/v2.0.0", status=403, headers={"Retry-After": "30"}
    )

    with pytest.raises(HomeAssistantError, match="retry in 30 seconds"):
        await manager.update_tracking("owner/foo", "tag", "v2.0.0")


async def test_a_broken_github_answer_while_checking_the_ref_is_reported(
    aioclient_mock, manager
):
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/v2.0.0", status=500)

    with pytest.raises(HomeAssistantError, match="Cannot read owner/foo"):
        await manager.update_tracking("owner/foo", "tag", "v2.0.0")


# --- Changing the integration folder ---------------------------------------


async def test_update_tracking_can_set_the_integration_name(aioclient_mock, manager):
    repo = track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})

    await manager.update_tracking(
        "owner/foo", "branch", "main", integration_name="foo"
    )

    assert repo.integration_name == "foo"


async def test_update_tracking_keeps_the_folder_when_not_given(manager):
    repo = track(manager, integration_name="foo")

    await manager.update_tracking("owner/foo", "release", None)

    assert repo.integration_name == "foo"


async def test_update_tracking_can_clear_the_integration_name(
    aioclient_mock, manager
):
    repo = track(manager, integration_name="foo")
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})

    await manager.update_tracking(
        "owner/foo", "branch", "main", integration_name=None
    )

    assert repo.integration_name is None


async def test_update_tracking_refuses_to_orphan_installed_files(hass, manager):
    """Changing the folder must not leave marked files no UI could reach."""
    dest = manager._custom_components_dir / "foo"
    dest.mkdir(parents=True)
    (dest / ".private_integrations.json").write_text(
        json.dumps({"full_name": "owner/foo"})
    )
    repo = track(
        manager,
        integration_name="foo",
        installed_version="main",
        installed_sha=SHA,
    )

    with pytest.raises(HomeAssistantError, match="would leave it behind"):
        await manager.update_tracking(
            "owner/foo", "branch", "main", integration_name="bar"
        )

    assert repo.integration_name == "foo"
    assert repo.installed_sha == SHA
    assert dest.is_dir()


async def test_update_tracking_clears_the_old_install_with_the_folder(
    aioclient_mock, manager
):
    repo = track(
        manager,
        integration_name="foo",
        installed_version="main",
        installed_sha=SHA,
    )
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})

    await manager.update_tracking(
        "owner/foo", "branch", "main", integration_name="bar"
    )

    assert repo.integration_name == "bar"
    assert repo.installed_version is None
    assert repo.installed_sha is None


async def test_update_tracking_treats_a_blank_folder_as_no_folder(
    aioclient_mock, manager
):
    repo = track(manager, integration_name="foo")
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})

    await manager.update_tracking(
        "owner/foo", "branch", "main", integration_name="   "
    )

    assert repo.integration_name is None


async def test_update_tracking_rejects_an_invalid_integration_name(manager):
    repo = track(manager)

    with pytest.raises(HomeAssistantError, match="folder name"):
        await manager.update_tracking(
            "owner/foo", "branch", "main", integration_name="my-int"
        )

    assert repo.integration_name is None


async def test_update_tracking_rejects_a_folder_of_another_repository(
    aioclient_mock, manager
):
    track(manager, integration_name="foo")
    other = track(manager, full_name="other/repo")
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})

    with pytest.raises(HomeAssistantError, match="already managed by owner/foo"):
        await manager.update_tracking(
            "other/repo", "branch", "main", integration_name="foo"
        )

    assert other.integration_name is None


async def test_a_repository_without_integrations_says_so(aioclient_mock, manager):
    """Advice for the wrong case ("set integration_name") was confusing."""
    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(
        f"{REPO_URL}/tarball/{SHA}", content=make_tarball_without_integrations()
    )

    with pytest.raises(HomeAssistantError, match="nothing to install"):
        await manager.install_or_update("owner/foo")


async def test_a_disk_error_while_writing_is_reported_as_an_install_failure(
    aioclient_mock, manager, monkeypatch
):
    """A full or read-only disk must not escape as a raw OSError."""
    from custom_components.private_integrations import manager as manager_module

    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())

    def no_space(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(manager_module, "install_from_tarball", no_space)

    with pytest.raises(HomeAssistantError, match="Install failed"):
        await manager.install_or_update("owner/foo")


async def test_removing_an_install_dismisses_its_restart_reminder(
    hass, aioclient_mock, manager
):
    from homeassistant.components.persistent_notification import (
        _async_get_or_create_notifications,
    )

    from custom_components.private_integrations.manager import (
        pending_restart_notification_id,
    )

    track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())
    await manager.install_or_update("owner/foo")
    notification_id = pending_restart_notification_id("foo")
    assert notification_id in _async_get_or_create_notifications(hass)

    await manager.remove_repository("owner/foo")

    assert notification_id not in _async_get_or_create_notifications(hass)


async def test_an_unchanged_install_updates_the_version_label(aioclient_mock, manager):
    """Same commit, different ref: the label must describe what is on disk."""
    repo = track(manager)
    aioclient_mock.get(f"{REPO_URL}/commits/main", json={"sha": SHA})
    aioclient_mock.get(f"{REPO_URL}/tarball/{SHA}", content=make_tarball())
    await manager.install_or_update("owner/foo")
    assert repo.installed_version == "main"

    # The tag points at the commit that is already installed.
    aioclient_mock.get(f"{REPO_URL}/commits/v1.0", json={"sha": SHA})
    await manager.update_tracking("owner/foo", "tag", "v1.0")
    await manager.install_or_update("owner/foo")

    assert repo.installed_version == "v1.0"
    assert repo.installed_sha == SHA
