"""Tests for importing the state of a previous Private HACS installation."""
from __future__ import annotations

import json

from custom_components.private_integrations.migration import (
    NEW_MARKER,
    OLD_STORE_KEY,
    migrate_private_hacs,
)

SHA = "a" * 40


def _repo(**kwargs):
    data = {
        "full_name": "owner/foo",
        "ref_type": "branch",
        "ref": "main",
        "integration_name": "foo",
        "installed_version": "main",
        "installed_sha": SHA,
    }
    data.update(kwargs)
    return data


def _old_store(config_dir, repositories):
    storage = config_dir / ".storage"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / OLD_STORE_KEY).write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 1,
                "key": OLD_STORE_KEY,
                "data": {"repositories": repositories},
            }
        ),
        encoding="utf-8",
    )


def _folder(config_dir, integration="foo", domain=None):
    folder = config_dir / "custom_components" / integration
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(
        json.dumps({"domain": domain or integration}), encoding="utf-8"
    )
    return folder


def _new_store(config_dir):
    return config_dir / ".storage" / "private_integrations.repositories"


def test_migrates_the_registry_and_the_marker(tmp_path):
    _old_store(tmp_path, [_repo()])
    folder = _folder(tmp_path)
    old_marker = folder / ".private_hacs.json"
    marker = {"full_name": "owner/foo", "integration_name": "foo"}
    old_marker.write_text(json.dumps(marker), encoding="utf-8")

    result = migrate_private_hacs(tmp_path)

    envelope = json.loads(_new_store(tmp_path).read_text(encoding="utf-8"))
    assert envelope["key"] == "private_integrations.repositories"
    assert envelope["data"]["repositories"] == [_repo()]
    assert (tmp_path / ".storage" / OLD_STORE_KEY).is_file()
    assert not old_marker.exists()
    assert json.loads((folder / NEW_MARKER).read_text(encoding="utf-8")) == marker
    assert any("imported 1 repositories" in action for action in result.actions)
    assert any(
        "renamed the ownership marker" in action for action in result.actions
    )
    assert result.repositories == [_repo()]


def test_migration_is_idempotent(tmp_path):
    _old_store(tmp_path, [_repo()])
    folder = _folder(tmp_path)
    (folder / ".private_hacs.json").write_text(
        json.dumps({"full_name": "owner/foo"}), encoding="utf-8"
    )
    migrate_private_hacs(tmp_path)

    second = migrate_private_hacs(tmp_path)
    assert second.actions == []
    assert second.repositories == []
    assert (folder / NEW_MARKER).is_file()


def test_a_new_registry_is_never_overwritten(tmp_path):
    _old_store(tmp_path, [_repo(full_name="old/repo")])
    storage = tmp_path / ".storage"
    storage.mkdir(exist_ok=True)
    new = _new_store(tmp_path)
    new.write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 1,
                "key": "private_integrations.repositories",
                "data": {"repositories": [_repo(full_name="new/repo")]},
            }
        ),
        encoding="utf-8",
    )

    result = migrate_private_hacs(tmp_path)

    envelope = json.loads(new.read_text(encoding="utf-8"))
    assert envelope["data"]["repositories"][0]["full_name"] == "new/repo"
    assert not any("imported" in action for action in result.actions)
    assert result.repositories == []


def test_a_malformed_new_registry_is_left_alone(tmp_path):
    _old_store(tmp_path, [_repo()])
    storage = tmp_path / ".storage"
    storage.mkdir(exist_ok=True)
    new = _new_store(tmp_path)
    new.write_text("not json", encoding="utf-8")

    result = migrate_private_hacs(tmp_path)

    assert new.read_text(encoding="utf-8") == "not json"
    assert not any("imported" in action for action in result.actions)


def test_an_unreadable_old_registry_is_ignored(tmp_path):
    storage = tmp_path / ".storage"
    storage.mkdir(parents=True)
    (storage / OLD_STORE_KEY).write_text("not json", encoding="utf-8")

    result = migrate_private_hacs(tmp_path)
    assert result.actions == []
    assert result.repositories == []
    assert not _new_store(tmp_path).exists()


def test_an_empty_old_registry_is_ignored(tmp_path):
    _old_store(tmp_path, [])

    result = migrate_private_hacs(tmp_path)
    assert result.actions == []
    assert result.repositories == []
    assert not _new_store(tmp_path).exists()


def test_adopts_a_folder_without_a_marker(tmp_path):
    _old_store(tmp_path, [_repo()])
    _folder(tmp_path)

    result = migrate_private_hacs(tmp_path)

    marker = json.loads(
        (tmp_path / "custom_components" / "foo" / NEW_MARKER).read_text(
            encoding="utf-8"
        )
    )
    assert marker["full_name"] == "owner/foo"
    assert marker["integration_name"] == "foo"
    assert marker["installed_sha"] == SHA
    assert any(
        "adopted custom_components/foo" in action for action in result.actions
    )


def test_does_not_adopt_a_folder_with_another_domain(tmp_path):
    _old_store(tmp_path, [_repo()])
    _folder(tmp_path, domain="other")

    _result = migrate_private_hacs(tmp_path)

    assert not (tmp_path / "custom_components" / "foo" / NEW_MARKER).exists()


def test_does_not_rename_a_marker_of_another_repository(tmp_path):
    _old_store(tmp_path, [_repo()])
    folder = _folder(tmp_path)
    old_marker = folder / ".private_hacs.json"
    old_marker.write_text(
        json.dumps({"full_name": "other/repo"}), encoding="utf-8"
    )

    _result = migrate_private_hacs(tmp_path)

    assert old_marker.is_file()
    assert not (folder / NEW_MARKER).exists()


def test_an_unreadable_old_marker_is_left_alone(tmp_path):
    _old_store(tmp_path, [_repo()])
    folder = _folder(tmp_path)
    old_marker = folder / ".private_hacs.json"
    old_marker.write_text("not json", encoding="utf-8")

    _result = migrate_private_hacs(tmp_path)

    assert old_marker.is_file()
    assert not (folder / NEW_MARKER).exists()


def test_missing_folders_and_invalid_names_are_skipped(tmp_path):
    _old_store(
        tmp_path,
        [
            _repo(integration_name="../outside"),
            _repo(full_name="", integration_name="bar"),
            _repo(integration_name="missing"),
        ],
    )

    result = migrate_private_hacs(tmp_path)

    assert result.actions == [
        "imported 3 repositories from private_hacs.repositories"
    ]
    assert not (tmp_path.parent / "outside").exists()


def test_no_old_installation_is_a_noop(tmp_path):
    result = migrate_private_hacs(tmp_path)
    assert result.actions == []
    assert result.repositories == []
    assert not _new_store(tmp_path).exists()
