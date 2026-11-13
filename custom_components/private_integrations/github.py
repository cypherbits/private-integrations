"""GitHub API client for Private Integrations."""
from __future__ import annotations

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

    async def _get_json(
        self, url: str, params: dict[str, Any] | None = None
    ) -> Any:
        try:
            async with self._session.get(
                url, headers=self._headers, params=params, timeout=REQUEST_TIMEOUT
            ) as resp:
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
        return _as_dict(payload, "GET /user")

    async def get_repo(self, full_name: str) -> dict[str, Any]:
        payload = await self._get_json(f"{GITHUB_API_BASE}/repos/{full_name}")
        return _as_dict(payload, f"GET /repos/{full_name}")

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
