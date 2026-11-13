"""Repository management — shared by services and the OptionsFlow."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    DOMAIN,
    REF_TYPE_BRANCH,
    REF_TYPE_COMMIT,
    REF_TYPE_RELEASE,
    VALID_REF_TYPES,
)
from .github import (
    GitHubClient,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from .installer import (
    InstallerError,
    install_from_tarball,
    installed_owner,
    list_integrations,
    remove_integration,
    validate_integration_name,
)
from .localize import async_translate
from .refs import parse_repo_reference
from .repository import Repository
from .store import RepositoryStore

_LOGGER = logging.getLogger(__name__)

class _Keep:
    """Type of the "leave it alone" sentinel used by update_tracking()."""


_KEEP = _Keep()


@dataclass(frozen=True)
class InstallResult:
    """Outcome of an install: the repository and whether files were written."""

    repository: Repository
    changed: bool


class PrivateIntegrationsManager:
    """Operations on the repository registry + custom_components/ directory."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: GitHubClient,
        store: RepositoryStore,
    ) -> None:
        self.hass = hass
        self.client = client
        self.store = store
        # One install or removal touches the filesystem at a time: two of them
        # interleaving would fight over the same staging and backup folders.
        self._lock = asyncio.Lock()

    @property
    def _custom_components_dir(self) -> Path:
        return Path(self.hass.config.path("custom_components"))

    @staticmethod
    def parse_repo_input(value: str) -> str:
        """Normalize user-supplied repo references to 'owner/repo'."""
        try:
            return parse_repo_reference(value)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    @staticmethod
    def _check_ref_type(ref_type: str) -> None:
        if ref_type not in VALID_REF_TYPES:
            raise HomeAssistantError(
                f"Invalid ref_type {ref_type!r}; expected one of {VALID_REF_TYPES}"
            )

    @classmethod
    def _normalise_ref(cls, ref_type: str, ref: str | None) -> str | None:
        """Validate a tracking configuration and return the normalised ref."""
        cls._check_ref_type(ref_type)
        if ref_type == REF_TYPE_RELEASE:
            return None
        cleaned = (ref or "").strip() or None
        if not cleaned:
            raise HomeAssistantError(
                f"A ref is required when ref_type is '{ref_type}'"
            )
        if ref_type == REF_TYPE_COMMIT and not re.fullmatch(
            r"[0-9a-fA-F]{7,40}", cleaned
        ):
            raise HomeAssistantError(
                f"{cleaned!r} is not a commit SHA (7-40 hex characters)"
            )
        return cleaned

    async def _resolve_tracking_ref(
        self,
        full_name: str,
        ref_type: str,
        ref: str | None,
        *,
        default_branch: str | None = None,
    ) -> str | None:
        """Normalise a tracking configuration and check the ref really exists.

        A typo in a tag or branch used to be stored happily and only surfaced on
        the next install, so the user was told the repository was added and the
        install failed right after. Resolving the ref here turns that into an
        immediate, actionable answer.

        Nothing is stored by this method: the caller applies the result once
        everything has been validated.
        """
        self._check_ref_type(ref_type)
        defaulted = ref_type == REF_TYPE_BRANCH and not (ref or "").strip()
        if defaulted:
            ref = default_branch or await self._default_branch(full_name)
        normalised = self._normalise_ref(ref_type, ref)
        if not defaulted:
            await self._assert_ref_exists(full_name, ref_type, normalised)
        return normalised

    async def _default_branch(self, full_name: str) -> str:
        try:
            info = await self.client.get_repo(full_name)
        except GitHubRateLimitError as err:
            raise HomeAssistantError(self._rate_limit_message(err)) from err
        except GitHubError as err:
            raise HomeAssistantError(
                f"Cannot read {full_name} from GitHub: {err}"
            ) from err
        return info.get("default_branch") or "main"

    async def _assert_ref_exists(
        self, full_name: str, ref_type: str, ref: str | None
    ) -> None:
        """Fail early when the ref the user typed is not on GitHub.

        Releases are not checked: a repository may publish its first release
        after being added, and the install reports the missing release then.
        """
        if ref is None or ref_type == REF_TYPE_RELEASE:
            return
        try:
            sha = await self.client.get_commit_sha(full_name, ref)
        except GitHubRateLimitError as err:
            raise HomeAssistantError(self._rate_limit_message(err)) from err
        except GitHubError as err:
            raise HomeAssistantError(
                f"Cannot read {full_name} from GitHub: {err}"
            ) from err
        if not sha:
            raise HomeAssistantError(
                f"{full_name} has no ref named {ref!r}. Check the spelling, or "
                "use a branch, tag or commit that exists in the repository"
            )

    async def add_repository(
        self,
        full_name_or_url: str,
        ref_type: str = REF_TYPE_BRANCH,
        ref: str | None = None,
        integration_name: str | None = None,
    ) -> Repository:
        full_name = self.parse_repo_input(full_name_or_url)

        integration_name = (integration_name or "").strip() or None
        if integration_name is not None:
            try:
                validate_integration_name(integration_name)
            except InstallerError as err:
                raise HomeAssistantError(str(err)) from err

        self._check_ref_type(ref_type)

        if self.store.get(full_name):
            raise HomeAssistantError(f"{full_name} is already tracked")

        self._assert_folder_is_free(full_name, integration_name)

        try:
            info = await self.client.get_repo(full_name)
        except GitHubNotFoundError as err:
            raise HomeAssistantError(
                f"Cannot access {full_name} on GitHub: repository not found "
                "or your token has no access to it. If you're using a "
                "fine-grained PAT, make sure this repository is in its "
                "'Repository access' list."
            ) from err
        except GitHubRateLimitError as err:
            raise HomeAssistantError(self._rate_limit_message(err)) from err
        except GitHubError as err:
            raise HomeAssistantError(
                f"Cannot access {full_name} on GitHub: {err}"
            ) from err

        resolved = await self._resolve_tracking_ref(
            full_name,
            ref_type,
            ref,
            default_branch=info.get("default_branch"),
        )

        repo = Repository(
            full_name=full_name,
            ref_type=ref_type,
            ref=resolved,
            integration_name=integration_name,
        )
        self.store.upsert(repo)
        await self.store.async_save()
        return repo

    async def remove_repository(
        self, full_name_or_url: str, *, delete_files: bool = True
    ) -> bool:
        """Stop tracking a repository; return True when its files were deleted."""
        full_name = self.parse_repo_input(full_name_or_url)

        async with self._lock:
            repo = self.store.get(full_name)
            if not repo:
                raise HomeAssistantError(f"{full_name} is not tracked")
            return await self._remove_repository(
                repo, full_name, delete_files=delete_files
            )

    async def _remove_repository(
        self, repo: Repository, full_name: str, *, delete_files: bool
    ) -> bool:
        removed = False
        if delete_files and repo.integration_name:
            dest_path = self._custom_components_dir / repo.integration_name
            owner = await self.hass.async_add_executor_job(
                installed_owner, dest_path
            )
            self._assert_owns_files(repo, dest_path, owner)
            try:
                removed = await self.hass.async_add_executor_job(
                    remove_integration,
                    self._custom_components_dir,
                    repo.integration_name,
                )
            except (InstallerError, OSError) as err:
                # Keep tracking: the user can fix the problem and retry, and the
                # files are still referenced by a repository that knows them.
                raise HomeAssistantError(
                    f"Could not finish deleting "
                    f"custom_components/{repo.integration_name}: {err}. The "
                    "repository is still tracked"
                ) from err
            if removed:
                # "Restart to load it" describes files that no longer exist.
                persistent_notification.async_dismiss(
                    self.hass,
                    pending_restart_notification_id(repo.integration_name),
                )

        self.store.remove(full_name)
        await self.store.async_save()
        return removed

    def _assert_owns_files(
        self, repo: Repository, dest_path: Path, owner: str | None
    ) -> None:
        """Refuse to delete files this repository did not install."""
        if not dest_path.exists():
            return
        if self._owns_folder(repo, owner):
            return
        if owner is not None:
            if self.store.get(owner) is None:
                raise HomeAssistantError(
                    f"custom_components/{dest_path.name} was installed by "
                    f"{owner}, which is no longer tracked. Install it again to "
                    "take the folder over, or delete it yourself"
                )
            raise HomeAssistantError(
                f"custom_components/{dest_path.name} is installed from {owner}, "
                f"not from {repo.full_name}; refusing to delete it. Remove that "
                "repository first, or stop tracking this one without deleting "
                "its files"
            )
        raise HomeAssistantError(
            f"custom_components/{dest_path.name} was not installed by Private "
            "Integrations; refusing to delete it. Remove it yourself, or stop "
            "tracking the repository without deleting its files"
        )

    async def update_tracking(
        self,
        full_name_or_url: str,
        ref_type: str,
        ref: str | None,
        *,
        integration_name: str | None | _Keep = _KEEP,
    ) -> Repository:
        """Change how a tracked repository is followed (does not reinstall).

        ``integration_name`` is only touched when it is passed: a repository
        whose folder was guessed wrong -- a repository with two integrations, an
        install that failed because of it -- could otherwise only be fixed by
        removing it and adding it again.
        """
        full_name = self.parse_repo_input(full_name_or_url)
        repo = self.store.get(full_name)
        if not repo:
            raise HomeAssistantError(f"{full_name} is not tracked")

        new_integration_name = None
        if not isinstance(integration_name, _Keep):
            new_integration_name = (integration_name or "").strip() or None
            if new_integration_name is not None:
                try:
                    validate_integration_name(new_integration_name)
                except InstallerError as err:
                    raise HomeAssistantError(str(err)) from err
                self._assert_folder_is_free(full_name, new_integration_name)

        if (
            not isinstance(integration_name, _Keep)
            and new_integration_name != repo.integration_name
        ):
            await self._assert_folder_change_is_safe(repo, new_integration_name)

        # Validate first, assign afterwards. The object returned by the store is
        # the live one: assigning ref_type before validating left the entry with
        # a half-applied configuration when the validation failed, visible in the
        # UI and used by the next install until Home Assistant restarted.
        resolved = await self._resolve_tracking_ref(full_name, ref_type, ref)

        repo.ref_type = ref_type
        repo.ref = resolved
        if not isinstance(integration_name, _Keep):
            if new_integration_name != repo.integration_name:
                # The files on disk belong to the old folder, so the next
                # install must not be skipped as "already installed".
                repo.installed_version = None
                repo.installed_sha = None
            repo.integration_name = new_integration_name
        self.store.upsert(repo)
        await self.store.async_save()
        return repo

    async def install_or_update(
        self, full_name_or_url: str, *, force: bool = False
    ) -> InstallResult:
        full_name = self.parse_repo_input(full_name_or_url)

        async with self._lock:
            # Read inside the lock: a removal may have finished while this
            # install was queued, and installing a stale snapshot would quietly
            # re-register the repository that was just removed.
            repo = self.store.get(full_name)
            if not repo:
                raise HomeAssistantError(f"{full_name} is not tracked")
            return await self._install_or_update(repo, force=force)

    async def _install_or_update(
        self, repo: Repository, *, force: bool
    ) -> InstallResult:
        full_name = repo.full_name
        try:
            sha, version = await self._resolve_version(repo)
        except GitHubRateLimitError as err:
            raise HomeAssistantError(self._rate_limit_message(err)) from err
        except HomeAssistantError:
            raise
        except GitHubError as err:
            raise HomeAssistantError(f"Download failed: {err}") from err

        if not force and await self._is_installed_at(repo, sha):
            _LOGGER.debug(
                "%s is already installed at %s, nothing to do", full_name, sha[:7]
            )
            if repo.installed_version != version:
                # Same commit under a new name (a tag pointing at the branch
                # head, say). The files are already right, but the label the
                # manage screen shows would still name the previous ref.
                repo.installed_version = version
                self.store.upsert(repo)
                await self.store.async_save()
            return InstallResult(repo, changed=False)

        try:
            tarball = await self.client.download_tarball(full_name, sha)
        except GitHubRateLimitError as err:
            raise HomeAssistantError(self._rate_limit_message(err)) from err
        except GitHubError as err:
            raise HomeAssistantError(f"Download failed: {err}") from err

        dest_root = self._custom_components_dir
        try:
            await self.hass.async_add_executor_job(_ensure_dir, dest_root)
        except OSError as err:
            raise HomeAssistantError(
                f"Install failed: cannot write to {dest_root}: {err}"
            ) from err

        try:
            names = await self.hass.async_add_executor_job(list_integrations, tarball)
        except (InstallerError, OSError) as err:
            raise HomeAssistantError(f"Install failed: {err}") from err

        integration_name = self._select_integration(repo, names)
        integration_name, dest_path = await self._install(
            repo,
            tarball,
            dest_root,
            integration_name,
            marker={
                "full_name": repo.full_name,
                "integration_name": integration_name,
                "installed_version": version,
                "installed_sha": sha,
            },
        )

        repo.integration_name = integration_name
        repo.installed_sha = sha
        repo.installed_version = version
        self.store.upsert(repo)
        await self.store.async_save()

        _LOGGER.info(
            "Installed %s into custom_components/%s — restart HA to load",
            full_name,
            integration_name,
        )

        version_label = repo.installed_version or (
            repo.installed_sha[:7] if repo.installed_sha else "?"
        )
        persistent_notification.async_create(
            self.hass,
            await async_translate(
                self.hass,
                "install_notification",
                repo=repo.full_name,
                integration=integration_name,
                version=version_label,
            ),
            title=await async_translate(
                self.hass,
                "install_notification_title",
                integration=integration_name,
            ),
            notification_id=pending_restart_notification_id(integration_name),
        )

        return InstallResult(repo, changed=True)

    async def _is_installed_at(self, repo: Repository, sha: str) -> bool:
        """True when the recorded version is on disk and matches ``sha``."""
        if not repo.integration_name or repo.installed_sha != sha:
            return False
        dest_path = self._custom_components_dir / repo.integration_name
        if not await self.hass.async_add_executor_job(dest_path.exists):
            return False
        owner = await self.hass.async_add_executor_job(installed_owner, dest_path)
        return self._owns_folder(repo, owner)

    @staticmethod
    def _owns_folder(repo: Repository, owner: str | None) -> bool:
        """True when the folder's ownership marker names this repository.

        Only the marker counts. A folder without one was not created by Private
        Integrations (or was replaced by another tool), so it is never touched,
        even when the entry records a previous install of this repository.
        """
        return owner is not None and owner.lower() == repo.full_name.lower()

    def _select_integration(self, repo: Repository, names: list[str]) -> str:
        """Pick the integration to install, before anything is written to disk."""
        if repo.integration_name:
            if repo.integration_name not in names:
                raise HomeAssistantError(
                    f"Integration '{repo.integration_name}' not found in "
                    f"{repo.full_name} (found: {names})"
                )
            return repo.integration_name
        if not names:
            # "There is no integration_name to set here" is what the user needs
            # to read: the advice for the many-integrations case would be wrong.
            raise HomeAssistantError(
                f"{repo.full_name} has no custom_components/<integration>/ "
                "folder, so there is nothing to install. Check that the "
                "integration is at the root of the repository"
            )
        if len(names) > 1:
            raise HomeAssistantError(
                f"{repo.full_name} has {len(names)} integrations: {names}. Set "
                "'Integration folder name' under Change tracking ref, then "
                "install again"
            )
        return names[0]

    async def _install(
        self,
        repo: Repository,
        tarball: bytes,
        dest_root: Path,
        integration_name: str,
        *,
        marker: dict[str, Any] | None = None,
    ) -> tuple[str, Path]:
        """Install into custom_components/, refusing to clobber foreign files."""
        dest_path = dest_root / integration_name
        allow_replace = await self._may_replace(repo, dest_path)
        try:
            return await self.hass.async_add_executor_job(
                partial(
                    install_from_tarball,
                    tarball,
                    dest_root,
                    integration_name,
                    allow_replace=allow_replace,
                    marker=marker,
                )
            )
        except (InstallerError, OSError) as err:
            # OSError is a full or read-only disk, a broken mount, a path that
            # stopped being writable: all of them become "Install failed"
            # instead of escaping the service call or the options flow.
            raise HomeAssistantError(f"Install failed: {err}") from err

    async def _may_replace(self, repo: Repository, dest_path: Path) -> bool:
        """Allow replacing a folder only when *this* repository owns it."""
        if not dest_path.exists():
            return True
        owner = await self.hass.async_add_executor_job(installed_owner, dest_path)
        if self._owns_folder(repo, owner):
            return True
        if owner is not None:
            if self.store.get(owner) is None:
                # The repository that installed this folder is gone: adopting it
                # is the only way out that does not need a shell on the host.
                _LOGGER.warning(
                    "Taking over custom_components/%s from %s, which is no "
                    "longer tracked",
                    dest_path.name,
                    owner,
                )
                return True
            raise HomeAssistantError(
                f"custom_components/{dest_path.name} is installed from {owner}, "
                f"not from {repo.full_name}. Two tracked repositories cannot "
                "share an integration folder: remove the other one first or "
                "pick a different integration_name."
            )
        raise HomeAssistantError(
            f"custom_components/{dest_path.name} already exists and was not "
            "installed by Private Integrations; refusing to overwrite it. Remove it "
            "first or pick a different integration_name."
        )

    def _assert_folder_is_free(
        self, full_name: str, integration_name: str | None
    ) -> None:
        """Reject a second repository claiming a folder another one manages."""
        if not integration_name:
            return
        for other in self.store.all():
            if other.full_name.lower() == full_name.lower():
                continue
            if other.integration_name == integration_name:
                raise HomeAssistantError(
                    f"custom_components/{integration_name} is already managed "
                    f"by {other.full_name}; a folder can only belong to one "
                    "tracked repository"
                )

    async def _assert_folder_change_is_safe(
        self, repo: Repository, new_integration_name: str | None
    ) -> None:
        """Refuse to change the folder while this repository still owns files there.

        Changing the folder does not move what is on disk. The old folder would
        stay behind, still marked as this repository's, and neither this nor any
        other repository could remove or take it over through the UI while this
        one is tracked.
        """
        old_name = repo.integration_name
        if old_name is None or old_name == new_integration_name:
            return
        dest_path = self._custom_components_dir / old_name
        owner = await self.hass.async_add_executor_job(
            installed_owner, dest_path
        )
        if self._owns_folder(repo, owner):
            raise HomeAssistantError(
                f"custom_components/{old_name} is installed from "
                f"{repo.full_name}. Changing the integration folder would "
                "leave it behind: remove the repository (with its files) and "
                "add it again, or delete that folder first."
            )

    async def _resolve_version(self, repo: Repository) -> tuple[str, str]:
        """Resolve the tracked ref to (commit SHA, version label).

        The SHA is what gets downloaded, so the files on disk always match the
        recorded ``installed_sha`` even if the branch moves during the install.
        """
        ref = repo.ref
        if repo.ref_type == REF_TYPE_RELEASE:
            release = await self.client.get_latest_release(repo.full_name)
            tag = release.get("tag_name") if release else None
            if not tag:
                raise HomeAssistantError(
                    f"{repo.full_name} has no published release to install"
                )
            ref = tag

        if not ref:
            raise HomeAssistantError(
                f"{repo.full_name} has no ref configured "
                f"(ref_type={repo.ref_type})"
            )

        sha = await self.client.get_commit_sha(repo.full_name, ref)
        if not sha:
            raise HomeAssistantError(
                f"Ref {ref!r} not found in {repo.full_name}"
            )
        version = ref[:7] if repo.ref_type == REF_TYPE_COMMIT else ref
        return sha, version

    @staticmethod
    def _rate_limit_message(err: GitHubRateLimitError) -> str:
        if err.retry_after is not None:
            return (
                "GitHub rate limit reached, retry in "
                f"{err.retry_after} seconds"
            )
        return "GitHub rate limit reached, try again later"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def pending_restart_notification_id(integration_name: str) -> str:
    """Id of the "restart Home Assistant" reminder for an integration."""
    return f"{DOMAIN}_pending_restart_{integration_name}"


def get_manager(hass: HomeAssistant) -> PrivateIntegrationsManager:
    """Return the manager for the (single) configured Private Integrations entry."""
    domain_data = hass.data.get(DOMAIN, {})
    if not domain_data:
        raise HomeAssistantError("Private Integrations is not configured")
    return next(iter(domain_data.values()))["manager"]
