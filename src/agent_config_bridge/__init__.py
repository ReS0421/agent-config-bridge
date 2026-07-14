"""Public API for the agent configuration bridge."""

from agent_config_bridge.config import ConfigError, load_config
from agent_config_bridge.models import (
    BridgeConfig,
    Component,
    LinkMode,
    Platform,
    Product,
    Surface,
    TargetConfig,
)
from agent_config_bridge.platforms import (
    UnsupportedPlatformError,
    current_platform,
    default_config_home,
    resolve_platform,
)

__all__ = [
    "BridgeConfig",
    "Component",
    "ConfigError",
    "LinkMode",
    "Platform",
    "Product",
    "Surface",
    "TargetConfig",
    "UnsupportedPlatformError",
    "current_platform",
    "default_config_home",
    "load_config",
    "resolve_platform",
]
