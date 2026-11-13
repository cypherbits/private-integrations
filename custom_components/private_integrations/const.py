"""Constants for the Private Integrations integration."""
from __future__ import annotations

DOMAIN = "private_integrations"

CONF_GITHUB_TOKEN = "github_token"

# Per-repo config keys
CONF_FULL_NAME = "full_name"
CONF_REF_TYPE = "ref_type"
CONF_REF = "ref"
CONF_INTEGRATION_NAME = "integration_name"
CONF_DELETE_FILES = "delete_files"
CONF_FORCE = "force"
CONF_ACTION = "action"
CONF_INSTALL_NOW = "install_now"

# Values of the options-flow action selector. Reinstalling the same commit was a
# checkbox shown next to every action, including the two that ignore it.
ACTION_INSTALL = "install"
ACTION_FORCE_INSTALL = "force_install"
ACTION_CHANGE_REF = "change_ref"
ACTION_REMOVE = "remove"

REF_TYPE_RELEASE = "release"
REF_TYPE_TAG = "tag"
REF_TYPE_BRANCH = "branch"
REF_TYPE_COMMIT = "commit"

VALID_REF_TYPES = (
    REF_TYPE_RELEASE,
    REF_TYPE_TAG,
    REF_TYPE_BRANCH,
    REF_TYPE_COMMIT,
)

STORAGE_KEY = f"{DOMAIN}.repositories"
STORAGE_VERSION = 1

GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "private-integrations"

SERVICE_ADD_REPOSITORY = "add_repository"
SERVICE_REMOVE_REPOSITORY = "remove_repository"
SERVICE_INSTALL = "install"
SERVICE_UPDATE = "update"
