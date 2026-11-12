"""Trusted plugins packaged with the application."""

from __future__ import annotations

from typing import Any

from installer.models import ModuleManifest

from .contracts import HealthReport, PluginContext


class DemoPlugin:
    """Small persistent plugin used to exercise the lifecycle without system privileges."""

    def __init__(self, manifest: ModuleManifest) -> None:
        self.manifest = manifest

    async def install(self, context: PluginContext) -> None:
        context.data.set("installed", "true")

    async def configure(self, context: PluginContext, values: dict[str, Any]) -> None:
        label = values.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("label is required")
        context.data.set("label", label.strip())

    async def enable(self, context: PluginContext) -> None:
        if context.data.get("installed") != "true":
            raise RuntimeError("plugin is not installed")
        context.data.set("enabled", "true")

    async def disable(self, context: PluginContext) -> None:
        context.data.set("enabled", "false")

    async def healthcheck(self, context: PluginContext) -> HealthReport:
        enabled = context.data.get("enabled") == "true"
        return HealthReport(enabled, "enabled" if enabled else "disabled")

    async def update(self, context: PluginContext, target_version: str) -> None:
        raise NotImplementedError(f"update to {target_version} is not supported")

    async def uninstall(self, context: PluginContext, *, purge_data: bool) -> None:
        context.data.set("enabled", "false")
        if purge_data:
            context.data.delete_all()
