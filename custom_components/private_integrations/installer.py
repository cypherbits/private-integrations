"""Tarball extraction into HA's custom_components/ folder.

Extraction is staged first and swapped into place atomically, so a failed or
malformed archive can never destroy the integration version already installed.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import tarfile
import tempfile
import zlib
from pathlib import Path
from typing import Any

from .names import is_valid_integration_name
from .refs import parse_repo_reference

_LOGGER = logging.getLogger(__name__)

#: Marker written inside every integration folder we install, so later installs
#: and removals can tell our files apart from core/third-party ones.
MARKER_FILENAME = ".private_integrations.json"
STAGING_PREFIX = ".private_integrations-staging-"

#: Ceiling for what a downloaded archive may expand to. The compressed size is
#: capped when downloading, which says nothing about the extracted size: a few
#: KiB of gzip data can expand to gigabytes of zeros.
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
#: An archive can stay under the byte ceiling with countless empty entries.
MAX_ARCHIVE_MEMBERS = 50_000
EXTRACT_CHUNK_SIZE = 64 * 1024
#: Suffix of the copy kept while the new version is being swapped in.
BACKUP_SUFFIX = ".private_integrations-backup"

#: Integrations this tool must never overwrite or delete. ``private_hacs`` is
#: the folder of the previous name: it belongs to the old integration and is
#: only removed by the user after the import.
PROTECTED_INTEGRATIONS = frozenset(
    {"hacs", "homeassistant", "marketplace", "private_hacs", "private_integrations"}
)

# Home Assistant imports custom_components/<domain> as a Python module and uses
# the folder name as the integration domain; the rule lives in names.py.


class InstallerError(Exception):
    """Raised on extraction problems."""


def _is_within(base: Path, target: Path) -> bool:
    """Reject paths that escape `base` after resolution."""
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _open_archive(tarball_bytes: bytes) -> tuple[tarfile.TarFile, list]:
    """Open a gzipped tarball, reporting anything that is not one as an error.

    GitHub answers some failures with an HTML page or a truncated body, so this
    gets whatever was downloaded: ``tarfile`` raises its own exceptions for that,
    and a body too short to even be gzip raises ``gzip.BadGzipFile`` (an
    OSError). None of them are ours, so without this they escape the installer
    as an unhandled crash.
    """
    try:
        archive = tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz")
        return archive, archive.getmembers()
    except (tarfile.TarError, EOFError, OSError, zlib.error) as err:
        raise InstallerError(f"Could not read the archive: {err}") from err


def _top_dir(members: list[tarfile.TarInfo]) -> str:
    top_dirs = {m.name.split("/", 1)[0] for m in members if m.name}
    if len(top_dirs) != 1:
        raise InstallerError(
            "The archive does not have a single top-level folder, which is what "
            "GitHub tarballs look like; is this a GitHub release archive?"
        )
    return next(iter(top_dirs))


def _integration_names(members: list[tarfile.TarInfo], cc_prefix: str) -> list[str]:
    """Return the folders directly under custom_components/.

    A path without a slash after the prefix is a file that happens to live there
    -- a README, a requirements file -- and not an integration. Counting those
    made a normal repository look like it held two integrations, so the install
    asked which one to use when there was only one.
    """
    names: set[str] = set()
    for m in members:
        if not m.name.startswith(cc_prefix):
            continue
        rel = m.name[len(cc_prefix):]
        if "/" not in rel:
            continue
        first = rel.split("/", 1)[0]
        if first:
            names.add(first)
    return sorted(names)


def list_integrations(tarball_bytes: bytes) -> list[str]:
    """Return integration directory names found under custom_components/."""
    tf, members = _open_archive(tarball_bytes)
    with tf:
        _check_member_count(members)
        top = _top_dir(members)
        return _integration_names(members, f"{top}/custom_components/")


def validate_integration_name(integration_name: str) -> None:
    """Reject folder names Home Assistant cannot use as a domain."""
    if integration_name in PROTECTED_INTEGRATIONS:
        raise InstallerError(
            f"Refusing to touch the protected integration '{integration_name}'"
        )
    if not is_valid_integration_name(integration_name):
        raise InstallerError(
            f"Invalid integration folder name: {integration_name!r}; Home "
            "Assistant domains are lowercase letters, digits and underscores"
        )


def _validate_manifest(directory: Path, integration_name: str) -> None:
    """Validate the extracted `manifest.json` before it goes live."""
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise InstallerError(
            f"{integration_name}/manifest.json is missing; "
            "refusing to install an integration Home Assistant cannot load"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise InstallerError(
            f"{integration_name}/manifest.json is not valid JSON: {err}"
        ) from err
    if not isinstance(manifest, dict):
        raise InstallerError(f"{integration_name}/manifest.json is not an object")

    domain = manifest.get("domain")
    if not isinstance(domain, str) or not domain:
        raise InstallerError(
            f"{integration_name}/manifest.json has no 'domain'"
        )
    if domain != integration_name:
        raise InstallerError(
            f"manifest domain {domain!r} does not match folder "
            f"{integration_name!r}"
        )
    version = manifest.get("version")
    if version is not None and not isinstance(version, str):
        raise InstallerError(
            f"{integration_name}/manifest.json 'version' must be a string"
        )


def _extract_members(
    tf: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    target_prefix: str,
    staging: Path,
) -> None:
    extracted = 0
    for m in members:
        if not m.name.startswith(target_prefix):
            continue
        rel = m.name[len(target_prefix):]
        if not rel:
            continue
        out = staging / rel
        if not _is_within(staging, out):
            raise InstallerError(f"Path traversal blocked: {m.name}")
        if m.isdir():
            out.mkdir(parents=True, exist_ok=True)
        elif m.isfile():
            if extracted + m.size > MAX_EXTRACTED_BYTES:
                raise InstallerError(
                    "Archive expands to more than "
                    f"{MAX_EXTRACTED_BYTES // (1024 * 1024)} MiB; refusing to "
                    "extract it"
                )
            out.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with open(out, "wb") as w:
                while chunk := src.read(EXTRACT_CHUNK_SIZE):
                    extracted += len(chunk)
                    if extracted > MAX_EXTRACTED_BYTES:
                        raise InstallerError(
                            "Archive expands to more than "
                            f"{MAX_EXTRACTED_BYTES // (1024 * 1024)} MiB; "
                            "refusing to extract it"
                        )
                    w.write(chunk)
        elif m.issym() or m.islnk():
            # Skip symlinks for safety.
            continue


def _check_member_count(members: list[tarfile.TarInfo]) -> None:
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise InstallerError(
            f"Archive has {len(members)} entries (limit {MAX_ARCHIVE_MEMBERS}); "
            "refusing to process it"
        )


def _backup_path(dest_path: Path) -> Path:
    """Where the previous version is parked while the new one is swapped in."""
    return dest_path.with_name(f".{dest_path.name}{BACKUP_SUFFIX}")


def _swap_into_place(staging: Path, dest_path: Path) -> None:
    """Replace `dest_path` with `staging`, restoring the old copy on failure."""
    backup: Path | None = None
    if dest_path.exists():
        backup = _backup_path(dest_path)
        if backup.exists():
            shutil.rmtree(backup)
        os.replace(dest_path, backup)
    try:
        os.replace(staging, dest_path)
    except OSError:
        if backup is not None:
            os.replace(backup, dest_path)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def install_from_tarball(
    tarball_bytes: bytes,
    dest_root: Path,
    integration_name: str | None,
    *,
    allow_replace: bool = True,
    marker: dict[str, Any] | None = None,
) -> tuple[str, Path]:
    """Extract `custom_components/<integration_name>/` into dest_root.

    If integration_name is None, the repo must contain exactly one integration.
    With `allow_replace=False` an existing destination aborts the install, which
    is how the manager protects integrations installed by someone else.

    The archive is extracted into a staging folder, validated, and only then
    renamed into place, so failures leave the previous version untouched. The
    ownership `marker` is written into that staging folder for the same reason:
    the folder and the record of who installed it land together, so a crash can
    never leave one without the other.

    Returns (integration_name, dest_path).
    """
    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)

    tf, members = _open_archive(tarball_bytes)
    with tf:
        _check_member_count(members)
        top = _top_dir(members)
        cc_prefix = f"{top}/custom_components/"
        names = _integration_names(members, cc_prefix)

        if integration_name:
            validate_integration_name(integration_name)
            if integration_name not in names:
                raise InstallerError(
                    f"Integration '{integration_name}' not found in repo "
                    f"(found: {names})"
                )

        if not integration_name:
            # Same wording as the manager: whoever reads this is looking at a
            # repository that does not have what an integration needs.
            if not names:
                raise InstallerError(
                    "The repository has no custom_components/<integration>/ "
                    "folder, so there is nothing to install"
                )
            if len(names) > 1:
                raise InstallerError(
                    f"The repository has {len(names)} integrations: {names}; "
                    "specify integration_name"
                )
            integration_name = names[0]

        validate_integration_name(integration_name)

        target_prefix = f"{cc_prefix}{integration_name}/"
        dest_path = dest_root / integration_name
        if not _is_within(dest_root, dest_path):
            raise InstallerError(
                "Refusing to install outside custom_components"
            )
        if dest_path.exists() and not allow_replace:
            raise InstallerError(
                f"custom_components/{integration_name} already exists and is not "
                "managed by Private Integrations"
            )

        staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=dest_root))
        try:
            try:
                _extract_members(tf, members, target_prefix, staging)
            except (tarfile.TarError, EOFError, zlib.error) as err:
                raise InstallerError(f"Could not read the archive: {err}") from err
            _validate_manifest(staging, integration_name)
            if marker is not None:
                write_marker(staging, marker)
            # mkdtemp creates 0o700. Widen it to group read, no more: Home
            # Assistant runs as the same user that installs the files.
            os.chmod(staging, 0o750)  # nosec B103
            _swap_into_place(staging, dest_path)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    return integration_name, dest_path


def remove_integration(dest_root: Path, integration_name: str) -> bool:
    validate_integration_name(integration_name)
    target = dest_root / integration_name
    if not _is_within(dest_root, target):
        raise InstallerError(
            "Refusing to remove outside custom_components"
        )
    if target.exists():
        shutil.rmtree(target)
        return True
    return False


def recover_stale_installs(dest_root: Path) -> list[str]:
    """Clean up after installs that were interrupted mid-swap.

    Swapping a new version in takes two renames: the installed folder is parked
    as a backup and the staged one takes its place. A crash between them leaves
    the integration missing from disk, with its previous version sitting in a
    hidden backup folder that nothing would ever look at again. Restore those,
    and drop the leftovers that are no longer needed.

    Only real directories are touched: a symlink in `custom_components/` is
    somebody else's business, and moving one would publish whatever it points
    at as an installed integration. Anything unexpected is skipped and logged
    instead of raised, because this runs during setup and must never be the
    reason Home Assistant fails to start.

    Returns a human readable list of what was done, for the caller to log.
    """
    if not dest_root.is_dir():
        return []

    actions: list[str] = []
    for entry in sorted(dest_root.iterdir()):
        try:
            actions.extend(_recover_entry(entry))
        except (OSError, ValueError) as err:
            _LOGGER.warning(
                "Private Integrations: skipping %s during recovery: %s", entry.name, err
            )
    return actions


def _recover_entry(entry: Path) -> list[str]:
    """Handle one entry of custom_components/, if it is one of our leftovers."""
    name = entry.name
    if name.startswith(STAGING_PREFIX):
        if not _is_real_dir(entry):
            _LOGGER.warning(
                "Private Integrations: not removing staging entry %s, it is not a "
                "directory",
                name,
            )
            return []
        shutil.rmtree(entry, ignore_errors=True)
        return [f"removed the stale staging folder {name}"]

    if not (name.startswith(".") and name.endswith(BACKUP_SUFFIX)):
        return []

    integration = name[1 : -len(BACKUP_SUFFIX)]
    if not integration or not is_valid_integration_name(integration):
        # Not a backup we could have created; leave it alone.
        return []
    if integration in PROTECTED_INTEGRATIONS:
        _LOGGER.warning(
            "Private Integrations: not restoring %s, %s is a protected "
            "integration",
            name,
            integration,
        )
        return []
    if not _is_real_dir(entry):
        _LOGGER.warning(
            "Private Integrations: not restoring %s, it is not a directory", name
        )
        return []

    original = entry.with_name(integration)
    if original.exists():
        # The swap did complete, the backup is just leftovers.
        shutil.rmtree(entry, ignore_errors=True)
        return [f"removed the stale backup of {original.name}"]

    os.replace(entry, original)
    return [f"restored {original.name} from an interrupted install"]


def _is_real_dir(path: Path) -> bool:
    """True for a directory that is not a symlink to one."""
    return path.is_dir() and not path.is_symlink()


def read_marker(dest_path: Path) -> dict[str, Any] | None:
    """Return the ownership marker of an installed integration, if any."""
    try:
        data = json.loads((dest_path / MARKER_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def installed_integrations(dest_root: Path) -> list[str]:
    """Integrations present under ``custom_components/``, sorted.

    A folder counts when it carries a ``manifest.json``, which is what Home
    Assistant needs to load it; ``custom_components/`` also holds the odd
    ``__pycache__`` or scratch folder, and those are not integrations. Used to
    tell the user which integrations are on disk but not managed here (installed
    by the Marketplace, HACS or by hand). A missing or unreadable directory is
    an empty list: this decorates a screen, it never blocks one.
    """
    try:
        entries = list(Path(dest_root).iterdir())
    except OSError:
        return []
    return sorted(
        entry.name
        for entry in entries
        if _is_real_dir(entry)
        and not entry.name.startswith(".")
        and (entry / "manifest.json").is_file()
    )


def installed_owner(dest_path: Path) -> str | None:
    """Return the repository recorded as owner of an integration folder.

    ``None`` means the folder carries no usable ownership marker, so it cannot
    be attributed to any tracked repository. The name is parsed as a repository
    reference: a hand-edited marker with whitespace or a path in ``full_name``
    must not turn an unrelated folder into ours.
    """
    marker = read_marker(dest_path)
    if marker is None:
        return None
    owner = marker.get("full_name")
    if not isinstance(owner, str):
        return None
    try:
        return parse_repo_reference(owner)
    except ValueError:
        return None


def installed_manifest_version(
    dest_root: Path, integration_name: str | None
) -> str | None:
    """Version recorded by the installed integration's manifest, if any.

    ``installed_version`` in the registry is the ref the repository follows
    (``main``, a tag, a short SHA), not the version the integration declares.
    The list and the restart notification want the latter, so they read it from
    the folder that was actually installed.
    """
    if not integration_name or not is_valid_integration_name(integration_name):
        return None
    manifest_path = Path(dest_root) / integration_name / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return version if isinstance(version, str) and version else None


def write_marker(dest_path: Path, data: dict[str, Any]) -> None:
    """Record which repository/version owns this integration folder."""
    (dest_path / MARKER_FILENAME).write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
