"""Tests for the repository discovery behind "find integrations"."""
from custom_components.private_integrations.catalog import (
    MAX_MANIFESTS,
    Candidate,
    Discovery,
    async_detect_integrations,
)
from custom_components.private_integrations.github import (
    GitHubError,
    GitHubRateLimitError,
)


class FakeClient:
    """A GitHub client that answers from dictionaries and records the calls."""

    def __init__(self, repositories=(), folders=None, files=None):
        self._repositories = list(repositories)
        self._folders = folders or {}
        self._files = files or {}
        self.folder_calls: list[tuple[str, str, str | None]] = []
        self.file_calls: list[tuple[str, str, str | None]] = []

    async def async_list_repositories(self):
        return self._repositories

    async def async_list_folder(self, full_name, path, ref=None):
        self.folder_calls.append((full_name, path, ref))
        value = self._folders.get(full_name)
        if isinstance(value, Exception):
            raise value
        return value or []

    async def async_get_file_text(self, full_name, path, ref=None):
        self.file_calls.append((full_name, path, ref))
        value = self._files.get((full_name, path))
        if isinstance(value, Exception):
            raise value
        return value


def repo(full_name="owner/foo", **extra):
    return {"full_name": full_name, "default_branch": "main", **extra}


def folder(*names):
    return [{"name": name, "type": "dir"} for name in names]


def manifest(domain="foo", version="1.2.3"):
    import json

    return json.dumps({"domain": domain, "version": version})


async def test_detects_the_integration_of_a_repository():
    client = FakeClient(
        [repo(private=True)],
        folders={"owner/foo": folder("foo")},
        files={("owner/foo", "custom_components/foo/manifest.json"): manifest()},
    )

    discovery = await async_detect_integrations(client)

    assert discovery.candidates == [
        Candidate(
            full_name="owner/foo",
            integration_name="foo",
            version="1.2.3",
            default_branch="main",
            private=True,
        )
    ]
    assert discovery.inspected == 1
    assert discovery.truncated is False
    assert discovery.rate_limited is False
    # The default branch is what gets inspected, not "main" blindly.
    assert client.folder_calls == [("owner/foo", "custom_components", "main")]
    assert client.file_calls == [
        ("owner/foo", "custom_components/foo/manifest.json", "main")
    ]


async def test_uses_the_repository_default_branch():
    client = FakeClient(
        [{"full_name": "owner/foo", "default_branch": "trunk"}],
        folders={"owner/foo": folder("foo")},
        files={("owner/foo", "custom_components/foo/manifest.json"): manifest()},
    )

    await async_detect_integrations(client)

    assert client.folder_calls == [("owner/foo", "custom_components", "trunk")]


async def test_skips_tracked_archived_and_malformed_repositories():
    client = FakeClient(
        [
            repo("owner/tracked"),
            repo("owner/archived", archived=True),
            repo("owner/one"),
            "not a repository",
            {"full_name": "too/many/slashes"},
            {"default_branch": "main"},
        ],
        folders={
            "owner/tracked": folder("tracked"),
            "owner/archived": folder("archived"),
            "owner/one": folder("one"),
        },
    )

    discovery = await async_detect_integrations(client, tracked=["Owner/Tracked"])

    assert discovery.inspected == 1
    assert [candidate.integration_name for candidate in discovery.candidates] == []


async def test_a_manifest_is_what_makes_a_folder_an_integration():
    """A folder without a matching, valid manifest is not offered."""
    client = FakeClient(
        [repo()],
        folders={"owner/foo": folder("good", "wrong_domain", "broken", "no_manifest")},
        files={
            ("owner/foo", "custom_components/good/manifest.json"): manifest("good"),
            ("owner/foo", "custom_components/wrong_domain/manifest.json"): manifest(
                "another"
            ),
            ("owner/foo", "custom_components/broken/manifest.json"): "{not json",
        },
    )

    discovery = await async_detect_integrations(client)

    assert [candidate.integration_name for candidate in discovery.candidates] == [
        "good"
    ]


async def test_a_manifest_that_is_not_an_object_is_ignored():
    client = FakeClient(
        [repo()],
        folders={"owner/foo": folder("foo")},
        files={("owner/foo", "custom_components/foo/manifest.json"): "[]"},
    )

    discovery = await async_detect_integrations(client)

    assert discovery.candidates == []


async def test_a_version_that_is_not_text_is_dropped():
    client = FakeClient(
        [repo()],
        folders={"owner/foo": folder("foo")},
        files={
            ("owner/foo", "custom_components/foo/manifest.json"): '{"domain": "foo"}'
        },
    )

    discovery = await async_detect_integrations(client)

    assert discovery.candidates[0].version is None


async def test_one_unreadable_repository_does_not_fail_the_lookup():
    client = FakeClient(
        [repo("owner/broken"), repo("owner/foo")],
        folders={
            "owner/broken": GitHubError("boom"),
            "owner/foo": folder("foo"),
        },
        files={("owner/foo", "custom_components/foo/manifest.json"): manifest()},
    )

    discovery = await async_detect_integrations(client)

    assert [candidate.full_name for candidate in discovery.candidates] == ["owner/foo"]


async def test_a_rate_limit_is_reported_instead_of_raised():
    client = FakeClient(
        [repo("owner/limited"), repo("owner/foo")],
        folders={
            "owner/limited": GitHubRateLimitError("slow down", 30),
            "owner/foo": folder("foo"),
        },
        files={("owner/foo", "custom_components/foo/manifest.json"): manifest()},
    )

    discovery = await async_detect_integrations(client)

    assert discovery.rate_limited is True


async def test_a_manifest_read_that_fails_is_skipped():
    client = FakeClient(
        [repo()],
        folders={"owner/foo": folder("first", "second")},
        files={
            ("owner/foo", "custom_components/first/manifest.json"): GitHubError("boom"),
            ("owner/foo", "custom_components/second/manifest.json"): manifest("second"),
        },
    )

    discovery = await async_detect_integrations(client)

    assert [candidate.integration_name for candidate in discovery.candidates] == [
        "second"
    ]


async def test_the_lookup_is_capped_and_says_so():
    client = FakeClient([repo(f"owner/repo{index}") for index in range(5)])

    discovery = await async_detect_integrations(client, max_repositories=2)

    assert discovery.inspected == 2
    assert discovery.truncated is True


async def test_manifest_reads_are_capped():
    """A repository cannot turn the lookup into a thousand API calls."""
    names = [f"integration{index:03d}" for index in range(MAX_MANIFESTS + 5)]
    client = FakeClient(
        [repo()],
        folders={"owner/foo": folder(*names)},
        files={
            ("owner/foo", f"custom_components/{name}/manifest.json"): manifest(name)
            for name in names
        },
    )

    discovery = await async_detect_integrations(client)

    assert len(client.file_calls) == MAX_MANIFESTS
    assert len(discovery.candidates) == MAX_MANIFESTS


async def test_no_repositories_is_an_empty_lookup():
    discovery = await async_detect_integrations(FakeClient())

    assert discovery == Discovery()


async def test_a_candidate_knows_if_it_is_already_on_disk():
    client = FakeClient(
        [repo()],
        folders={"owner/foo": folder("foo")},
        files={("owner/foo", "custom_components/foo/manifest.json"): manifest()},
    )

    discovery = await async_detect_integrations(client, installed=["foo"])

    assert discovery.candidates[0].installed is True


async def test_candidates_are_sorted_by_repository():
    client = FakeClient(
        [repo("owner/zeta"), repo("owner/alpha")],
        folders={"owner/zeta": folder("zeta"), "owner/alpha": folder("alpha")},
        files={
            ("owner/zeta", "custom_components/zeta/manifest.json"): manifest("zeta"),
            ("owner/alpha", "custom_components/alpha/manifest.json"): manifest("alpha"),
        },
    )

    discovery = await async_detect_integrations(client)

    assert [candidate.full_name for candidate in discovery.candidates] == [
        "owner/alpha",
        "owner/zeta",
    ]


def test_candidate_label_and_key():
    candidate = Candidate(
        full_name="owner/foo",
        integration_name="foo",
        version="1.0.0",
        private=True,
        installed=True,
    )

    assert candidate.key == "owner/foo::foo"
    assert "owner/foo" in candidate.label
    assert "custom_components/foo" in candidate.label
    assert "(1.0.0)" in candidate.label
    assert "private" in candidate.label
    assert "already installed" in candidate.label


def test_candidate_key_is_unique_per_integration():
    """A repository can hold several integrations; the selector must differ."""
    first = Candidate(full_name="owner/foo", integration_name="one")
    second = Candidate(full_name="owner/foo", integration_name="two")

    assert first.key != second.key
