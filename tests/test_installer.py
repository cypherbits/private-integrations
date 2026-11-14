"""Tests for tarball extraction and installation."""
import io
import json
import tarfile

import pytest

from custom_components.private_integrations import installer
from custom_components.private_integrations.installer import (
    PROTECTED_INTEGRATIONS,
    STAGING_PREFIX,
    InstallerError,
    install_from_tarball,
    is_managed,
    list_integrations,
    read_marker,
    recover_stale_installs,
    remove_integration,
    validate_integration_name,
    write_marker,
)


def make_tarball(members):
    """Build a .tar.gz from (name, content) pairs; content None means a dir."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in members:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def manifest(domain, version="1.0.0"):
    return json.dumps({"domain": domain, "version": version}).encode()


def repo_tarball(domain="foo", version="1.0.0", extra=()):
    members = [
        ("top", None),
        (f"top/custom_components/{domain}/manifest.json", manifest(domain, version)),
        (f"top/custom_components/{domain}/__init__.py", b"# version 1\n"),
        (f"top/custom_components/{domain}/translations/en.json", b"{}"),
        ("top/README.md", b"readme"),
        *extra,
    ]
    return make_tarball(members)


@pytest.fixture
def dest_root(tmp_path):
    root = tmp_path / "custom_components"
    root.mkdir()
    return root


def test_installs_only_the_integration(dest_root):
    name, path = install_from_tarball(repo_tarball(), dest_root, None)

    assert name == "foo"
    assert path == dest_root / "foo"
    assert (path / "manifest.json").is_file()
    assert (path / "translations/en.json").is_file()
    assert not (dest_root / "README.md").exists()


def test_leaves_no_staging_artifacts(dest_root):
    install_from_tarball(repo_tarball(), dest_root, None)

    assert [p.name for p in dest_root.iterdir()] == ["foo"]


def test_rejects_archive_without_manifest(dest_root):
    tarball = make_tarball(
        [("top", None), ("top/custom_components/bar/readme.txt", b"x")]
    )

    with pytest.raises(InstallerError, match="manifest.json is missing"):
        install_from_tarball(tarball, dest_root, None)
    assert not (dest_root / "bar").exists()


def test_rejects_domain_not_matching_folder(dest_root):
    tarball = make_tarball(
        [
            ("top", None),
            ("top/custom_components/wrong/manifest.json", manifest("real_domain")),
        ]
    )

    with pytest.raises(InstallerError, match="does not match folder"):
        install_from_tarball(tarball, dest_root, None)
    assert not (dest_root / "wrong").exists()


def test_rejects_invalid_json_manifest(dest_root):
    tarball = make_tarball(
        [
            ("top", None),
            ("top/custom_components/broken/manifest.json", b"{not json"),
        ]
    )

    with pytest.raises(InstallerError, match="not valid JSON"):
        install_from_tarball(tarball, dest_root, None)


def test_blocks_path_traversal(dest_root, tmp_path):
    tarball = make_tarball(
        [
            ("top", None),
            ("top/custom_components/foo/manifest.json", manifest("foo")),
            ("top/custom_components/foo/../../../escaped.txt", b"pwn"),
        ]
    )

    with pytest.raises(InstallerError, match="Path traversal blocked"):
        install_from_tarball(tarball, dest_root, "foo")
    assert not (tmp_path / "escaped.txt").exists()


def test_failed_upgrade_keeps_previous_version(dest_root):
    _, path = install_from_tarball(repo_tarball(), dest_root, None)
    original = (path / "__init__.py").read_text()

    broken = make_tarball(
        [
            ("top", None),
            ("top/custom_components/foo/manifest.json", manifest("foo", "2.0.0")),
            ("top/custom_components/foo//etc/evil", b"boom"),
        ]
    )

    with pytest.raises(InstallerError):
        install_from_tarball(broken, dest_root, "foo")

    assert (path / "__init__.py").read_text() == original
    assert b'"1.0.0"' in (path / "manifest.json").read_bytes()
    assert sorted(p.name for p in dest_root.iterdir()) == ["foo"]


def test_failed_validation_keeps_previous_version(dest_root):
    _, path = install_from_tarball(repo_tarball(), dest_root, None)

    mismatch = make_tarball(
        [
            ("top", None),
            ("top/custom_components/foo/manifest.json", manifest("other")),
        ]
    )

    with pytest.raises(InstallerError, match="does not match folder"):
        install_from_tarball(mismatch, dest_root, "foo")

    assert b'"1.0.0"' in (path / "manifest.json").read_bytes()


@pytest.mark.parametrize("name", sorted(PROTECTED_INTEGRATIONS))
def test_refuses_protected_integrations(dest_root, name):
    with pytest.raises(InstallerError, match="protected"):
        install_from_tarball(repo_tarball(), dest_root, name)
    assert not (dest_root / name).exists()


def test_refuses_to_replace_foreign_folder(dest_root):
    foreign = dest_root / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("keep")

    tarball = make_tarball(
        [
            ("top", None),
            (
                "top/custom_components/foreign/manifest.json",
                manifest("foreign"),
            ),
        ]
    )

    with pytest.raises(InstallerError, match="not managed by Private Integrations"):
        install_from_tarball(tarball, dest_root, "foreign", allow_replace=False)
    assert (foreign / "keep.txt").read_text() == "keep"


def test_ownership_marker(dest_root):
    _, path = install_from_tarball(repo_tarball(), dest_root, None)
    assert not is_managed(path)

    write_marker(path, {"full_name": "owner/foo"})

    assert is_managed(path)
    assert read_marker(path)["full_name"] == "owner/foo"


def test_requires_integration_name_when_ambiguous(dest_root):
    tarball = make_tarball(
        [
            ("top", None),
            ("top/custom_components/a/manifest.json", manifest("a")),
            ("top/custom_components/b/manifest.json", manifest("b")),
        ]
    )

    assert list_integrations(tarball) == ["a", "b"]
    with pytest.raises(InstallerError, match="specify integration_name"):
        install_from_tarball(tarball, dest_root, None)
    assert install_from_tarball(tarball, dest_root, "b")[0] == "b"


def test_remove_integration(dest_root):
    _, path = install_from_tarball(repo_tarball(), dest_root, None)

    assert remove_integration(dest_root, "foo")
    assert not path.exists()


def test_remove_integration_refuses_protected(dest_root):
    with pytest.raises(InstallerError, match="protected"):
        remove_integration(dest_root, "hacs")


# --- Archive expansion limits ---------------------------------------------


def test_refuses_an_archive_that_expands_too_much(dest_root, monkeypatch):
    monkeypatch.setattr(installer, "MAX_EXTRACTED_BYTES", 4096)
    tarball = repo_tarball(
        extra=[("top/custom_components/foo/blob.bin", b"x" * 8192)]
    )

    with pytest.raises(InstallerError, match="expands to more than"):
        install_from_tarball(tarball, dest_root, None)

    assert list(dest_root.iterdir()) == []


def test_refuses_an_archive_with_too_many_entries(dest_root, monkeypatch):
    monkeypatch.setattr(installer, "MAX_ARCHIVE_MEMBERS", 3)

    with pytest.raises(InstallerError, match="entries"):
        install_from_tarball(repo_tarball(), dest_root, None)

    assert list(dest_root.iterdir()) == []


def test_rejected_expansion_keeps_the_installed_version(dest_root, monkeypatch):
    install_from_tarball(repo_tarball(), dest_root, None)
    installed = (dest_root / "foo" / "__init__.py").read_text()
    monkeypatch.setattr(installer, "MAX_EXTRACTED_BYTES", 4096)
    tarball = repo_tarball(
        version="2.0.0", extra=[("top/custom_components/foo/blob.bin", b"x" * 8192)]
    )

    with pytest.raises(InstallerError, match="expands to more than"):
        install_from_tarball(tarball, dest_root, None)

    assert (dest_root / "foo" / "__init__.py").read_text() == installed
    assert sorted(p.name for p in dest_root.iterdir()) == ["foo"]


def test_list_integrations_refuses_too_many_entries(monkeypatch):
    monkeypatch.setattr(installer, "MAX_ARCHIVE_MEMBERS", 3)

    with pytest.raises(InstallerError, match="entries"):
        list_integrations(repo_tarball())


# --- Recovery from an interrupted swap ------------------------------------


def test_recovery_restores_a_backup_without_its_integration(dest_root):
    backup = dest_root / ".foo.private_integrations-backup"
    backup.mkdir()
    (backup / "manifest.json").write_text("{}")

    actions = recover_stale_installs(dest_root)

    assert (dest_root / "foo" / "manifest.json").is_file()
    assert not backup.exists()
    assert "restored foo" in actions[0]


def test_recovery_drops_a_backup_that_is_no_longer_needed(dest_root):
    install_from_tarball(repo_tarball(), dest_root, None)
    backup = dest_root / ".foo.private_integrations-backup"
    backup.mkdir()
    (backup / "old.txt").write_text("stale")

    actions = recover_stale_installs(dest_root)

    assert not backup.exists()
    assert (dest_root / "foo" / "__init__.py").is_file()
    assert "stale backup" in actions[0]


def test_recovery_drops_stale_staging_folders(dest_root):
    staging = dest_root / f"{STAGING_PREFIX}abcd"
    staging.mkdir()
    (staging / "junk.txt").write_text("junk")

    actions = recover_stale_installs(dest_root)

    assert not staging.exists()
    assert "staging" in actions[0]


def test_recovery_is_a_noop_without_leftovers(dest_root):
    install_from_tarball(repo_tarball(), dest_root, None)

    assert recover_stale_installs(dest_root) == []
    assert recover_stale_installs(dest_root / "missing") == []


# --- Folder names ----------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["my-int", "My_Int", "with space", "", ".hidden", "a/b"]
)
def test_rejects_folder_names_that_are_not_domains(dest_root, name):
    with pytest.raises(InstallerError, match="folder name"):
        validate_integration_name(name)


@pytest.mark.parametrize("name", ["foo", "foo_bar", "foo2", "hacs_extra"])
def test_accepts_valid_folder_names(name):
    validate_integration_name(name)


def test_a_dashed_folder_is_never_extracted(dest_root):
    tarball = repo_tarball(domain="my-int")

    with pytest.raises(InstallerError, match="folder name"):
        install_from_tarball(tarball, dest_root, None)

    assert list(dest_root.iterdir()) == []


def test_installed_folder_is_not_world_readable(dest_root):
    _, path = install_from_tarball(repo_tarball(), dest_root, None)

    assert path.stat().st_mode & 0o007 == 0


def test_recovery_leaves_foreign_backup_named_folders_alone(dest_root):
    """A folder that is not a backup we could have created is not touched."""
    foreign = dest_root / ".private_integrations-backup"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("not ours")
    dotted = dest_root / "..private_integrations-backup"
    dotted.mkdir()
    (dotted / "keep.txt").write_text("not ours either")

    assert recover_stale_installs(dest_root) == []
    assert (foreign / "keep.txt").is_file()
    assert (dotted / "keep.txt").is_file()


def test_recovery_does_not_move_symlinks(dest_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("outside")
    (dest_root / ".foo.private_integrations-backup").symlink_to(
        outside, target_is_directory=True
    )

    assert recover_stale_installs(dest_root) == []
    assert not (dest_root / "foo").exists()
    assert (outside / "keep.txt").is_file()


def test_recovery_does_not_remove_staging_symlinks(dest_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("outside")
    staging = dest_root / f"{STAGING_PREFIX}abcd"
    staging.symlink_to(outside, target_is_directory=True)

    assert recover_stale_installs(dest_root) == []
    assert staging.is_symlink()
    assert (outside / "keep.txt").is_file()


def test_marker_is_installed_together_with_the_files(dest_root):
    """The ownership marker lands in the same rename as the code."""
    marker = {"full_name": "owner/foo", "installed_sha": "a" * 40}

    _, path = install_from_tarball(
        repo_tarball(), dest_root, None, marker=marker
    )

    assert read_marker(path) == marker


def test_rejected_install_writes_no_marker(dest_root, monkeypatch):
    monkeypatch.setattr(installer, "MAX_EXTRACTED_BYTES", 4096)
    marker = {"full_name": "owner/foo"}
    tarball = repo_tarball(
        extra=[("top/custom_components/foo/blob.bin", b"x" * 8192)]
    )

    with pytest.raises(InstallerError, match="expands to more than"):
        install_from_tarball(tarball, dest_root, None, marker=marker)

    assert list(dest_root.iterdir()) == []


# --- Rejected archives ------------------------------------------------------


def test_two_top_level_directories_are_rejected(dest_root):
    tarball = make_tarball(
        [
            ("a/custom_components/foo/manifest.json", manifest("foo")),
            ("b/custom_components/foo/manifest.json", manifest("foo")),
        ]
    )

    with pytest.raises(InstallerError, match="single top-level folder"):
        install_from_tarball(tarball, dest_root, None)


def test_a_repo_without_custom_components_is_rejected(dest_root):
    tarball = make_tarball([("top/README.md", b"readme")])

    with pytest.raises(InstallerError, match="nothing to install"):
        install_from_tarball(tarball, dest_root, None)


def test_several_integrations_need_a_name(dest_root):
    tarball = make_tarball(
        [
            ("top/custom_components/foo/manifest.json", manifest("foo")),
            ("top/custom_components/bar/manifest.json", manifest("bar")),
        ]
    )

    with pytest.raises(InstallerError, match="2 integrations"):
        install_from_tarball(tarball, dest_root, None)


def test_an_unknown_integration_name_is_rejected(dest_root):
    with pytest.raises(InstallerError, match="not found in repo"):
        install_from_tarball(repo_tarball(), dest_root, "nope")


def test_a_protected_integration_is_rejected(dest_root):
    tarball = repo_tarball(domain="hacs")

    with pytest.raises(InstallerError, match="protected"):
        install_from_tarball(tarball, dest_root, "hacs")


@pytest.mark.parametrize(
    ("manifest_body", "message"),
    [
        (b"not json", "not valid JSON"),
        (b'["a list"]', "not an object"),
        (b'{"version": "1.0.0"}', "no 'domain'"),
        (b'{"domain": "bar"}', "does not match folder"),
        (b'{"domain": "foo", "version": 2}', "must be a string"),
    ],
)
def test_invalid_manifests_are_rejected(dest_root, manifest_body, message):
    tarball = make_tarball(
        [
            ("top", None),
            ("top/custom_components/foo/manifest.json", manifest_body),
        ]
    )

    with pytest.raises(InstallerError, match=message):
        install_from_tarball(tarball, dest_root, None)

    assert list(dest_root.iterdir()) == []


def test_a_manifest_that_is_missing_is_rejected(dest_root):
    tarball = make_tarball(
        [("top", None), ("top/custom_components/foo/__init__.py", b"")]
    )

    with pytest.raises(InstallerError, match="manifest.json is missing"):
        install_from_tarball(tarball, dest_root, None)


def test_path_traversal_members_are_blocked(dest_root):
    tarball = make_tarball(
        [
            ("top", None),
            ("top/custom_components/foo/manifest.json", manifest("foo")),
            ("top/custom_components/foo/../../../evil.py", b"boom"),
        ]
    )

    with pytest.raises(InstallerError, match="Path traversal blocked"):
        install_from_tarball(tarball, dest_root, None)

    assert not (dest_root.parent / "evil.py").exists()


def test_symlinks_inside_the_archive_are_skipped(dest_root):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in (
            ("top", None),
            ("top/custom_components/foo/manifest.json", manifest("foo")),
        ):
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        link = tarfile.TarInfo("top/custom_components/foo/evil")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)

    _, path = install_from_tarball(buffer.getvalue(), dest_root, None)

    assert not (path / "evil").exists()


def test_a_failed_swap_keeps_the_previous_version(dest_root, monkeypatch):
    install_from_tarball(repo_tarball(), dest_root, None)
    installed = (dest_root / "foo" / "__init__.py").read_text()

    real_replace = installer.os.replace

    def failing_replace(src, dst):
        if ".private_integrations-staging-" in str(src):
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(installer.os, "replace", failing_replace)

    with pytest.raises(OSError):
        install_from_tarball(repo_tarball(version="2.0.0"), dest_root, None)

    assert (dest_root / "foo" / "__init__.py").read_text() == installed
    assert sorted(p.name for p in dest_root.iterdir()) == ["foo"]


# --- Removal ----------------------------------------------------------------


def test_removal_refuses_to_leave_the_root(dest_root):
    with pytest.raises(InstallerError, match="folder name"):
        remove_integration(dest_root, "../evil")


def test_removal_of_a_missing_folder_reports_it(dest_root):
    assert remove_integration(dest_root, "foo") is False


def test_recovery_ignores_a_file_at_the_root(tmp_path):
    path = tmp_path / "custom_components"
    path.write_text("not a directory")

    assert recover_stale_installs(path) == []


def test_empty_member_names_are_skipped(dest_root):
    tarball = make_tarball(
        [
            ("top", None),
            ("top/custom_components/", None),
            ("top/custom_components/foo/", None),
            ("top/custom_components/foo/manifest.json", manifest("foo")),
            ("top/custom_components/foo/translations/", None),
            ("top/custom_components/foo/translations/en.json", b"{}"),
        ]
    )

    name, path = install_from_tarball(tarball, dest_root, None)

    assert name == "foo"
    assert (path / "translations" / "en.json").is_file()


def test_an_existing_backup_is_replaced(dest_root):
    install_from_tarball(repo_tarball(), dest_root, None)
    backup = dest_root / ".foo.private_integrations-backup"
    backup.mkdir()
    (backup / "stale.txt").write_text("stale")

    install_from_tarball(repo_tarball(version="2.0.0"), dest_root, None)

    assert not backup.exists()
    assert json.loads((dest_root / "foo" / "manifest.json").read_text())[
        "version"
    ] == "2.0.0"


def test_recovery_reports_an_entry_it_cannot_handle(dest_root, monkeypatch):
    backup = dest_root / ".foo.private_integrations-backup"
    backup.mkdir()

    def failing_replace(src, dst):
        raise OSError("boom")

    monkeypatch.setattr(installer.os, "replace", failing_replace)

    assert recover_stale_installs(dest_root) == []
    assert backup.is_dir()


def test_a_body_that_is_not_gzip_at_all_is_rejected(dest_root):
    with pytest.raises(InstallerError, match="Could not read the archive"):
        install_from_tarball(b"no", dest_root, None)

    assert list(dest_root.iterdir()) == []


def test_a_file_under_custom_components_is_not_an_integration():
    """Only folders are integrations; a README next to them is not one.

    Counting files made a perfectly normal repository look like it contained two
    integrations, and the install asked the user to pick one of them.
    """
    tarball = make_tarball(
        [
            ("top/custom_components/README.md", b"# integrations live here"),
            ("top/custom_components/foo/manifest.json", manifest("foo")),
        ]
    )

    assert list_integrations(tarball) == ["foo"]


def test_a_stray_file_does_not_block_the_install(dest_root):
    tarball = make_tarball(
        [
            ("top/custom_components/README.md", b"# integrations live here"),
            ("top/custom_components/foo/manifest.json", manifest("foo")),
        ]
    )

    name, path = install_from_tarball(tarball, dest_root, None)

    assert name == "foo"
    assert (path / "manifest.json").is_file()


def test_installed_integrations_lists_only_real_integrations(tmp_path):
    """``custom_components/`` also holds junk; a manifest is what counts."""
    from custom_components.private_integrations.installer import (
        installed_integrations,
    )

    for name in ("alpha", "beta"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "manifest.json").write_text("{}")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / ".hidden").mkdir()

    assert installed_integrations(tmp_path) == ["alpha", "beta"]
    assert installed_integrations(tmp_path / "missing") == []
