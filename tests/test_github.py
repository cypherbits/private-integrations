"""Tests for the GitHub API client."""
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.private_integrations import github
from custom_components.private_integrations.github import (
    GitHubClient,
    GitHubError,
    GitHubUnprocessableError,
)

USER_URL = "https://api.github.com/user"
TARBALL_URL = "https://api.github.com/repos/owner/foo/tarball/main"


async def test_download_returns_the_body(hass, aioclient_mock):
    aioclient_mock.get(TARBALL_URL, content=b"\x1f\x8bpayload")
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert await client.download_tarball("owner/foo", "main") == b"\x1f\x8bpayload"


async def test_download_is_capped_while_reading(hass, aioclient_mock, monkeypatch):
    """The cap applies even when GitHub sends no Content-Length."""
    monkeypatch.setattr(github, "MAX_TARBALL_BYTES", 1024)
    aioclient_mock.get(TARBALL_URL, content=b"x" * 4096)
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(GitHubError, match="larger than"):
        await client.download_tarball("owner/foo", "main")


async def test_download_is_capped_by_content_length(
    hass, aioclient_mock, monkeypatch
):
    monkeypatch.setattr(github, "MAX_TARBALL_BYTES", 1024)
    aioclient_mock.get(
        TARBALL_URL, content=b"x", headers={"Content-Length": "4096"}
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(GitHubError, match="larger than"):
        await client.download_tarball("owner/foo", "main")


async def test_download_404_is_not_found(hass, aioclient_mock):
    aioclient_mock.get(TARBALL_URL, status=404)
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(GitHubError, match="Not found"):
        await client.download_tarball("owner/foo", "main")


# --- Rate limits ------------------------------------------------------------


async def test_retry_after_header_is_reported(hass, aioclient_mock):
    from custom_components.private_integrations.github import GitHubRateLimitError

    aioclient_mock.get(USER_URL, status=403, headers={"Retry-After": "30"})
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(GitHubRateLimitError) as info:
        await client.verify_token()

    assert info.value.retry_after == 30


async def test_exhausted_quota_is_a_rate_limit(hass, aioclient_mock):
    import time

    from custom_components.private_integrations.github import GitHubRateLimitError

    reset = int(time.time()) + 60
    aioclient_mock.get(
        USER_URL,
        status=403,
        headers={
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(reset),
        },
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(GitHubRateLimitError) as info:
        await client.verify_token()

    assert info.value.retry_after is not None
    assert 50 <= info.value.retry_after <= 60


async def test_plain_403_is_still_an_auth_error(hass, aioclient_mock):
    from custom_components.private_integrations.github import GitHubAuthError

    aioclient_mock.get(USER_URL, status=403)
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(GitHubAuthError):
        await client.verify_token()


async def test_429_is_a_rate_limit(hass, aioclient_mock):
    from custom_components.private_integrations.github import GitHubRateLimitError

    aioclient_mock.get(USER_URL, status=429)
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(GitHubRateLimitError):
        await client.verify_token()


# --- Error mapping ----------------------------------------------------------


async def test_401_is_an_auth_error(hass, aioclient_mock):
    from custom_components.private_integrations.github import GitHubAuthError

    aioclient_mock.get(USER_URL, status=401)
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(GitHubAuthError, match="401"):
        await client.verify_token()


async def test_500_is_a_github_error(hass, aioclient_mock):
    aioclient_mock.get(USER_URL, status=500)
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(GitHubError, match="500"):
        await client.verify_token()


async def test_invalid_json_is_reported(hass, aioclient_mock):
    aioclient_mock.get(
        USER_URL, text="not json", headers={"Content-Type": "text/plain"}
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(GitHubError, match="Invalid JSON"):
        await client.verify_token()


async def test_unknown_repository_is_not_found(hass, aioclient_mock):
    from custom_components.private_integrations.github import GitHubNotFoundError

    aioclient_mock.get("https://api.github.com/repos/owner/foo", status=404)
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(GitHubNotFoundError):
        await client.get_repo("owner/foo")


@pytest.mark.parametrize("status", [404, 409, 422])
async def test_unknown_ref_resolves_to_none(hass, aioclient_mock, status):
    """GitHub reports an unknown ref as 404, 409 (empty repo) or 422."""
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/commits/nope", status=status
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert await client.get_commit_sha("owner/foo", "nope") is None


async def test_an_unprocessable_response_is_reported(hass, aioclient_mock):
    aioclient_mock.get("https://api.github.com/repos/owner/foo", status=422)
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(GitHubUnprocessableError):
        await client.get_repo("owner/foo")


async def test_commit_sha_is_returned(hass, aioclient_mock):
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/commits/main", json={"sha": "b" * 40}
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert await client.get_commit_sha("owner/foo", "main") == "b" * 40


async def test_ref_with_a_slash_is_encoded(hass, aioclient_mock):
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/commits/feature%2Fnew",
        json={"sha": "c" * 40},
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert await client.get_commit_sha("owner/foo", "feature/new") == "c" * 40


# --- Release selection ------------------------------------------------------


async def test_latest_release_is_used_when_published(hass, aioclient_mock):
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/releases/latest",
        json={"tag_name": "v2.0.0"},
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert (await client.get_latest_release("owner/foo"))["tag_name"] == "v2.0.0"


async def test_release_falls_back_to_the_newest_published(hass, aioclient_mock):
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/releases/latest", status=404
    )
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/releases",
        json=[
            {"tag_name": "v3.0.0-beta.1", "prerelease": True, "draft": False},
            {"tag_name": "v2.9.0", "prerelease": False, "draft": False},
        ],
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    release = await client.get_latest_release("owner/foo")

    assert release["tag_name"] == "v2.9.0"


async def test_release_uses_a_prerelease_when_there_is_nothing_else(
    hass, aioclient_mock
):
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/releases/latest", status=404
    )
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/releases",
        json=[{"tag_name": "v3.0.0-beta.1", "prerelease": True, "draft": False}],
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    release = await client.get_latest_release("owner/foo")

    assert release["tag_name"] == "v3.0.0-beta.1"


async def test_release_ignores_drafts(hass, aioclient_mock):
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/releases/latest", status=404
    )
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/releases",
        json=[{"tag_name": "v3.0.0", "prerelease": False, "draft": True}],
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert await client.get_latest_release("owner/foo") is None


async def test_release_with_no_releases_returns_none(hass, aioclient_mock):
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/releases/latest", status=404
    )
    aioclient_mock.get("https://api.github.com/repos/owner/foo/releases", json=[])
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert await client.get_latest_release("owner/foo") is None


async def test_release_with_an_unexpected_payload_returns_none(
    hass, aioclient_mock
):
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/releases/latest", status=404
    )
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/releases", json={"message": "?"}
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert await client.get_latest_release("owner/foo") is None


async def test_download_network_error_is_a_github_error(hass, aioclient_mock):
    import aiohttp

    aioclient_mock.get(TARBALL_URL, exc=aiohttp.ClientError("boom"))
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(GitHubError, match="Could not reach GitHub"):
        await client.download_tarball("owner/foo", "main")


@pytest.mark.parametrize(
    "payload",
    [None, [], "surprise", 42],
)
async def test_an_unexpected_user_payload_is_reported(hass, aioclient_mock, payload):
    """GitHub, or whatever sits in front of it, can answer with anything.

    Returning the payload unchecked made the caller crash on `.get` instead of
    reporting a GitHub error, so the setup form showed "Unknown error".
    """
    aioclient_mock.get(USER_URL, json=payload)
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(GitHubError):
        await client.verify_token()


@pytest.mark.parametrize("payload", [None, [], "surprise"])
async def test_an_unexpected_repository_payload_is_reported(
    hass, aioclient_mock, payload
):
    aioclient_mock.get("https://api.github.com/repos/owner/foo", json=payload)
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(GitHubError):
        await client.get_repo("owner/foo")


@pytest.mark.parametrize("payload", [[], "surprise", 7])
async def test_an_unexpected_release_payload_is_ignored(
    hass, aioclient_mock, payload
):
    """A release endpoint answering garbage means "no release", not a crash."""
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/releases/latest", status=404
    )
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/releases", json=payload
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert await client.get_latest_release("owner/foo") is None


async def test_a_releases_latest_answering_garbage_is_ignored(hass, aioclient_mock):
    """`/releases/latest` answering something else means "no release"."""
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/releases/latest", json=["nope"]
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert await client.get_latest_release("owner/foo") is None


async def test_a_release_list_with_junk_entries_is_filtered(hass, aioclient_mock):
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/releases/latest", status=404
    )
    aioclient_mock.get(
        "https://api.github.com/repos/owner/foo/releases",
        json=["junk", {"tag_name": "v1", "prerelease": False, "draft": False}],
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    release = await client.get_latest_release("owner/foo")

    assert release == {"tag_name": "v1", "prerelease": False, "draft": False}


# --- Listing repositories and reading files --------------------------------

LIST_URL = "https://api.github.com/user/repos"
PAGE_1 = (
    f"{LIST_URL}?per_page=100&page=1&sort=updated"
    "&affiliation=owner%2Ccollaborator%2Corganization_member"
)
PAGE_2 = (
    f"{LIST_URL}?per_page=100&page=2&sort=updated"
    "&affiliation=owner%2Ccollaborator%2Corganization_member"
)
CONTENTS_URL = "https://api.github.com/repos/owner/foo/contents/custom_components"


async def test_repositories_are_listed_with_the_paging_parameters(
    hass, aioclient_mock
):
    aioclient_mock.get(PAGE_1, json=[{"full_name": "owner/foo"}])
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert await client.async_list_repositories() == [{"full_name": "owner/foo"}]


async def test_a_full_page_asks_for_the_next_one(hass, aioclient_mock):
    first = [{"full_name": f"owner/repo{index}"} for index in range(100)]
    aioclient_mock.get(PAGE_1, json=first)
    aioclient_mock.get(PAGE_2, json=[{"full_name": "owner/last"}])
    client = GitHubClient(async_get_clientsession(hass), "token")

    repositories = await client.async_list_repositories()

    assert len(repositories) == 101
    assert repositories[-1] == {"full_name": "owner/last"}


async def test_a_repository_returned_twice_is_listed_once(hass, aioclient_mock):
    """The list is sorted by update time, so a repository can shift pages."""
    aioclient_mock.get(
        PAGE_1,
        json=[{"full_name": "owner/foo"}, {"full_name": "Owner/Foo"}],
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert await client.async_list_repositories() == [{"full_name": "owner/foo"}]


async def test_a_page_that_only_repeats_entries_ends_the_listing(
    hass, aioclient_mock
):
    """A proxy that ignores ``page`` must not multiply the same hundred."""
    page = [{"full_name": f"owner/repo{index}"} for index in range(100)]
    aioclient_mock.get(PAGE_1, json=page)
    aioclient_mock.get(PAGE_2, json=page)
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert len(await client.async_list_repositories()) == 100


async def test_a_non_list_payload_ends_the_listing(hass, aioclient_mock):
    aioclient_mock.get(PAGE_1, json={"message": "unexpected"})
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert await client.async_list_repositories() == []


async def test_a_folder_is_listed(hass, aioclient_mock):
    aioclient_mock.get(
        CONTENTS_URL,
        json=[{"name": "foo", "type": "dir"}, {"name": "README.md", "type": "file"}],
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    entries = await client.async_list_folder("owner/foo", "custom_components", "main")

    assert [entry["name"] for entry in entries] == ["foo", "README.md"]


async def test_a_missing_folder_is_empty(hass, aioclient_mock):
    aioclient_mock.get(CONTENTS_URL, status=404)
    client = GitHubClient(async_get_clientsession(hass), "token")

    entries = await client.async_list_folder("owner/foo", "custom_components", "main")

    assert entries == []


async def test_a_folder_answering_garbage_is_empty(hass, aioclient_mock):
    aioclient_mock.get(CONTENTS_URL, json={"message": "not a list"})
    client = GitHubClient(async_get_clientsession(hass), "token")

    entries = await client.async_list_folder("owner/foo", "custom_components", "main")

    assert entries == []


async def test_a_file_is_decoded_from_base64(hass, aioclient_mock):
    import base64

    aioclient_mock.get(
        f"{CONTENTS_URL}/foo/manifest.json",
        json={
            "encoding": "base64",
            "content": base64.b64encode(b'{"domain": "foo"}').decode() + "\n",
        },
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    text = await client.async_get_file_text(
        "owner/foo", "custom_components/foo/manifest.json", "main"
    )

    assert text == '{"domain": "foo"}'


async def test_a_missing_file_is_none(hass, aioclient_mock):
    aioclient_mock.get(f"{CONTENTS_URL}/foo/manifest.json", status=404)
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert (
        await client.async_get_file_text(
            "owner/foo", "custom_components/foo/manifest.json", "main"
        )
        is None
    )


async def test_a_file_without_base64_content_is_none(hass, aioclient_mock):
    aioclient_mock.get(
        f"{CONTENTS_URL}/foo/manifest.json",
        json={"encoding": "none", "content": ""},
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert (
        await client.async_get_file_text(
            "owner/foo", "custom_components/foo/manifest.json", "main"
        )
        is None
    )


async def test_a_file_with_broken_base64_is_none(hass, aioclient_mock):
    aioclient_mock.get(
        f"{CONTENTS_URL}/foo/manifest.json",
        json={"encoding": "base64", "content": "%%% not base64 %%%"},
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    assert (
        await client.async_get_file_text(
            "owner/foo", "custom_components/foo/manifest.json", "main"
        )
        is None
    )


async def test_a_secondary_rate_limit_403_is_a_rate_limit(hass, aioclient_mock):
    """GitHub sends 403 for abuse detection, with no quota headers."""
    aioclient_mock.get(
        USER_URL,
        status=403,
        text=(
            "You have exceeded a secondary rate limit. "
            "Please wait a few minutes before you try again."
        ),
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(github.GitHubRateLimitError):
        await client.verify_token()


async def test_a_permission_403_is_still_an_auth_error(hass, aioclient_mock):
    aioclient_mock.get(
        USER_URL,
        status=403,
        text="Resource not accessible by personal access token",
    )
    client = GitHubClient(async_get_clientsession(hass), "token")

    with pytest.raises(github.GitHubAuthError):
        await client.verify_token()
