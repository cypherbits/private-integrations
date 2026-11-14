"""Config flow + options flow.

The config flow runs once during initial setup (token entry).
The options flow ("Configure" button) is the day-to-day UI for managing repos.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    UnknownEntry,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .catalog import (
    MAX_MANIFESTS,
    MAX_REPOSITORIES,
    Candidate,
    Discovery,
    async_detect_integrations,
)
from .const import (
    ACTION_CHANGE_REF,
    ACTION_FORCE_INSTALL,
    ACTION_INSTALL,
    ACTION_REMOVE,
    CONF_ACTION,
    CONF_DELETE_FILES,
    CONF_FULL_NAME,
    CONF_GITHUB_TOKEN,
    CONF_INSTALL_NOW,
    CONF_INTEGRATION_NAME,
    CONF_REF,
    CONF_REF_TYPE,
    DOMAIN,
    OLD_DOMAIN,
    REF_TYPE_BRANCH,
    REF_TYPE_RELEASE,
    VALID_REF_TYPES,
)
from .github import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubRateLimitError,
)
from .installer import (
    PROTECTED_INTEGRATIONS,
    installed_integrations,
    installed_manifest_version,
    installed_owner,
)
from .localize import async_translate
from .manager import get_manager
from .repository import Repository

_LOGGER = logging.getLogger(__name__)

CLASSIC_PAT_URL = (
    "https://github.com/settings/tokens/new"
    "?scopes=repo&description=Private+Integrations"
)
FINE_GRAINED_PAT_URL = "https://github.com/settings/personal-access-tokens/new"
TOKEN_PLACEHOLDERS = {
    "classic_url": CLASSIC_PAT_URL,
    "fine_url": FINE_GRAINED_PAT_URL,
}

#: A discovery makes up to 140 requests. With a broken connection each one can
#: take its own timeout, so the whole lookup gets a ceiling.
DISCOVERY_TIMEOUT = 120


def _token_schema() -> vol.Schema:
    """Form schema for a Personal Access Token.

    Three steps ask for one (initial setup, reauthentication, token rotation);
    they must look and validate the same.
    """
    return vol.Schema(
        {
            vol.Required(CONF_GITHUB_TOKEN): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            )
        }
    )


async def _verify_token(
    hass: HomeAssistant, token: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Ask GitHub about a token: (user payload, error key)."""
    client = GitHubClient(async_get_clientsession(hass), token)
    try:
        return await client.verify_token(), None
    except GitHubAuthError:
        return None, "invalid_auth"
    except GitHubError:
        return None, "cannot_connect"


def _old_config_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """Return the config entry of the previous Private HACS installation."""
    entries = hass.config_entries.async_entries(OLD_DOMAIN)
    return entries[0] if entries else None


class PrivateIntegrationsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup: collect a GitHub PAT."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None and _old_config_entry(self.hass) is not None:
            return await self.async_step_migrate()

        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()

            token = user_input[CONF_GITHUB_TOKEN].strip()
            user, error = await _verify_token(self.hass, token)
            if error or user is None:
                errors["base"] = error or "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"Private Integrations ({user.get('login', 'github')})",
                    data={CONF_GITHUB_TOKEN: token},
                )

        return self._show_token_form(errors)

    async def async_step_migrate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Import the GitHub token of a previous Private HACS installation."""
        old_entry = _old_config_entry(self.hass)
        if old_entry is None:
            return self._show_token_form()

        if user_input is not None:
            raw_token = old_entry.data.get(CONF_GITHUB_TOKEN, "")
            token = raw_token.strip() if isinstance(raw_token, str) else ""
            if not token:
                return self._show_token_form()
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            user, error = await _verify_token(self.hass, token)
            if error or user is None:
                return self._show_token_form(
                    {"base": error or "cannot_connect"}
                )
            return self.async_create_entry(
                title=f"Private Integrations ({user.get('login', 'github')})",
                data={CONF_GITHUB_TOKEN: token},
            )

        return self.async_show_form(
            step_id="migrate",
            data_schema=vol.Schema({}),
            description_placeholders={
                "title": old_entry.title or "Private HACS"
            },
        )

    def _show_token_form(
        self, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="user",
            data_schema=_token_schema(),
            errors=errors or {},
            description_placeholders=TOKEN_PLACEHOLDERS,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            token = user_input[CONF_GITHUB_TOKEN].strip()
            _, error = await _verify_token(self.hass, token)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_GITHUB_TOKEN: token}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_token_schema(),
            errors=errors,
            description_placeholders=TOKEN_PLACEHOLDERS,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PrivateIntegrationsOptionsFlow()


class PrivateIntegrationsOptionsFlow(OptionsFlow):
    """Configure-button UI: add/remove/install/update repos, change token."""

    def _entry(self) -> ConfigEntry:
        """Return this flow's config entry on every supported HA version.

        Home Assistant 2024.11 has no OptionsFlow.config_entry -- only
        OptionsFlowWithConfigEntry carries one -- while 2025.1 and later set it
        on the flow. For an options flow the handler *is* the entry id, which is
        how the core base class finds it on the old version too.
        """
        entry = getattr(self, "config_entry", None)
        if entry is not None:
            return entry
        entry = self.hass.config_entries.async_get_entry(self.handler)
        if entry is None:
            raise UnknownEntry(self.handler)
        return entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "manage_repository",
                "add_repository",
                "discover",
                "update_token",
            ],
        )

    # --- Add ----------------------------------------------------------------

    async def _add_and_install(
        self,
        *,
        full_name: str,
        ref_type: str,
        ref: str | None,
        integration_name: str | None,
        install_now: bool,
    ) -> ConfigFlowResult | None:
        """Track a repository and optionally install it, shared by both add paths.

        Returns the next screen, or ``None`` when adding failed -- the caller
        reports it -- so the manual form and the discovered-proposal screen
        cannot drift apart.
        """
        manager = get_manager(self.hass)
        try:
            repo = await manager.add_repository(
                full_name_or_url=full_name,
                ref_type=ref_type,
                ref=ref,
                integration_name=integration_name,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("add_repository failed for %r: %s", full_name, err)
            self._error_detail = str(err)
            return None

        self._installed_repo_name = repo.full_name
        if not install_now:
            # install_now was unchecked — repo registered, no install
            return await self.async_step_tracked_confirmation()
        try:
            result = await manager.install_or_update(repo.full_name)
        except HomeAssistantError as err:
            # Keep the repository tracked. It was added successfully -- the URL,
            # the token and the ref are fine -- and only the download or the
            # extraction failed. Untracking it meant retyping everything to
            # retry, and the usual causes (the wrong integration folder, a ref
            # that moved) are fixable from the manage screen.
            _LOGGER.warning("install_or_update failed for %s: %s", repo.full_name, err)
            self._error_detail = str(err)
            return await self.async_step_install_failed()
        if result.changed:
            return await self.async_step_installed_confirmation()
        return await self.async_step_up_to_date_confirmation()

    async def _async_install_blocker(
        self,
        full_name_or_url: str,
        integration_name: str | None,
        *,
        install_now: bool,
    ) -> str | None:
        """Why the install would be refused, before the repository is tracked.

        Only usable when the folder is known in advance (a discovered proposal,
        or the manual form with a folder name). A blank folder is resolved from
        the archive, which is only known after the download.
        """
        if not install_now or not integration_name:
            return None
        manager = get_manager(self.hass)
        try:
            full_name = manager.parse_repo_input(full_name_or_url)
        except HomeAssistantError:
            # Let add_repository report the malformed reference.
            return None
        return await manager.install_blocker(full_name, integration_name)

    async def async_step_add_repository(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        self._error_detail = ""

        if user_input is not None:
            install_now = user_input.get(CONF_INSTALL_NOW, True)
            integration_name = user_input.get(CONF_INTEGRATION_NAME) or None
            blocker = await self._async_install_blocker(
                user_input[CONF_FULL_NAME],
                integration_name,
                install_now=install_now,
            )
            if blocker is not None:
                self._error_detail = blocker
                errors["base"] = "add_failed"
            else:
                result = await self._add_and_install(
                    full_name=user_input[CONF_FULL_NAME],
                    ref_type=user_input[CONF_REF_TYPE],
                    ref=user_input.get(CONF_REF) or None,
                    integration_name=integration_name,
                    install_now=install_now,
                )
                if result is not None:
                    return result
                errors["base"] = "add_failed"

        schema = vol.Schema(
            {
                vol.Required(CONF_FULL_NAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL)
                ),
                vol.Required(
                    CONF_REF_TYPE, default=REF_TYPE_BRANCH
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=list(VALID_REF_TYPES),
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="ref_type",
                    )
                ),
                vol.Optional(CONF_REF, default=""): str,
                vol.Optional(CONF_INTEGRATION_NAME, default=""): str,
                vol.Optional(CONF_INSTALL_NOW, default=True): bool,
            }
        )
        return self.async_show_form(
            step_id="add_repository",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "detail": self._error_detail
            },
        )

    # --- Discover -----------------------------------------------------------

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Look for integrations in every repository the token can read.

        Nothing is cached: the answer depends on what the repositories look
        like right now, and it is the user who asked for the list.
        """
        if user_input is not None:
            # This screen is also the "nothing to offer / cannot ask GitHub"
            # result; submitting it goes back to the menu.
            return await self.async_step_init()

        manager = get_manager(self.hass)
        installed = await self.hass.async_add_executor_job(
            installed_integrations,
            Path(self.hass.config.path("custom_components")),
        )
        try:
            async with asyncio.timeout(DISCOVERY_TIMEOUT):
                discovery = await async_detect_integrations(
                    manager.client,
                    tracked={repo.full_name for repo in manager.store.all()},
                    installed=installed,
                )
        except TimeoutError:
            return self._show_discover("cannot_connect", "")
        except GitHubRateLimitError as err:
            return self._show_discover(
                "rate_limited", manager.rate_limit_message(err)
            )
        except GitHubAuthError:
            return self._show_discover("invalid_auth", "")
        except GitHubError as err:
            _LOGGER.warning("Discovery failed: %s", err)
            return self._show_discover("cannot_connect", "")

        if not discovery.candidates:
            if discovery.listed == 0:
                return self._show_discover("no_repositories_visible", "")
            if discovery.inspected and discovery.unreadable == discovery.inspected:
                return self._show_discover(
                    "not_readable", "", inspected=discovery.inspected
                )
            return self._show_discover(
                "no_candidates", "", inspected=discovery.inspected
            )

        _LOGGER.debug(
            "Discovery offered %s integration(s) after checking %s "
            "repositories%s",
            len(discovery.candidates),
            discovery.inspected,
            " (list truncated)" if discovery.truncated else "",
        )
        self._candidates = discovery.candidates
        self._discovery = discovery
        return await self.async_step_proposals()

    def _show_discover(
        self, error: str, detail: str, *, inspected: int = 0
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="discover",
            data_schema=vol.Schema({}),
            errors={"base": error},
            description_placeholders={
                "limit": str(MAX_REPOSITORIES),
                "inspected": str(inspected),
                "detail": detail,
            },
        )

    async def async_step_proposals(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The candidates: what the token can see that is not tracked yet."""
        candidates: list[Candidate] = list(getattr(self, "_candidates", []))
        if not candidates:
            return await self.async_step_discover()

        if user_input is not None:
            selected = user_input[CONF_FULL_NAME]
            self._selected_candidate = next(
                (candidate for candidate in candidates if candidate.key == selected),
                None,
            )
            if self._selected_candidate is None:
                return self.async_abort(reason="no_candidates")
            return await self.async_step_proposal()

        options = [
            SelectOptionDict(
                value=candidate.key,
                label=await self._async_candidate_label(candidate),
            )
            for candidate in candidates
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_FULL_NAME, default=candidates[0].key
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        discovery: Discovery | None = getattr(self, "_discovery", None)
        return self.async_show_form(
            step_id="proposals",
            data_schema=schema,
            description_placeholders={
                "count": str(len(candidates)),
                "note": await self._async_discovery_note(discovery),
            },
        )

    async def _async_discovery_note(self, discovery: Discovery | None) -> str:
        """Say when the list is not the whole account.

        A capped scan and a rate limit still return what was found, which is
        better than an empty screen, but the user has to know the list may not
        be complete before concluding an integration is missing.
        """
        if discovery is None:
            return ""
        notes = []
        if discovery.truncated:
            notes.append(
                await async_translate(
                    self.hass, "discovery_truncated", limit=str(MAX_REPOSITORIES)
                )
            )
        if discovery.manifests_truncated:
            notes.append(
                await async_translate(
                    self.hass,
                    "discovery_manifests_truncated",
                    limit=str(MAX_MANIFESTS),
                )
            )
        if discovery.rate_limited:
            notes.append(
                await async_translate(self.hass, "discovery_rate_limited")
            )
        return "\n\n".join(notes)

    async def async_step_proposal(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm one candidate: how to follow it, and whether to install it."""
        errors: dict[str, str] = {}
        self._error_detail = ""
        candidate: Candidate | None = getattr(self, "_selected_candidate", None)
        candidates: list[Candidate] = list(getattr(self, "_candidates", []))
        if candidate is None or candidate not in candidates:
            # A stale selection from a previous search: the list on screen is
            # the only thing the user can have picked from.
            return self.async_abort(reason="no_candidates")

        if user_input is not None:
            install_now = user_input.get(CONF_INSTALL_NOW, True)
            blocker = await self._async_install_blocker(
                candidate.full_name,
                candidate.integration_name,
                install_now=install_now,
            )
            if blocker is not None:
                self._error_detail = blocker
                errors["base"] = "add_failed"
            else:
                result = await self._add_and_install(
                    full_name=candidate.full_name,
                    ref_type=user_input[CONF_REF_TYPE],
                    ref=user_input.get(CONF_REF) or None,
                    integration_name=candidate.integration_name,
                    install_now=install_now,
                )
                if result is not None:
                    return result
                errors["base"] = "add_failed"

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_REF_TYPE, default=REF_TYPE_BRANCH
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=list(VALID_REF_TYPES),
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="ref_type",
                    )
                ),
                vol.Optional(CONF_REF, default=""): str,
                vol.Optional(CONF_INSTALL_NOW, default=True): bool,
            }
        )
        return self.async_show_form(
            step_id="proposal",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "repo": candidate.full_name,
                "integration": candidate.integration_name,
                "version": candidate.version or "?",
                "branch": candidate.default_branch,
                "detail": self._error_detail,
            },
        )

    # --- Manage existing ----------------------------------------------------

    async def async_step_manage_repository(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The list: every tracked repository and what is on disk for it."""
        manager = get_manager(self.hass)
        repos = sorted(
            manager.store.all(), key=lambda repo: repo.full_name.lower()
        )

        if not repos:
            return self.async_abort(reason="no_repositories")

        if user_input is not None:
            self._selected_full_name = user_input[CONF_FULL_NAME]
            return await self.async_step_repository_action()

        options = [
            SelectOptionDict(
                value=repo.full_name, label=await self._async_repo_label(repo)
            )
            for repo in repos
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_FULL_NAME): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        # A list, not a dropdown: this screen is meant to be
                        # read like the Marketplace's installed tab -- one row
                        # per integration, with its version next to it.
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="manage_repository",
            data_schema=schema,
            description_placeholders={
                "count": str(len(repos)),
                "other": str(await self._count_unmanaged(repos)),
            },
        )

    async def _async_repo_label(self, repo: Repository) -> str:
        """One row of the list: what it is, and what is on disk for it.

        Every word a translator can change comes from ``common``: a Spanish UI
        showing an English row would be the only place the integration is not
        translated. The repository and folder names are not translated, they
        are identifiers.
        """
        version = await self._async_installed_version(repo)
        if version is None:
            version = repo.installed_version or await async_translate(
                self.hass, "not_installed"
            )
        if repo.ref_type == REF_TYPE_RELEASE:
            tracking = await async_translate(self.hass, "latest_release")
        else:
            ref = repo.ref or await async_translate(self.hass, "default_branch")
            tracking = f"{repo.ref_type}:{ref}"
        return " · ".join(
            (
                repo.full_name,
                await self._async_folder_label(repo),
                version,
                tracking,
            )
        )

    async def _async_folder_label(self, repo: Repository) -> str:
        """The folder path to display, or a translated placeholder."""
        if repo.integration_name:
            return f"custom_components/{repo.integration_name}"
        return await async_translate(self.hass, "folder_not_chosen")

    async def _async_installed_version(self, repo: Repository) -> str | None:
        """Version in the installed manifest, or None when it cannot be read.

        The ownership marker decides: a tracked repository without files must
        not display the version of a folder another tool put under the same
        name.
        """
        if not repo.integration_name:
            return None
        root = Path(self.hass.config.path("custom_components"))
        owner = await self.hass.async_add_executor_job(
            installed_owner, root / repo.integration_name
        )
        if owner is None or owner.lower() != repo.full_name.lower():
            return None
        return await self.hass.async_add_executor_job(
            installed_manifest_version, root, repo.integration_name
        )

    async def _async_candidate_label(self, candidate: Candidate) -> str:
        """One proposal: what it is, and anything worth knowing about it."""
        flags = []
        if candidate.private:
            flags.append(await async_translate(self.hass, "flag_private"))
        if candidate.installed:
            flags.append(await async_translate(self.hass, "flag_installed"))
        label = (
            f"{candidate.full_name} — "
            f"custom_components/{candidate.integration_name}"
            f" ({candidate.version or '?'})"
        )
        return f"{label} · {' · '.join(flags)}" if flags else label

    async def _count_unmanaged(self, repos: list[Repository]) -> int:
        """Integrations on disk that no tracked repository here installed.

        The ownership marker decides, not the folder name: a repository tracked
        without its files (a failed install, or "Install immediately" unchecked)
        must not hide a folder another tool installed under the same name.
        """
        return await self.hass.async_add_executor_job(
            _count_unmanaged_folders,
            Path(self.hass.config.path("custom_components")),
            {repo.full_name.lower() for repo in repos},
        )

    async def async_step_repository_action(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        self._error_detail = ""
        manager = get_manager(self.hass)
        full_name: str = self._selected_full_name
        if manager.store.get(full_name) is None:
            return self.async_abort(reason="repository_missing")

        if user_input is not None:
            self._notice = ""
            action = user_input[CONF_ACTION]
            if action == ACTION_CHANGE_REF:
                return await self.async_step_edit_repository()
            if action == ACTION_REMOVE:
                return await self.async_step_confirm_remove()
            try:
                result = await manager.install_or_update(
                    full_name, force=action == ACTION_FORCE_INSTALL
                )
            except HomeAssistantError as err:
                errors["base"] = "action_failed"
                self._error_detail = str(err)
            else:
                self._installed_repo_name = full_name
                if result.changed:
                    return await self.async_step_installed_confirmation()
                return await self.async_step_up_to_date_confirmation()

        repo = manager.store.get(full_name)
        title_label = (
            await self._async_repo_label(repo) if repo else full_name
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_ACTION, default=ACTION_INSTALL): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            ACTION_INSTALL,
                            ACTION_FORCE_INSTALL,
                            ACTION_CHANGE_REF,
                            ACTION_REMOVE,
                        ],
                        mode=SelectSelectorMode.LIST,
                        translation_key="repository_action",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="repository_action",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "repo": title_label,
                "detail": self._error_detail,
                "notice": getattr(self, "_notice", ""),
            },
        )

    # --- Change how a repository is tracked ---------------------------------

    async def async_step_edit_repository(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        self._error_detail = ""
        manager = get_manager(self.hass)
        full_name: str = self._selected_full_name
        repo = manager.store.get(full_name)
        if repo is None:
            return self.async_abort(reason="repository_missing")

        if user_input is not None:
            try:
                repo = await manager.update_tracking(
                    full_name,
                    user_input[CONF_REF_TYPE],
                    user_input.get(CONF_REF) or None,
                    integration_name=user_input.get(CONF_INTEGRATION_NAME) or None,
                )
            except HomeAssistantError as err:
                errors["base"] = "action_failed"
                self._error_detail = str(err)
            else:
                _LOGGER.info(
                    "%s now tracks %s:%s",
                    repo.full_name,
                    repo.ref_type,
                    repo.ref,
                )
                # Back to the action list instead of closing the dialog: the new
                # ref only lands on disk when the user installs it, and that is
                # one click away from here.
                self._notice = await async_translate(
                    self.hass,
                    "tracking_saved",
                    repo=await self._async_repo_label(repo),
                )
                return await self.async_step_repository_action()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_REF_TYPE, default=repo.ref_type
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=list(VALID_REF_TYPES),
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="ref_type",
                    )
                ),
                vol.Optional(CONF_REF, default=repo.ref or ""): str,
                vol.Optional(
                    CONF_INTEGRATION_NAME, default=repo.integration_name or ""
                ): str,
            }
        )
        return self.async_show_form(
            step_id="edit_repository",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "repo": await self._async_repo_label(repo),
                "detail": self._error_detail,
            },
        )

    # --- Remove -------------------------------------------------------------

    async def async_step_confirm_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        self._error_detail = ""
        manager = get_manager(self.hass)
        full_name: str = self._selected_full_name
        repo = manager.store.get(full_name)
        if repo is None:
            return self.async_abort(reason="repository_missing")

        if user_input is not None:
            delete_files = user_input.get(CONF_DELETE_FILES, True)
            try:
                files_removed = await manager.remove_repository(
                    full_name, delete_files=delete_files
                )
            except HomeAssistantError as err:
                errors["base"] = "action_failed"
                self._error_detail = str(err)
            else:
                self._removed_repo_name = full_name
                self._removed_files = files_removed
                return await self.async_step_removed_confirmation()

        schema = vol.Schema({vol.Optional(CONF_DELETE_FILES, default=True): bool})
        return self.async_show_form(
            step_id="confirm_remove",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "repo": await self._async_repo_label(repo),
                "integration": repo.integration_name
                or await async_translate(self.hass, "folder_not_chosen"),
                "detail": self._error_detail,
            },
        )

    # --- Post-install confirmation -----------------------------------------

    async def async_step_installed_confirmation(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data={})

        full_name: str = getattr(self, "_installed_repo_name", "")
        repo = get_manager(self.hass).store.get(full_name) if full_name else None
        version = await self._async_installed_version(repo) if repo else None
        if version is None and repo is not None:
            version = repo.installed_version or (
                repo.installed_sha[:7] if repo.installed_sha else None
            )
        return self.async_show_form(
            step_id="installed_confirmation",
            data_schema=vol.Schema({}),
            description_placeholders={
                "repo": full_name or "?",
                "integration": (repo.integration_name if repo else "?") or "?",
                "version": version or "?",
            },
            last_step=True,
        )

    async def async_step_up_to_date_confirmation(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The tracked ref already matches the files on disk."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="up_to_date_confirmation",
            data_schema=vol.Schema({}),
            description_placeholders={
                "repo": getattr(self, "_installed_repo_name", "") or "?"
            },
            last_step=True,
        )

    async def async_step_tracked_confirmation(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The repository is registered but nothing was downloaded."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})

        full_name: str = getattr(self, "_installed_repo_name", "")
        return self.async_show_form(
            step_id="tracked_confirmation",
            data_schema=vol.Schema({}),
            description_placeholders={"repo": full_name or "?"},
            last_step=True,
        )

    async def async_step_install_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The repository is tracked, but nothing reached custom_components/."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="install_failed",
            data_schema=vol.Schema({}),
            description_placeholders={
                "repo": getattr(self, "_installed_repo_name", "?") or "?",
                "reason": self._error_detail,
            },
            last_step=True,
        )

    async def async_step_removed_confirmation(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data={})

        files = await async_translate(
            self.hass,
            (
                "files_deleted"
                if getattr(self, "_removed_files", False)
                else "files_kept"
            ),
        )
        return self.async_show_form(
            step_id="removed_confirmation",
            data_schema=vol.Schema({}),
            description_placeholders={
                "repo": getattr(self, "_removed_repo_name", "?") or "?",
                "files": files,
            },
            last_step=True,
        )

    # --- Token rotation -----------------------------------------------------

    async def async_step_update_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input[CONF_GITHUB_TOKEN].strip()
            _, error = await _verify_token(self.hass, token)
            if error:
                errors["base"] = error
            else:
                entry = self._entry()
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_GITHUB_TOKEN: token},
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="update_token", data_schema=_token_schema(), errors=errors
        )


def _count_unmanaged_folders(root: Path, tracked: set[str]) -> int:
    """Folders on disk whose ownership marker names no tracked repository."""
    unmanaged = 0
    for name in installed_integrations(root):
        if name in PROTECTED_INTEGRATIONS:
            continue
        owner = installed_owner(root / name)
        if owner is None or owner.lower() not in tracked:
            unmanaged += 1
    return unmanaged
