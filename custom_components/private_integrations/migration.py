"""Import the state left by the previous ``private_hacs`` installation.

The integration used to be called ``private_hacs``. Renaming the domain also
renamed its storage key and the marker written inside every installed
integration folder, so a plain upgrade would leave the tracked repositories
unreachable and turn the installed folders into foreign ones.

This module copies the repository registry and renames those markers. It only
runs while the new integration is being set up, never deletes anything, and
keeps the old registry as a backup.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .const import OLD_DOMAIN, STORAGE_KEY, STORAGE_VERSION
from .installer import PROTECTED_INTEGRATIONS
from .names import is_valid_integration_name
from .refs import parse_repo_reference

_LOGGER = logging.getLogger(__name__)

#: Name of the old repository registry below ``.storage/``.
OLD_STORE_KEY = f"{OLD_DOMAIN}.repositories"

#: Marker written by this integration, and the old one it replaces.
NEW_MARKER = ".private_integrations.json"
_OLD_MARKER_GLOB = ".private*hacs*.json"


@dataclass(frozen=True)
class MigrationResult:
    """What the import did, for the caller to log and load into the store."""

    actions: list[str]
    repositories: list[dict[str, Any]]


def migrate_private_hacs(config_dir: Path) -> MigrationResult:
    """Import the old registry and markers, returning a log summary.

    A missing or unreadable old file is not an error: a new installation simply
    has nothing to import. ``repositories`` contains the entries copied from the
    old registry, so the caller can load them into the store through Home
    Assistant's public API (the storage cache may not know about the new file
    yet).

    A marker is renamed for any tracked folder that carries the previous name.
    An unmarked folder is adopted only while the old registry is being imported,
    and only when that registry records the same repository and integration
    folder as an installed one. After the import, an unmarked folder belongs to
    no tracked repository here: another tool may have installed it.
    """
    config_dir = Path(config_dir)
    actions: list[str] = []
    imported: list[dict[str, Any]] = []
    try:
        repositories, imported, adoptable = _migrate_store(config_dir, actions)
        _migrate_markers(
            config_dir, repositories, actions, adoptable=adoptable
        )
    except OSError as err:
        _LOGGER.warning(
            "Private Integrations: could not import a previous Private HACS "
            "installation: %s",
            err,
        )
    return MigrationResult(actions, imported)


def _read_object(path: Path) -> dict[str, Any] | None:
    """Read a JSON object, reporting anything else as unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        _LOGGER.warning("Private Integrations: cannot read %s: %s", path, err)
        return None
    if not isinstance(data, dict):
        _LOGGER.warning(
            "Private Integrations: %s does not contain a JSON object", path
        )
        return None
    return data


def _repositories(envelope: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the valid entries of a registry envelope."""
    data = envelope.get("data") if envelope is not None else None
    repositories = data.get("repositories") if isinstance(data, dict) else None
    if not isinstance(repositories, list):
        return []
    return [repo for repo in repositories if isinstance(repo, dict)]


def _normalise_entry(repo: dict[str, Any]) -> dict[str, Any]:
    """Normalise the fields this integration stores in a fixed shape.

    Older registries may have a full URL or surrounding whitespace where the
    current store expects ``owner/repo`` and a bare folder name. The store
    rejects those entries, so normalise them while importing instead of
    silently losing the repository.
    """
    full_name = repo.get("full_name")
    if isinstance(full_name, str):
        try:
            normalised = parse_repo_reference(full_name)
        except ValueError:
            pass
        else:
            if normalised != full_name:
                repo = {**repo, "full_name": normalised}

    integration_name = repo.get("integration_name")
    if isinstance(integration_name, str):
        normalised_name = integration_name.strip() or None
        if normalised_name != integration_name:
            repo = {**repo, "integration_name": normalised_name}
    return repo


def _migrate_store(
    config_dir: Path, actions: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, set[str]]]:
    """Copy the old registry to the new key, unless one already exists.

    Returns the entries used to migrate markers, the entries that were imported
    (empty when the new registry was already authoritative) and the integration
    folders the old registry is allowed to adopt. The last one is only filled
    while the import happens: once the new registry exists, an unmarked folder
    belongs to no one here and must stay foreign.
    """
    storage = config_dir / ".storage"
    old_path = storage / OLD_STORE_KEY
    new_path = storage / STORAGE_KEY

    new_envelope = _read_object(new_path) if new_path.is_file() else None
    if new_path.exists():
        # The new registry is authoritative even when it is empty: its absence
        # is what marks the import as pending. Re-importing an empty registry
        # would resurrect repositories the user removed. Adoption is disabled
        # for the same reason: an unmarked folder found after the import may be
        # a replacement installed by another tool.
        return _repositories(new_envelope), [], {}

    old_repositories = [
        _normalise_entry(repo) for repo in _old_repositories(old_path)
    ]
    adoptable = _repository_names(old_repositories)
    if not old_repositories:
        return [], [], adoptable

    envelope = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"repositories": old_repositories},
    }
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    actions.append(
        f"imported {len(old_repositories)} repositories from {OLD_STORE_KEY}"
    )
    return old_repositories, old_repositories, adoptable


def _old_repositories(path: Path) -> list[dict[str, Any]]:
    """Return the old registry's entries, or an empty list when it is absent."""
    if not path.is_file():
        return []
    return _repositories(_read_object(path))


def _repository_names(
    repositories: list[dict[str, Any]],
) -> dict[str, set[str]]:
    """Integration folders each repository in the old registry may own."""
    names: dict[str, set[str]] = {}
    for repo in repositories:
        full_name = repo.get("full_name")
        integration_name = repo.get("integration_name")
        if not isinstance(full_name, str) or not full_name:
            continue
        if not is_valid_integration_name(integration_name):
            continue
        try:
            normalised = parse_repo_reference(full_name)
        except ValueError:
            continue
        names.setdefault(normalised.lower(), set()).add(integration_name)
    return names


def _was_installed(repo: dict[str, Any]) -> bool:
    """True when the old registry records an actual installation."""
    return any(
        isinstance(value, str) and bool(value)
        for value in (repo.get("installed_sha"), repo.get("installed_version"))
    )


def _marker_owner(path: Path) -> str | None:
    """Repository named by an old ownership marker, normalised."""
    marker = _read_object(path)
    owner = marker.get("full_name") if marker is not None else None
    if not isinstance(owner, str):
        return None
    try:
        return parse_repo_reference(owner)
    except ValueError:
        return None


def _migrate_markers(
    config_dir: Path,
    repositories: list[dict[str, Any]],
    actions: list[str],
    *,
    adoptable: dict[str, set[str]],
) -> None:
    """Rename markers, and adopt unmarked folders only from the old registry."""
    root = (config_dir / "custom_components").resolve()
    for repo in repositories:
        integration_name = repo.get("integration_name")
        full_name = repo.get("full_name")
        if not is_valid_integration_name(integration_name):
            _LOGGER.debug(
                "Private Integrations: cannot locate the folder of %r; "
                "set its integration name and install it again",
                full_name,
            )
            continue
        if integration_name in PROTECTED_INTEGRATIONS:
            _LOGGER.debug(
                "Private Integrations: not touching the protected integration "
                "'%s'",
                integration_name,
            )
            continue
        if not isinstance(full_name, str) or not full_name:
            _LOGGER.warning(
                "Private Integrations: a repository in %s has no name",
                OLD_STORE_KEY,
            )
            continue
        try:
            normalised_full_name = parse_repo_reference(full_name)
        except ValueError:
            _LOGGER.warning(
                "Private Integrations: %r is not 'owner/repo'; not looking "
                "for its folder",
                full_name,
            )
            continue

        folder = (root / integration_name).resolve()
        if not folder.is_relative_to(root) or not folder.is_dir():
            _LOGGER.debug(
                "Private Integrations: custom_components/%s was not found; "
                "%s is tracked without files",
                integration_name,
                full_name,
            )
            continue

        new_marker = folder / NEW_MARKER
        if new_marker.is_file():
            continue

        old_marker = _find_old_marker(folder)
        if old_marker is not None:
            owner = _marker_owner(old_marker)
            if owner is None or owner.lower() != normalised_full_name.lower():
                _LOGGER.warning(
                    "Private Integrations: %s does not belong to %s; "
                    "leaving it untouched",
                    old_marker,
                    full_name,
                )
                continue
            old_marker.rename(new_marker)
            actions.append(
                f"renamed the ownership marker of custom_components/{integration_name}"
            )
            continue

        if integration_name not in adoptable.get(
            normalised_full_name.lower(), set()
        ):
            # A repository added after the migration, or a folder the old
            # registry never recorded for this repository: it stays foreign.
            continue
        if not _was_installed(repo):
            # A repository tracked without files never installed this folder.
            continue

        # The old registry says this repository installed the folder, but the
        # old marker is gone. Adopt it only when the manifest proves it is the
        # same integration.
        manifest = _read_object(folder / "manifest.json")
        if manifest is None or manifest.get("domain") != integration_name:
            _LOGGER.warning(
                "Private Integrations: custom_components/%s has no ownership "
                "marker and its manifest.json does not match; not adopting it",
                integration_name,
            )
            continue
        marker = {
            "full_name": normalised_full_name,
            "integration_name": integration_name,
            "installed_version": repo.get("installed_version"),
            "installed_sha": repo.get("installed_sha"),
        }
        new_marker.write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        actions.append(
            f"adopted custom_components/{integration_name} for {full_name}"
        )


def _find_old_marker(folder: Path) -> Path | None:
    """Return the marker written by the previous name, if there is one."""
    for path in sorted(folder.glob(_OLD_MARKER_GLOB)):
        if path.is_file():
            return path
    return None
