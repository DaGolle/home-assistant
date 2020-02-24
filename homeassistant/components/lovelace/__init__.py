"""Support for the Lovelace UI."""
from abc import ABC, abstractmethod
from functools import wraps
import logging
import os
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import frontend, websocket_api
from homeassistant.const import CONF_FILENAME, CONF_ICON
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import sanitize_filename, slugify
from homeassistant.util.yaml import load_yaml

_LOGGER = logging.getLogger(__name__)

DOMAIN = "lovelace"
STORAGE_KEY_DEFAULT_CONFIG = DOMAIN
STORAGE_VERSION = 1
CONF_MODE = "mode"
MODE_YAML = "yaml"
MODE_STORAGE = "storage"
MODE_STRATEGY = "strategy"

CONF_DASHBOARDS = "dashboards"
CONF_SIDEBAR = "sidebar"
CONF_TITLE = "title"
CONF_URL_PATH = "url_path"
CONF_REQUIRE_ADMIN = "require_admin"

DASHBOARD_BASE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_REQUIRE_ADMIN, default=False): cv.boolean,
        vol.Optional(CONF_SIDEBAR): {
            vol.Required(CONF_ICON): cv.icon,
            vol.Required(CONF_TITLE): cv.string,
        },
    }
)

YAML_DASHBOARD_SCHEMA = DASHBOARD_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_MODE): MODE_YAML,
        vol.Required(CONF_FILENAME): vol.All(cv.string, sanitize_filename),
    }
)


def url_slug(value: Any) -> str:
    """Validate value is a valid url slug."""
    if value is None:
        raise vol.Invalid("Slug should not be None")
    str_value = str(value)
    slg = slugify(str_value, separator="-")
    if str_value == slg:
        return str_value
    raise vol.Invalid(f"invalid slug {value} (try {slg})")


CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN, default={}): vol.Schema(
            {
                vol.Optional(CONF_MODE, default=MODE_STORAGE): vol.All(
                    vol.Lower, vol.In([MODE_YAML, MODE_STORAGE])
                ),
                vol.Optional(CONF_DASHBOARDS): cv.schema_with_slug_keys(
                    YAML_DASHBOARD_SCHEMA, slug_validator=url_slug,
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

EVENT_LOVELACE_UPDATED = "lovelace_updated"

LOVELACE_CONFIG_FILE = "ui-lovelace.yaml"


class ConfigNotFound(HomeAssistantError):
    """When no config available."""


async def async_setup(hass, config):
    """Set up the Lovelace commands."""
    # Pass in default to `get` because defaults not set if loaded as dep
    mode = config[DOMAIN][CONF_MODE]

    frontend.async_register_built_in_panel(hass, DOMAIN, config={"mode": mode})

    if mode == MODE_YAML:
        default_config = LovelaceYAML(hass, None, LOVELACE_CONFIG_FILE)
    else:
        default_config = LovelaceStorage(hass, None)

    # We store a dictionary mapping url_path: config. None is the default.
    dashboards = hass.data[DOMAIN] = {None: default_config}

    hass.components.websocket_api.async_register_command(websocket_lovelace_config)
    hass.components.websocket_api.async_register_command(websocket_lovelace_save_config)
    hass.components.websocket_api.async_register_command(
        websocket_lovelace_delete_config
    )

    hass.components.system_health.async_register_info(DOMAIN, system_health_info)

    if hass.config.safe_mode or CONF_DASHBOARDS not in config[DOMAIN]:
        return True

    for url_path, dashboard in config[DOMAIN][CONF_DASHBOARDS].items():
        # For now always mode=yaml
        config = LovelaceYAML(hass, url_path, dashboard[CONF_FILENAME])
        dashboards[url_path] = config

        kwargs = {
            "hass": hass,
            "component_name": DOMAIN,
            "frontend_url_path": url_path,
            "require_admin": dashboard[CONF_REQUIRE_ADMIN],
            "config": {"mode": dashboard[CONF_MODE]},
        }

        if CONF_SIDEBAR in dashboard:
            kwargs["sidebar_title"] = dashboard[CONF_SIDEBAR][CONF_TITLE]
            kwargs["sidebar_icon"] = dashboard[CONF_SIDEBAR][CONF_ICON]

        try:
            frontend.async_register_built_in_panel(**kwargs)
        except ValueError:
            _LOGGER.warning("Panel url path %s is not unique", url_path)

    return True


class LovelaceConfig(ABC):
    """Base class for Lovelace config."""

    def __init__(self, hass, url_path):
        """Initialize Lovelace config."""
        self.hass = hass
        self.url_path = url_path

    @abstractmethod
    async def async_get_info(self):
        """Return the config info."""

    @abstractmethod
    async def async_load(self, force):
        """Load config."""

    async def async_save(self, config):
        """Save config."""
        raise HomeAssistantError("Not supported")

    async def async_delete(self):
        """Delete config."""
        raise HomeAssistantError("Not supported")

    @callback
    def _config_updated(self):
        """Fire config updated event."""
        self.hass.bus.async_fire(EVENT_LOVELACE_UPDATED, {"url_path": self.url_path})


class LovelaceStorage(LovelaceConfig):
    """Class to handle Storage based Lovelace config."""

    def __init__(self, hass, url_path):
        """Initialize Lovelace config based on storage helper."""
        super().__init__(hass, url_path)
        if url_path is None:
            storage_key = STORAGE_KEY_DEFAULT_CONFIG
        else:
            raise ValueError("Storage-based dashboards are not supported")

        self._store = hass.helpers.storage.Store(STORAGE_VERSION, storage_key)
        self._data = None

    async def async_get_info(self):
        """Return the YAML storage mode."""
        if self._data is None:
            await self._load()

        if self._data["config"] is None:
            return {"mode": "auto-gen"}

        return _config_info("storage", self._data["config"])

    async def async_load(self, force):
        """Load config."""
        if self.hass.config.safe_mode:
            raise ConfigNotFound

        if self._data is None:
            await self._load()

        config = self._data["config"]

        if config is None:
            raise ConfigNotFound

        return config

    async def async_save(self, config):
        """Save config."""
        if self._data is None:
            await self._load()
        self._data["config"] = config
        self._config_updated()
        await self._store.async_save(self._data)

    async def async_delete(self):
        """Delete config."""
        await self.async_save(None)

    async def _load(self):
        """Load the config."""
        data = await self._store.async_load()
        self._data = data if data else {"config": None}


class LovelaceYAML(LovelaceConfig):
    """Class to handle YAML-based Lovelace config."""

    def __init__(self, hass, url_path, path):
        """Initialize the YAML config."""
        super().__init__(hass, url_path)
        self.path = hass.config.path(path)
        self._cache = None

    async def async_get_info(self):
        """Return the YAML storage mode."""
        try:
            config = await self.async_load(False)
        except ConfigNotFound:
            return {
                "mode": "yaml",
                "error": "{} not found".format(self.path),
            }

        return _config_info("yaml", config)

    async def async_load(self, force):
        """Load config."""
        is_updated, config = await self.hass.async_add_executor_job(
            self._load_config, force
        )
        if is_updated:
            self._config_updated()
        return config

    def _load_config(self, force):
        """Load the actual config."""
        # Check for a cached version of the config
        if not force and self._cache is not None:
            config, last_update = self._cache
            modtime = os.path.getmtime(self.path)
            if config and last_update > modtime:
                return False, config

        is_updated = self._cache is not None

        try:
            config = load_yaml(self.path)
        except FileNotFoundError:
            raise ConfigNotFound from None

        self._cache = (config, time.time())
        return is_updated, config


def handle_yaml_errors(func):
    """Handle error with WebSocket calls."""

    @wraps(func)
    async def send_with_error_handling(hass, connection, msg):
        url_path = msg.get(CONF_URL_PATH)
        config = hass.data[DOMAIN].get(url_path)

        if config is None:
            connection.send_error(
                msg["id"], "config_not_found", f"Unknown config specified: {url_path}"
            )
            return

        error = None
        try:
            result = await func(hass, connection, msg, config)
        except ConfigNotFound:
            error = "config_not_found", "No config found."
        except HomeAssistantError as err:
            error = "error", str(err)

        if error is not None:
            connection.send_error(msg["id"], *error)
            return

        if msg is not None:
            await connection.send_big_result(msg["id"], result)
        else:
            connection.send_result(msg["id"], result)

    return send_with_error_handling


@websocket_api.async_response
@websocket_api.websocket_command(
    {
        "type": "lovelace/config",
        vol.Optional("force", default=False): bool,
        vol.Optional(CONF_URL_PATH): vol.Any(None, cv.string),
    }
)
@handle_yaml_errors
async def websocket_lovelace_config(hass, connection, msg, config):
    """Send Lovelace UI config over WebSocket configuration."""
    return await config.async_load(msg["force"])


@websocket_api.async_response
@websocket_api.websocket_command(
    {
        "type": "lovelace/config/save",
        "config": vol.Any(str, dict),
        vol.Optional(CONF_URL_PATH): vol.Any(None, cv.string),
    }
)
@handle_yaml_errors
async def websocket_lovelace_save_config(hass, connection, msg, config):
    """Save Lovelace UI configuration."""
    await config.async_save(msg["config"])


@websocket_api.async_response
@websocket_api.websocket_command(
    {
        "type": "lovelace/config/delete",
        vol.Optional(CONF_URL_PATH): vol.Any(None, cv.string),
    }
)
@handle_yaml_errors
async def websocket_lovelace_delete_config(hass, connection, msg, config):
    """Delete Lovelace UI configuration."""
    await config.async_delete()


async def system_health_info(hass):
    """Get info for the info page."""
    return await hass.data[DOMAIN][None].async_get_info()


def _config_info(mode, config):
    """Generate info about the config."""
    return {
        "mode": mode,
        "resources": len(config.get("resources", [])),
        "views": len(config.get("views", [])),
    }
