"""Private Integrations — install custom integrations from private GitHub repos."""
from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_DELETE_FILES,
    CONF_FORCE,
    CONF_FULL_NAME,
    CONF_GITHUB_TOKEN,
    CONF_INTEGRATION_NAME,
    CONF_REF,
    CONF_REF_TYPE,
    DOMAIN,
    REF_TYPE_BRANCH,
    SERVICE_ADD_REPOSITORY,
    SERVICE_INSTALL,
    SERVICE_REMOVE_REPOSITORY,
    SERVICE_UPDATE,
    VALID_REF_TYPES,
)
from .github import GitHubAuthError, GitHubClient, GitHubError, GitHubRateLimitError
from .installer import recover_stale_installs
from .manager import (
    PrivateIntegrationsManager,
    get_manager,
    pending_restart_notification_id,
)
from .store import RepositoryStore

_LOGGER = logging.getLogger(__name__)

ADD_REPO_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_FULL_NAME): cv.string,
        vol.Optional(CONF_REF_TYPE, default=REF_TYPE_BRANCH): vol.In(
            VALID_REF_TYPES
        ),
        vol.Optional(CONF_REF, default=""): cv.string,
        vol.Optional(CONF_INTEGRATION_NAME): cv.string,
    }
)

REMOVE_REPO_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_FULL_NAME): cv.string,
        vol.Optional(CONF_DELETE_FILES, default=True): cv.boolean,
    }
)

INSTALL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_FULL_NAME): cv.string,
        vol.Optional(CONF_FORCE, default=False): cv.boolean,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Private Integrations from a config entry."""
    token: str = entry.data[CONF_GITHUB_TOKEN]
    session = async_get_clientsession(hass)
    client = GitHubClient(session, token)

    try:
        await client.verify_token()
    except GitHubAuthError as err:
        raise ConfigEntryAuthFailed("GitHub token rejected") from err
    except GitHubRateLimitError as err:
        # Transient: retry later instead of asking the user for a new token.
        raise ConfigEntryNotReady(f"GitHub rate limit reached: {err}") from err
    except GitHubError as err:
        _LOGGER.warning("GitHub API unavailable at startup: %s", err)

    store = RepositoryStore(hass)
    await store.async_load()

    custom_components_dir = Path(hass.config.path("custom_components"))
    if hass.state is CoreState.starting:
        # Only while starting. is_running is also true in CoreState.starting, so
        # it cannot tell a boot from a reload; and a reload happens with installs
        # possibly in flight, whose staging folder must not be cleaned up here.
        try:
            actions = await hass.async_add_executor_job(
                recover_stale_installs, custom_components_dir
            )
        except Exception:  # noqa: BLE001 - recovery must not break setup
            _LOGGER.exception(
                "Private Integrations: could not clean up an interrupted install"
            )
        else:
            for action in actions:
                _LOGGER.info("Private Integrations: %s", action)

        for repo in store.all():
            # The integration is loaded again: the reminder is obsolete.
            if repo.integration_name:
                persistent_notification.async_dismiss(
                    hass, pending_restart_notification_id(repo.integration_name)
                )

    manager = PrivateIntegrationsManager(hass, client, store)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "store": store,
        "manager": manager,
    }

    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not hass.data.get(DOMAIN):
        for svc in (
            SERVICE_ADD_REPOSITORY,
            SERVICE_REMOVE_REPOSITORY,
            SERVICE_INSTALL,
            SERVICE_UPDATE,
        ):
            if hass.services.has_service(DOMAIN, svc):
                hass.services.async_remove(DOMAIN, svc)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget the tracked repositories when the entry is deleted.

    The installed integrations are left on disk: they are loaded by Home
    Assistant on their own and deleting them from here would be a surprise.
    """
    store = RepositoryStore(hass)
    await store.async_load()
    installed = sorted(
        repo.integration_name for repo in store.all() if repo.integration_name
    )
    await store.async_remove()
    if installed:
        _LOGGER.info(
            "Private Integrations removed; it left these integrations in "
            "custom_components/ untouched: %s",
            ", ".join(installed),
        )


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_ADD_REPOSITORY):
        return

    async def add_repository(call: ServiceCall) -> None:
        await get_manager(hass).add_repository(
            full_name_or_url=call.data[CONF_FULL_NAME],
            ref_type=call.data.get(CONF_REF_TYPE, REF_TYPE_BRANCH),
            ref=call.data.get(CONF_REF) or None,
            integration_name=call.data.get(CONF_INTEGRATION_NAME),
        )

    async def remove_repository(call: ServiceCall) -> None:
        await get_manager(hass).remove_repository(
            call.data[CONF_FULL_NAME],
            delete_files=call.data[CONF_DELETE_FILES],
        )

    async def install_or_update(call: ServiceCall) -> None:
        await get_manager(hass).install_or_update(
            call.data[CONF_FULL_NAME], force=call.data[CONF_FORCE]
        )

    hass.services.async_register(
        DOMAIN, SERVICE_ADD_REPOSITORY, add_repository, schema=ADD_REPO_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_REPOSITORY,
        remove_repository,
        schema=REMOVE_REPO_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_INSTALL, install_or_update, schema=INSTALL_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE, install_or_update, schema=INSTALL_SCHEMA
    )
