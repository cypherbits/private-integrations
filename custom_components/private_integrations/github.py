"""GitHub API client for Private Integrations."""
from __future__ import annotations

import base64
import logging
import time
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import GITHUB_API_BASE, USER_AGENT

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)
TARBALL_TIMEOUT = aiohttp.ClientTimeout(total=300, connect=10)
#: Hard cap so a wrong/unexpected ref cannot exhaust Home Assistant memory.
MAX_TARBALL_BYTES = 64 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 64 * 1024

#: Repositories read by one discovery run: three pages of a hundred. A token
#: can see thousands; the caller only offers a short list anyway.
MAX_LISTED_REPOSITORIES = 300
LIST_PAGE_SIZE = 100


class GitHubError(Exception):
    """Raised on any GitHub API failure."""


class GitHubAuthError(GitHubError):
    """Raised on 401/403 from GitHub (bad token or missing permission)."""


class GitHubNotFoundError(GitHubError):
    """Raised on 404 from GitHub."""


class GitHubUnprocessableError(GitHubError):
    """Raised when GitHub cannot resolve the request (409/422).

    The commits endpoint answers 422 ("No commit found for SHA") for a ref it
    does not know and 409 for an empty repository; both mean "no such commit".
    """


class GitHubRateLimitError(GitHubError):
    """Raised when GitHub refuses a request because of a rate limit."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _as_dict(payload: Any, request: str) -> dict[str, Any]:
    """Reject a payload that is not a JSON object.

    GitHub answers with objects for everything this client reads, but a proxy, a
    captive portal or a broken redirect can return anything at all. Returning the
    payload unchecked moved the failure to the caller, which crashed on ``.get``
    instead of reporting a GitHub error -- and the config flow turned that crash
    into "Unknown error".
    """
    if not isinstance(payload, dict):
        raise GitHubError(
            f"Unexpected answer from GitHub for {request}: expected a JSON object"
        )
    return payload


class GitHubClient:
    """Thin authenticated wrapper around the GitHub REST API."""

    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        self._session = session
        self._token = token
        #: Login of the account behind the token, when it is already known.
        self._login: str | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }

    @staticmethod
    def _retry_after(resp: aiohttp.ClientResponse) -> int | None:
        """Seconds to wait when the response is a rate-limit rejection."""
        header = resp.headers.get("Retry-After")
        if header and header.isdigit():
            return int(header)
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = resp.headers.get("X-RateLimit-Reset")
            if reset and reset.isdigit():
                return max(0, int(reset) - int(time.time()))
        return None

    def _raise_for_status(self, resp: aiohttp.ClientResponse, url: str) -> None:
        if resp.status < 400:
            return
        if resp.status == 404:
            raise GitHubNotFoundError(f"Not found: {url}")
        if resp.status in (403, 429):
            retry_after = self._retry_after(resp)
            if retry_after is not None or resp.status == 429:
                raise GitHubRateLimitError(
                    f"GitHub rate limit reached for {url}", retry_after
                )
        if resp.status in (401, 403):
            raise GitHubAuthError(f"Auth failed ({resp.status}) for {url}")
        if resp.status in (409, 422):
            raise GitHubUnprocessableError(
                f"GitHub could not resolve the request ({resp.status}) for {url}"
            )
        raise GitHubError(f"GitHub error {resp.status} for {url}")

    async def _raise_for_secondary_limit(
        self, resp: aiohttp.ClientResponse, url: str
    ) -> None:
        """Turn a secondary rate limit (403 without quota headers) into ours.

        GitHub answers abuse detection with 403 and no ``Retry-After`` while
        ``X-RateLimit-Remaining`` still looks healthy, so the headers alone
        cannot tell it from a permission error. The message can.
        """
        if resp.status != 403 or self._retry_after(resp) is not None:
            return
        try:
            text = await resp.text()
        except (aiohttp.ClientError, UnicodeDecodeError):
            return
        lowered = text.lower()
        if "rate limit" in lowered or "abuse" in lowered:
            raise GitHubRateLimitError(
                f"GitHub rate limit reached for {url}", None
            )

    async def _get_json(
        self, url: str, params: dict[str, Any] | None = None
    ) -> Any:
        try:
            async with self._session.get(
                url, headers=self._headers, params=params, timeout=REQUEST_TIMEOUT
            ) as resp:
                await self._raise_for_secondary_limit(resp, url)
                self._raise_for_status(resp, url)
                try:
                    return await resp.json()
                except (aiohttp.ContentTypeError, ValueError) as err:
                    raise GitHubError(f"Invalid JSON from {url}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GitHubError(f"Could not reach GitHub: {err}") from err

    async def verify_token(self) -> dict[str, Any]:
        """Confirm the token works and return /user payload."""
        payload = await self._get_json(f"{GITHUB_API_BASE}/user")
        user = _as_dict(payload, "GET /user")
        login = user.get("login")
        if isinstance(login, str) and login:
            self._login = login
        return user

    async def async_get_login(self) -> str:
        """Login of the token's account, asking GitHub only when unknown.

        ``async_setup_entry`` already verifies the token, so discovery normally
        reuses that answer instead of spending another request on ``/user``.
        """
        if self._login is None:
            await self.verify_token()
        return self._login or ""

    async def get_repo(self, full_name: str) -> dict[str, Any]:
        payload = await self._get_json(f"{GITHUB_API_BASE}/repos/{full_name}")
        return _as_dict(payload, f"GET /repos/{full_name}")

    async def async_list_repositories(self) -> list[dict[str, Any]]:
        """Every repository the token can read, most recently updated first.

        ``/user/repos`` is what both token kinds answer with: a classic PAT gets
        everything the account sees, a fine-grained one only the repositories it
        was scoped to. That is exactly the set the caller should offer.

        Pages are deduplicated by name: the list is sorted by last update, so a
        repository updated while page 2 is being fetched moves to page 1 and
        would otherwise come back twice. A page that adds nothing new also ends
        the walk, which keeps a proxy that ignores ``page`` from multiplying
        the same hundred entries.
        """
        repositories: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, MAX_LISTED_REPOSITORIES // LIST_PAGE_SIZE + 1):
            payload = await self._get_json(
                f"{GITHUB_API_BASE}/user/repos",
                params={
                    "per_page": LIST_PAGE_SIZE,
                    "page": page,
                    "sort": "updated",
                    "affiliation": "owner,collaborator,organization_member",
                },
            )
            if not isinstance(payload, list):
                break
            added = 0
            for repository in payload:
                if not isinstance(repository, dict):
                    continue
                full_name = repository.get("full_name")
                if isinstance(full_name, str):
                    key = full_name.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                repositories.append(repository)
                added += 1
            if len(payload) < LIST_PAGE_SIZE or not added:
                break
        return repositories

    async def async_list_folder(
        self, full_name: str, path: str, ref: str | None = None
    ) -> list[dict[str, Any]]:
        """Entries of a directory, or an empty list when it does not exist."""
        url = (
            f"{GITHUB_API_BASE}/repos/{full_name}/contents/"
            f"{quote(path, safe='/')}"
        )
        try:
            payload = await self._get_json(
                url, params={"ref": ref} if ref else None
            )
        except (GitHubNotFoundError, GitHubUnprocessableError):
            return []
        if not isinstance(payload, list):
            return []
        return [entry for entry in payload if isinstance(entry, dict)]

    async def async_get_file_text(
        self, full_name: str, path: str, ref: str | None = None
    ) -> str | None:
        """Text of a small file, or None when it is missing or unreadable."""
        url = (
            f"{GITHUB_API_BASE}/repos/{full_name}/contents/"
            f"{quote(path, safe='/')}"
        )
        try:
            payload = await self._get_json(
                url, params={"ref": ref} if ref else None
            )
        except (GitHubNotFoundError, GitHubUnprocessableError):
            return None
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            return None
        content = payload.get("content")
        if not isinstance(content, str):
            return None
        try:
            return base64.b64decode(content).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

    async def get_latest_release(self, full_name: str) -> dict[str, Any] | None:
        """Return the release to install: latest, else newest published one.

        GitHub's ``/releases/latest`` ignores pre-releases and drafts, so a
        repository that only publishes pre-releases would otherwise look empty.
        """
        try:
            latest = await self._get_json(
                f"{GITHUB_API_BASE}/repos/{full_name}/releases/latest"
            )
        except GitHubNotFoundError:
            pass
        else:
            return latest if isinstance(latest, dict) else None

        releases = await self._get_json(
            f"{GITHUB_API_BASE}/repos/{full_name}/releases",
            params={"per_page": 10},
        )
        if not isinstance(releases, list):
            return None
        published = [
            release
            for release in releases
            if isinstance(release, dict) and not release.get("draft")
        ]
        for release in published:
            if not release.get("prerelease"):
                return release
        return published[0] if published else None

    async def get_commit_sha(self, full_name: str, ref: str) -> str | None:
        """Resolve a tag, branch or SHA to the commit it points at now.

        GitHub reports an unknown ref as 422 (or 409 for an empty repository),
        not as 404, so both are treated as "no such ref".
        """
        url = (
            f"{GITHUB_API_BASE}/repos/{full_name}/commits/"
            f"{quote(ref, safe='')}"
        )
        try:
            data = await self._get_json(url)
        except (GitHubNotFoundError, GitHubUnprocessableError):
            return None
        sha = data.get("sha") if isinstance(data, dict) else None
        return sha if isinstance(sha, str) else None

    async def download_tarball(self, full_name: str, ref: str) -> bytes:
        """Download a gzipped tarball for the given ref (preferably a SHA)."""
        url = (
            f"{GITHUB_API_BASE}/repos/{full_name}/tarball/"
            f"{quote(ref, safe='')}"
        )
        try:
            async with self._session.get(
                url,
                headers=self._headers,
                allow_redirects=True,
                timeout=TARBALL_TIMEOUT,
            ) as resp:
                await self._raise_for_secondary_limit(resp, url)
                self._raise_for_status(resp, url)
                length = resp.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > MAX_TARBALL_BYTES:
                    raise GitHubError(
                        "Tarball is larger than "
                        f"{MAX_TARBALL_BYTES // (1024 * 1024)} MiB"
                    )
                # Read in chunks: Content-Length is only a hint, the body can be
                # anything (and a redirect response may not carry it at all).
                downloaded = bytearray()
                async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                    downloaded.extend(chunk)
                    if len(downloaded) > MAX_TARBALL_BYTES:
                        raise GitHubError(
                            "Tarball is larger than "
                            f"{MAX_TARBALL_BYTES // (1024 * 1024)} MiB"
                        )
                return bytes(downloaded)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GitHubError(f"Could not reach GitHub: {err}") from err
