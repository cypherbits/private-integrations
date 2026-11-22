"""Look for custom integrations in the repositories a token can read.

The options flow offers what this module finds as proposals, so adding a
repository is picking from a list instead of retyping ``owner/repo``. Only
repositories the token itself can see are inspected: a fine-grained PAT that is
scoped to three repositories sees three, which is the point of asking GitHub
instead of keeping a catalog somewhere else.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from .github import GitHubClient, GitHubError, GitHubRateLimitError
from .installer import PROTECTED_INTEGRATIONS
from .names import is_valid_integration_name

_LOGGER = logging.getLogger(__name__)

CUSTOM_COMPONENTS = "custom_components"

#: Repositories inspected per run. Each one costs at least one API call, and a
#: token that belongs to an organisation can see thousands of them: the cap
#: keeps the lookup at a few seconds and inside the hourly rate limit.
MAX_REPOSITORIES = 60
#: ``manifest.json`` files read per run, to confirm what a folder is.
MAX_MANIFESTS = 80
#: Requests in flight at once. Enough to hide the latency, few enough to stay
#: polite with the API.
CONCURRENCY = 6


@dataclass(frozen=True)
class Candidate:
    """An integration found in a repository that is not tracked yet."""

    full_name: str
    integration_name: str
    version: str | None = None
    default_branch: str = "main"
    private: bool = False
    installed: bool = False
    shared: bool = False

    @property
    def key(self) -> str:
        """Selector value, unique even when a repository holds several."""
        return f"{self.full_name}::{self.integration_name}"


@dataclass(frozen=True)
class Discovery:
    """What one lookup found, plus how much of the account it covered."""

    candidates: list[Candidate] = field(default_factory=list)
    inspected: int = 0
    listed: int = 0
    truncated: bool = False
    manifests_truncated: bool = False
    rate_limited: bool = False
    unreadable: int = 0


class _Budget:
    """A countdown of manifest reads shared by the concurrent inspectors.

    ``asyncio`` runs one coroutine at a time between awaits, so a plain
    counter is enough: nothing can interleave inside ``take``.
    """

    def __init__(self, limit: int) -> None:
        self._left = limit
        self.exhausted = False

    def take(self) -> bool:
        if self._left <= 0:
            self.exhausted = True
            return False
        self._left -= 1
        return True


async def async_detect_integrations(
    client: GitHubClient,
    *,
    tracked: Collection[str] = (),
    installed: Collection[str] = (),
    max_repositories: int = MAX_REPOSITORIES,
) -> Discovery:
    """Return every integration offered by a private repository the token can read.

    Repositories that are already tracked are skipped: they are on the manage
    screen, and offering them again would only be a way to get an error.
    ``installed`` is the list of folders already in ``custom_components/``, used
    to say which candidates are already on disk.

    Public repositories are not offered at all: the Marketplace handles them.
    The candidates are ordered with the token account's private repositories
    first -- installed ones before the rest -- and repositories shared with the
    account after them.

    A single unreadable repository does not fail the lookup -- a token usually
    sees unrelated repositories too -- but a rate limit stops it, and is
    reported so the UI can ask the user to retry instead of showing an empty
    list that looks like "you have no integrations".
    """
    repositories = await client.async_list_repositories()
    login = (await client.async_get_login()).lower()
    tracked_keys = {name.lower() for name in tracked}
    installed_names = set(installed)

    inspectable = [
        repository
        for repository in repositories
        if _is_inspectable(repository, tracked_keys)
    ]
    truncated = len(inspectable) > max_repositories
    inspectable = inspectable[:max_repositories]

    budget = _Budget(MAX_MANIFESTS)
    semaphore = asyncio.Semaphore(CONCURRENCY)
    candidates: list[Candidate] = []
    rate_limited = False
    unreadable = 0

    async def inspect(repository: dict[str, Any]) -> None:
        nonlocal rate_limited, unreadable
        full_name = repository["full_name"]
        branch = repository.get("default_branch") or "main"

        async with semaphore:
            try:
                entries = await client.async_list_folder(
                    full_name, CUSTOM_COMPONENTS, branch
                )
            except GitHubRateLimitError:
                rate_limited = True
                return
            except GitHubError as err:
                unreadable += 1
                _LOGGER.debug("Skipping %s during discovery: %s", full_name, err)
                return

            for entry in entries:
                name = entry.get("name")
                if entry.get("type") != "dir" or not isinstance(name, str):
                    continue
                if (
                    not is_valid_integration_name(name)
                    or name in PROTECTED_INTEGRATIONS
                    or not budget.take()
                ):
                    continue
                try:
                    text = await client.async_get_file_text(
                        full_name, f"{CUSTOM_COMPONENTS}/{name}/manifest.json", branch
                    )
                except GitHubRateLimitError:
                    rate_limited = True
                    return
                except GitHubError as err:
                    _LOGGER.debug(
                        "Skipping %s/%s during discovery: %s", full_name, name, err
                    )
                    continue
                manifest = _parse_manifest(text)
                if manifest is None or manifest.get("domain") != name:
                    continue
                candidates.append(
                    Candidate(
                        full_name=full_name,
                        integration_name=name,
                        version=_text(manifest.get("version")),
                        default_branch=branch,
                        private=True,
                        installed=name in installed_names,
                        shared=_is_shared(repository, login),
                    )
                )

    await asyncio.gather(*(inspect(repository) for repository in inspectable))

    if rate_limited and not candidates:
        # A partial list is still useful; an empty one caused by the rate limit
        # is not, and would read as "you have no integrations".
        raise GitHubRateLimitError(
            "GitHub rate limit reached before any integration was found"
        )

    candidates.sort(
        key=lambda c: (
            c.shared,
            not c.installed,
            c.full_name.lower(),
            c.integration_name,
        )
    )
    return Discovery(
        candidates=candidates,
        inspected=len(inspectable),
        listed=len(repositories),
        truncated=truncated,
        manifests_truncated=budget.exhausted,
        rate_limited=rate_limited,
        unreadable=unreadable,
    )


def _is_inspectable(repository: Any, tracked: set[str]) -> bool:
    """True for a private repository worth one API call."""
    if not isinstance(repository, dict):
        return False
    full_name = repository.get("full_name")
    if not isinstance(full_name, str) or full_name.count("/") != 1:
        return False
    if full_name.lower() in tracked:
        return False
    if repository.get("private") is not True:
        # Public repositories belong to the Marketplace.
        return False
    return not repository.get("archived")


def _is_shared(repository: dict[str, Any], login: str) -> bool:
    """True when the repository belongs to another account or organisation.

    A missing login (GitHub did not say who the token belongs to) keeps every
    candidate in the first group, which is the least surprising fallback.
    """
    if not login:
        return False
    owner = repository.get("owner")
    owner_login = owner.get("login") if isinstance(owner, dict) else None
    return not (
        isinstance(owner_login, str) and owner_login.lower() == login
    )


def _parse_manifest(text: str | None) -> dict[str, Any] | None:
    """Read a manifest, treating anything that is not a JSON object as absent."""
    if not text:
        return None
    try:
        manifest = json.loads(text)
    except ValueError:
        return None
    return manifest if isinstance(manifest, dict) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
