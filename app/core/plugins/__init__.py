"""Trusted built-in plugin SDK and lifecycle management."""

from .contracts import Capability, HealthReport, ModuleStatus, PlatformPlugin, PluginContext, ServiceRegistry
from .lifecycle import LifecycleManager, UnsupportedOperation
from .registry import PluginRegistry
from .runtime import CoreRuntime

__all__ = [
    "Capability",
    "CoreRuntime",
    "HealthReport",
    "LifecycleManager",
    "ModuleStatus",
    "PlatformPlugin",
    "PluginContext",
    "PluginRegistry",
    "ServiceRegistry",
    "UnsupportedOperation",
]
