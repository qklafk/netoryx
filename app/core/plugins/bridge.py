"""Compatibility metadata for gradual migration from FeatureModule objects."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.modules import FeatureModule


@dataclass(frozen=True, slots=True)
class LegacyModuleBridge:
    identifier: str
    feature_module: FeatureModule
    migratable: bool = True


def bridge_feature_modules(modules: list[FeatureModule]) -> dict[str, LegacyModuleBridge]:
    """Expose existing feature modules for inventory without changing their runtime lifecycle."""
    return {module.name: LegacyModuleBridge(f"legacy.{module.name}", module) for module in modules}
