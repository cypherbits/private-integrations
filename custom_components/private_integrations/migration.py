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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .const import OLD_DOMAIN, STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

#: Name of the old repository registry below ``.storage/``.
OLD_STORE_KEY = f"{OLD_DOMAIN}.repositories"

#: Marker written by this integration, and the old one it replaces.
NEW_MARKER = ".private_integrations.json"
_OLD_MARKER_GLOB = ".private*hacs*.json"

#: Same rule the installer applies to integration folder names.
_VALID_INTEGRATION = re.compile(r"^[a-z0-9_]+$")


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
    """
    config_dir = Path(config_dir)
    actions: list[str] = []
    imported: list[dict[str, Any]] = []
    try:
        repositories, imported = _migrate_store(config_dir, actions)
        _migrate_markers(config_dir, repositories, actions)
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


def _migrate_store(
    config_dir: Path, actions: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Copy the old registry to the new key, unless one already exists.

    Returns the entries used to migrate markers and the entries that were
    imported (empty when the new registry was already authoritative).
    """
    storage = config_dir / ".storage"
    old_path = storage / OLD_STORE_KEY
    new_path = storage / STORAGE_KEY

    new_envelope = _read_object(new_path) if new_path.is_file() else None
    new_repositories = _repositories(new_envelope)
    if new_repositories:
        # The new registry is authoritative; do not merge two sets of entries.
        return new_repositories, []
    if new_path.exists() and new_envelope is None:
        # A malformed registry is the user's to fix; never overwrite it.
        return new_repositories, []
    if not old_path.is_file():
        return new_repositories, []

    old_envelope = _read_object(old_path)
    if old_envelope is None:
        return new_repositories, []
    old_repositories = _repositories(old_envelope)
    if not old_repositories:
        return new_repositories, []

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
    return old_repositories, old_repositories


def _migrate_markers(
    config_dir: Path,
    repositories: list[dict[str, Any]],
    actions: list[str],
) -> None:
    """Rename (or create) the ownership marker of every tracked folder."""
    root = (config_dir / "custom_components").resolve()
    for repo in repositories:
        integration_name = repo.get("integration_name")
        full_name = repo.get("full_name")
        if not isinstance(integration_name, str) or not _VALID_INTEGRATION.match(
            integration_name
        ):
            _LOGGER.warning(
                "Private Integrations: cannot locate the folder of %r; "
                "set its integration name and install it again",
                full_name,
            )
            continue
        if not isinstance(full_name, str) or not full_name:
            _LOGGER.warning(
                "Private Integrations: a repository in %s has no name",
                OLD_STORE_KEY,
            )
            continue

        folder = (root / integration_name).resolve()
        if not folder.is_relative_to(root) or not folder.is_dir():
            _LOGGER.warning(
                "Private Integrations: custom_components/%s was not found; "
                "%s will be tracked without files",
                integration_name,
                full_name,
            )
            continue

        new_marker = folder / NEW_MARKER
        if new_marker.is_file():
            continue

        old_marker = _find_old_marker(folder)
        if old_marker is not None:
            marker = _read_object(old_marker)
            owner = marker.get("full_name") if marker is not None else None
            if not isinstance(owner, str) or owner.lower() != full_name.lower():
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

        # No marker at all: adopt the folder only when its manifest proves that
        # it is the integration the old registry says it installed.
        manifest = _read_object(folder / "manifest.json")
        if manifest is None or manifest.get("domain") != integration_name:
            _LOGGER.warning(
                "Private Integrations: custom_components/%s has no ownership "
                "marker and its manifest.json does not match; not adopting it",
                integration_name,
            )
            continue
        marker = {
            "full_name": full_name,
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
