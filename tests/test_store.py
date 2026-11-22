"""Tests for the persistent repository store."""
import pytest

from custom_components.private_integrations.const import STORAGE_KEY
from custom_components.private_integrations.repository import Repository
from custom_components.private_integrations.store import RepositoryStore

FULL_NAME = "owner/foo"


def stored(hass_storage, repositories):
    hass_storage[STORAGE_KEY] = {"version": 1, "data": {"repositories": repositories}}


async def test_save_and_load_round_trip(hass, hass_storage):
    store = RepositoryStore(hass)
    await store.async_load()
    store.upsert(
        Repository(
            full_name=FULL_NAME,
            ref_type="tag",
            ref="v1.2.3",
            integration_name="foo",
            installed_version="v1.2.3",
            installed_sha="a" * 40,
            adopt=True,
        )
    )
    await store.async_save()

    reloaded = RepositoryStore(hass)
    await reloaded.async_load()

    repo = reloaded.get(FULL_NAME)
    assert repo is not None
    assert repo.to_dict() == store.get(FULL_NAME).to_dict()


async def test_lookup_is_case_insensitive(hass, hass_storage):
    stored(hass_storage, [{"full_name": "Owner/Foo"}])
    store = RepositoryStore(hass)

    await store.async_load()

    assert store.get("owner/foo") is not None
    assert store.get("OWNER/FOO") is not None


async def test_remove_reports_whether_it_removed_anything(hass, hass_storage):
    stored(hass_storage, [{"full_name": FULL_NAME}])
    store = RepositoryStore(hass)
    await store.async_load()

    assert store.remove("owner/FOO") is True
    assert store.remove("owner/foo") is False
    assert store.all() == []


async def test_upsert_replaces_an_existing_entry(hass, hass_storage):
    store = RepositoryStore(hass)
    await store.async_load()
    store.upsert(Repository(full_name=FULL_NAME, ref="main"))
    store.upsert(Repository(full_name=FULL_NAME, ref="develop"))

    assert len(store.all()) == 1
    assert store.get(FULL_NAME).ref == "develop"


async def test_load_is_idempotent(hass, hass_storage):
    stored(hass_storage, [{"full_name": FULL_NAME}])
    store = RepositoryStore(hass)

    await store.async_load()
    await store.async_load()

    assert len(store.all()) == 1


async def test_async_remove_deletes_the_storage(hass, hass_storage):
    stored(hass_storage, [{"full_name": FULL_NAME}])
    store = RepositoryStore(hass)
    await store.async_load()

    await store.async_remove()

    assert STORAGE_KEY not in hass_storage
    assert store.all() == []


async def test_malformed_entries_are_ignored(hass, hass_storage):
    """A corrupt file must not stop Home Assistant from setting up."""
    stored(
        hass_storage,
        ["not-a-repository", {"full_name": FULL_NAME}, {"ref": "no-owner"}, 42],
    )
    store = RepositoryStore(hass)

    await store.async_load()

    assert [repo.full_name for repo in store.all()] == [FULL_NAME]


async def test_a_non_list_repositories_key_is_ignored(hass, hass_storage):
    hass_storage[STORAGE_KEY] = {"version": 1, "data": {"repositories": {"a": 1}}}
    store = RepositoryStore(hass)

    await store.async_load()

    assert store.all() == []


async def test_empty_storage_loads_nothing(hass, hass_storage):
    store = RepositoryStore(hass)

    await store.async_load()

    assert store.all() == []


async def test_an_entry_with_a_non_string_full_name_is_skipped(
    hass, hass_storage
):
    stored(
        hass_storage,
        [
            {"full_name": 123, "ref_type": "branch", "ref": "main"},
            {"full_name": "owner/good", "ref_type": "branch", "ref": "main"},
        ],
    )
    store = RepositoryStore(hass)

    await store.async_load()

    assert [r.full_name for r in store.all()] == ["owner/good"]


@pytest.mark.parametrize(
    "raw",
    [
        {"full_name": ""},
        {"full_name": "owner/foo", "ref_type": "banana"},
        {"full_name": "owner/foo", "ref_type": "branch", "integration_name": 5},
        {"full_name": "owner/foo", "ref_type": "branch", "installed_sha": 1},
        {"full_name": "owner/foo", "ref_type": "branch", "adopt": "yes"},
        {"ref_type": "branch"},
        {"full_name": "owner/foo", "ref_type": None},
    ],
)
async def test_a_broken_entry_never_crashes_the_load(hass, hass_storage, raw):
    stored(hass_storage, [raw])
    store = RepositoryStore(hass)

    await store.async_load()

    assert store.all() == []


async def test_a_missing_ref_type_falls_back_to_releases(hass, hass_storage):
    stored(hass_storage, [{"full_name": "owner/foo"}])
    store = RepositoryStore(hass)

    await store.async_load()

    repo = store.get("owner/foo")
    assert repo is not None
    assert repo.ref_type == "release"
    assert repo.ref is None


async def test_a_damaged_file_does_not_break_the_load(hass, hass_storage):
    """The stored payload itself can be the wrong shape, not just an entry."""
    hass_storage[STORAGE_KEY] = {"version": 1, "data": [1, 2, 3]}
    store = RepositoryStore(hass)

    await store.async_load()

    assert store.all() == []


async def test_a_stored_payload_that_is_not_an_object_is_ignored(hass, hass_storage):
    hass_storage[STORAGE_KEY] = {"version": 1, "data": "nope"}
    store = RepositoryStore(hass)

    await store.async_load()

    assert store.all() == []


async def test_an_older_storage_file_is_read(hass, hass_storage):
    """A file from an older build migrates through the store's hook.

    Home Assistant hands every major version mismatch to async_migrate_func and
    the default one raises NotImplementedError, which is raised out of setup.
    """
    hass_storage[STORAGE_KEY] = {
        "version": 0,
        "data": {
            "repositories": [
                {"full_name": "owner/foo", "ref_type": "branch", "ref": "main"}
            ]
        },
    }
    store = RepositoryStore(hass)

    await store.async_load()

    assert [r.full_name for r in store.all()] == ["owner/foo"]


async def test_a_newer_storage_file_does_not_break_the_load(hass, hass_storage):
    """A downgrade must not leave the integration unusable.

    Home Assistant 2026.10 and later refuse the file themselves
    (UnsupportedStorageVersionError) before any hook runs; older versions hand it
    to the hook. Either way setup has to survive, and the store stays usable.
    """
    hass_storage[STORAGE_KEY] = {
        "version": 99,
        "data": {
            "repositories": [
                {"full_name": "owner/foo", "ref_type": "branch", "ref": "main"}
            ]
        },
    }
    store = RepositoryStore(hass)

    await store.async_load()

    store.upsert(
        Repository(full_name="other/bar", ref_type="branch", ref="main")
    )
    await store.async_save()
    assert store.get("other/bar") is not None


@pytest.mark.parametrize(
    "full_name",
    ["https://github.com/owner/foo", "owner/foo?x=1", "owner", "owner/foo/bar"],
)
async def test_a_stored_name_that_is_not_owner_slash_repo_is_skipped(
    hass, hass_storage, full_name
):
    """Every API URL is built from this value, so it must be exactly owner/repo."""
    entry = {"full_name": full_name, "ref_type": "branch", "ref": "main"}
    stored(hass_storage, [entry])
    store = RepositoryStore(hass)

    await store.async_load()

    assert store.all() == []


def test_a_bad_integration_name_is_rejected():
    with pytest.raises(ValueError, match="integration_name"):
        Repository.from_dict(
            {"full_name": FULL_NAME, "integration_name": "../outside"}
        )


def test_an_integration_name_is_normalised():
    repo = Repository.from_dict(
        {"full_name": FULL_NAME, "integration_name": "  foo  "}
    )

    assert repo.integration_name == "foo"


async def test_a_stored_entry_with_a_bad_folder_is_skipped(hass, hass_storage):
    stored(
        hass_storage,
        [{"full_name": FULL_NAME, "integration_name": "../outside"}],
    )
    store = RepositoryStore(hass)

    await store.async_load()

    assert store.all() == []
