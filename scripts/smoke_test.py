"""End-to-end check of Private Integrations on a real Home Assistant.

The pytest suite runs on the Home Assistant version that
pytest-homeassistant-custom-component pins, which lags behind: 2026.11 was
released before the harness supported it. This script boots the Home Assistant
installed in the current interpreter (whatever version that is), with the
integration copied into its `custom_components/`, and walks the whole surface:
discovery, config flow, entry setup, services, options flow (the list and the
search of the account included), install, restart notification, removal and
unload.

    python scripts/smoke_test.py [
        --repo PATH] [--config-dir PATH] [--keep] [--skip-pip]

It exits non-zero on the first failed check, so CI can run it against the latest
Home Assistant without waiting for the test harness.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import pathlib
import shutil
import sys
import tarfile
from unittest.mock import patch

from homeassistant import bootstrap, loader
from homeassistant.components import persistent_notification
from homeassistant.runner import RuntimeConfig

TOKEN = "ghp_smoke"
USER = {"login": "smoke"}
SHA = "b" * 40
INTEGRATION = "private_integrations"
INSTALLED = "smoke_thing"


class Report:
    """Collect the checks so a failure is listed once, at the end."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def __call__(self, condition: bool, label: str, detail: object = "") -> None:
        if condition:
            print(f"  ok   {label}")
            return
        print(f"  FAIL {label} {detail}")
        self.failures.append(label)


def make_tarball(domain: str = INSTALLED) -> bytes:
    """A GitHub-style tarball holding one minimal integration."""
    manifest = json.dumps({"domain": domain, "version": "1.0.0"}).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(f"top/custom_components/{domain}/manifest.json")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
    return buffer.getvalue()


def notification_ids(hass) -> set[str]:
    """Ids of the persistent notifications currently shown.

    There is no public accessor; failing loudly is better than silently
    reporting a check that never ran.
    """
    getter = getattr(
        persistent_notification, "_async_get_or_create_notifications", None
    )
    if getter is None:
        raise RuntimeError(
            "this Home Assistant has no way to inspect persistent notifications"
        )
    return set(getter(hass))


def mount_custom_components(config_dir: pathlib.Path) -> None:
    """Make `custom_components` importable, as Home Assistant does internally.

    The helper that wraps this (`async_mount_config_dir`) is private in 2026.10
    and public in 2026.11, so it is done here instead, and the path stays: the
    process ends when the check does.
    """
    if str(config_dir) not in sys.path:
        sys.path.insert(0, str(config_dir))
    import custom_components  # noqa: F401


async def check_everything(
    repo: pathlib.Path, config_dir: pathlib.Path, *, skip_pip: bool = False
) -> int:
    check = Report()

    if config_dir.exists():
        shutil.rmtree(config_dir)
    (config_dir / "custom_components").mkdir(parents=True)
    shutil.copytree(
        repo / "custom_components" / INTEGRATION,
        config_dir / "custom_components" / INTEGRATION,
    )
    (config_dir / "configuration.yaml").write_text(
        "homeassistant:\n  time_zone: UTC\n", encoding="utf-8"
    )

    mount_custom_components(config_dir)
    hass = await bootstrap.async_setup_hass(
        RuntimeConfig(config_dir=str(config_dir), skip_pip=skip_pip)
    )
    if hass is None:
        print("Home Assistant did not start")
        return 1
    await hass.async_start()
    try:
        from homeassistant.const import __version__

        print(
            f"Home Assistant {__version__} on Python "
            f"{sys.version.split()[0]}\n"
        )

        print("1. the integration is discovered")
        integration = await loader.async_get_integration(hass, INTEGRATION)
        check(integration.version is not None, f"{INTEGRATION} is known")
        check(integration.config_flow is True, "it advertises a config flow")

        print("2. brand assets (served from the custom integration since 2026.3)")
        check(
            integration.has_branding,
            "the loader sees a brand/ folder",
        )
        brand_dir = pathlib.Path(integration.file_path) / "brand"
        for image in (
            "icon.png",
            "icon@2x.png",
            "dark_icon.png",
            "dark_icon@2x.png",
            "logo.png",
            "logo@2x.png",
            "dark_logo.png",
            "dark_logo@2x.png",
        ):
            check((brand_dir / image).is_file(), f"brand/{image}")

        print("3. the config flow")
        result = await hass.config_entries.flow.async_init(
            INTEGRATION, context={"source": "user"}
        )
        check(result["type"].value == "form", "the token step is a form", result)
        check(
            "github_token" in result["data_schema"].schema,
            "the form asks for a token",
        )
        # A real call to GitHub with a token that cannot work: the error mapping
        # has to survive whatever this Home Assistant version changed.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"github_token": "not-a-real-token"}
        )
        check(
            result.get("errors") == {"base": "invalid_auth"},
            "a bad token is reported as an auth problem",
            result.get("errors"),
        )

        print("4. setting up an entry")
        # The same flow continues: a second one aborts with "already in progress",
        # which is Home Assistant's own guard.
        with patch(
            f"custom_components.{INTEGRATION}.GitHubClient.verify_token",
            return_value=USER,
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"github_token": TOKEN}
            )
            await hass.async_block_till_done()
        check(
            result["type"].value == "create_entry",
            "a valid token creates the entry",
            result["type"],
        )
        entry = hass.config_entries.async_entries(INTEGRATION)[0]
        check(entry.state.value == "loaded", "the entry loads", entry.state.value)

        print("5. services are registered")
        for service in ("add_repository", "remove_repository", "install", "update"):
            check(
                hass.services.has_service(INTEGRATION, service),
                f"{INTEGRATION}.{service}",
            )

        print("6. the options flow")
        result = await hass.config_entries.options.async_init(entry.entry_id)
        check(
            result["type"].value == "menu", "the options flow shows a menu", result
        )
        check(
            result["menu_options"]
            == [
                "manage_repository",
                "add_repository",
                "discover",
                "update_token",
            ],
            "with the four entries",
            result["menu_options"],
        )

        print("7. adding and installing a repository")
        manager = hass.data[INTEGRATION][entry.entry_id]["manager"]
        with (
            patch(
                f"custom_components.{INTEGRATION}.GitHubClient.get_repo",
                return_value={"default_branch": "trunk"},
            ),
            patch(
                f"custom_components.{INTEGRATION}.GitHubClient.get_commit_sha",
                return_value=SHA,
            ),
            patch(
                f"custom_components.{INTEGRATION}.GitHubClient.download_tarball",
                return_value=make_tarball(),
            ),
        ):
            repo_entry = await manager.add_repository(f"owner/{INSTALLED}")
            check(
                repo_entry.ref == "trunk",
                "the default branch is picked",
                repo_entry.ref,
            )
            first_install = await manager.install_or_update(f"owner/{INSTALLED}")
            check(first_install.changed, "the first install writes files")

        folder = config_dir / "custom_components" / INSTALLED
        check(folder.is_dir(), "the files are on disk")
        marker = folder / ".private_integrations.json"
        check(marker.is_file(), "the ownership marker is written")
        if marker.is_file():
            check(
                json.loads(marker.read_text())["installed_sha"] == SHA,
                "the marker records the commit",
            )
        check(
            any(INSTALLED in key for key in notification_ids(hass)),
            "a restart notification is created",
        )

        print("8. the list and the discovery")
        listed = await hass.config_entries.options.async_init(entry.entry_id)
        listed = await hass.config_entries.options.async_configure(
            listed["flow_id"], {"next_step_id": "manage_repository"}
        )
        check(listed["step_id"] == "manage_repository", "the list opens", listed)
        selector = next(iter(listed["data_schema"].schema.values()))
        check(
            selector.config.get("mode") == "list",
            "the list is a list, not a dropdown",
        )
        row = selector.config["options"][0]["label"]
        check(INSTALLED in row, "the row names the integration folder", row)
        repo_entry = manager.store.get(f"owner/{INSTALLED}")
        check(
            bool(repo_entry and repo_entry.installed_version)
            and repo_entry.installed_version in row,
            "and says what is installed",
            row,
        )

        from custom_components.private_integrations.catalog import (
            Candidate,
            Discovery,
        )

        with patch(
            f"custom_components.{INTEGRATION}.config_flow.async_detect_integrations",
            return_value=Discovery(
                candidates=[
                    Candidate(
                        full_name=f"owner/{INSTALLED}",
                        integration_name=INSTALLED,
                        version="1.0.0",
                        private=True,
                    )
                ],
                inspected=1,
                listed=1,
            ),
        ):
            found = await hass.config_entries.options.async_init(entry.entry_id)
            found = await hass.config_entries.options.async_configure(
                found["flow_id"], {"next_step_id": "discover"}
            )
            check(
                found["step_id"] == "proposals",
                "the search offers candidates",
                found,
            )
            option = next(iter(found["data_schema"].schema.values())).config[
                "options"
            ][0]
            check(
                option["value"] == f"owner/{INSTALLED}::{INSTALLED}",
                "the value is unique",
                option,
            )
            check("private" in option["label"], "the row says it is private", option)
            chosen = await hass.config_entries.options.async_configure(
                found["flow_id"], {"full_name": option["value"]}
            )
            check(chosen["step_id"] == "proposal", "picking one confirms it", chosen)
            check(
                chosen["description_placeholders"]["integration"] == INSTALLED,
                "the confirmation names the integration",
            )

        print("9. a second install changes nothing")
        with patch(
            f"custom_components.{INTEGRATION}.GitHubClient.get_commit_sha",
            return_value=SHA,
        ):
            second_install = await manager.install_or_update(f"owner/{INSTALLED}")
        check(not second_install.changed, "a repeated install changes nothing")
        check(folder.is_dir(), "the folder is still there")

        print("10. removing it")
        await manager.remove_repository(f"owner/{INSTALLED}")
        check(not folder.exists(), "the folder is deleted")
        check(
            manager.store.get(f"owner/{INSTALLED}") is None, "the entry is gone"
        )
        check(
            not any(INSTALLED in key for key in notification_ids(hass)),
            "the restart notification is gone",
        )

        print("11. no leftover staging or backup folders")
        leftovers = [
            path.name
            for path in (config_dir / "custom_components").iterdir()
            if path.name.startswith(".")
        ]
        check(leftovers == [], "custom_components/ is clean", leftovers)

        print("12. unloading is clean")
        await hass.config_entries.async_unload(entry.entry_id)
        check(
            not hass.services.has_service(INTEGRATION, "install"),
            "the services are removed with the entry",
        )
    finally:
        await hass.async_stop()

    print()
    if check.failures:
        print(f"{len(check.failures)} check(s) failed: {check.failures}")
        return 1
    print("all checks passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parent.parent,
        help="root of the integration repository",
    )
    parser.add_argument(
        "--config-dir",
        type=pathlib.Path,
        default=pathlib.Path("/tmp/private_integrations_smoke"),
        help="where to build the temporary Home Assistant config",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the generated config directory in place",
    )
    parser.add_argument(
        "--skip-pip",
        action="store_true",
        help=(
            "do not let Home Assistant install the requirements of its own "
            "integrations; without them it can fall into recovery mode, where "
            "custom integrations are not loaded at all"
        ),
    )
    args = parser.parse_args()

    exit_code = asyncio.run(
        check_everything(args.repo, args.config_dir, skip_pip=args.skip_pip)
    )
    if not args.keep and args.config_dir.exists():
        shutil.rmtree(args.config_dir, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
