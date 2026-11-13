"""Persistent storage for tracked repositories."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant import exceptions as ha_exceptions
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_VERSION
from .repository import Repository

_LOGGER = logging.getLogger(__name__)

class _NeverRaised(HomeAssistantError):
    """Stand-in for a storage error older Home Assistant versions do not have."""


#: Home Assistant 2026.10 onwards refuses a file written by a newer major version
#: itself, before any migration hook is called. Older versions have no such
#: error, so the lookup falls back to something that is never raised.
_NEWER_STORAGE_FILE: type[Exception] = getattr(
    ha_exceptions, "UnsupportedStorageVersionError", _NeverRaised
)


class _TolerantStore(Store[dict[str, Any]]):
    """A store that also reads a file written by another version.

    Home Assistant hands every major version mismatch to ``async_migrate_func``
    and, with the default implementation, raises ``NotImplementedError`` -- from
    inside ``async_setup_entry``, so the integration would not start (and the
    tracked repositories would be unreachable) until somebody edited the file by
    hand. The format has had a single shape so far, so a file from a newer build
    (a downgrade) or an older one is read as it is and flagged in the log.
    """

    # Home Assistant calls this hook; it is a private name in 2024.11 and 2025.1
    # and a public one in later releases, so both are defined and share the work.
    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        return self._accept_other_version(
            old_major_version, old_minor_version, old_data
        )

    async def async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        return self._accept_other_version(
            old_major_version, old_minor_version, old_data
        )

    def _accept_other_version(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        _LOGGER.warning(
            "%s was written by another version of Private Integrations (storage %s.%s, "
            "this build expects %s.%s); reading it as it is",
            STORAGE_KEY,
            old_major_version,
            old_minor_version,
            self.version,
            self.minor_version,
        )
        return old_data


class RepositoryStore:
    """Wraps HA's Store with a list[Repository] view."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = _TolerantStore(hass, STORAGE_VERSION, STORAGE_KEY)
        self._repos: dict[str, Repository] = {}
        self._loaded = False

    async def async_load(self) -> None:
        if self._loaded:
            return
        try:
            data: Any = await self._store.async_load()
        except _NEWER_STORAGE_FILE as err:
            # A downgrade: the file was written by a newer build, and this one
            # cannot promise to understand it. Refusing to start would leave the
            # user with a broken integration and no way out through the UI, so
            # the list starts empty and says why. Any change made from now on
            # rewrites the file, which is what the backup is for.
            _LOGGER.error(
                "Cannot read %s: %s. Starting with an empty list of repositories; "
                "restore the file from a backup, or run the version of Private "
                "Integrations that wrote it",
                STORAGE_KEY,
                err,
            )
            data = None

        if data is not None and not isinstance(data, dict):
            # The file parses, but it is not the object this code writes.
            _LOGGER.warning(
                "Ignoring %s: the stored data is a %s, not an object",
                STORAGE_KEY,
                type(data).__name__,
            )
            data = None
        repos = (data or {}).get("repositories", [])
        if not isinstance(repos, list):
            _LOGGER.warning(
                "Ignoring %s: 'repositories' is not a list", STORAGE_KEY
            )
            repos = []
        self._repos = {}
        for raw in repos:
            if not isinstance(raw, dict):
                _LOGGER.warning("Ignoring a malformed entry in %s", STORAGE_KEY)
                continue
            try:
                repo = Repository.from_dict(raw)
                key = repo.key
            except (AttributeError, TypeError, ValueError) as err:
                # Every value used to build the entry is validated here: a
                # broken one skips that repository with a warning instead of
                # failing setup, which is where this code runs.
                _LOGGER.warning(
                    "Ignoring an unreadable entry in %s: %s", STORAGE_KEY, err
                )
                continue
            self._repos[key] = repo
        self._loaded = True

    async def async_save(self) -> None:
        await self._store.async_save(
            {"repositories": [r.to_dict() for r in self._repos.values()]}
        )

    async def async_remove(self) -> None:
        """Delete the storage file, e.g. when the config entry goes away."""
        self._repos = {}
        self._loaded = False
        await self._store.async_remove()

    def all(self) -> list[Repository]:
        return list(self._repos.values())

    def get(self, full_name: str) -> Repository | None:
        return self._repos.get(full_name.lower())

    def upsert(self, repo: Repository) -> None:
        self._repos[repo.key] = repo

    def remove(self, full_name: str) -> bool:
        return self._repos.pop(full_name.lower(), None) is not None
