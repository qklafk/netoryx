from __future__ import annotations

from installer.models import DeploymentRole, InstallRequest, MachineFacts, PortRequirement, ProductProfile
from installer.planner import build_plan
from installer.registry import builtin_registry
from installer.state import InstallState, save_state


def facts(*, ram_mb: int = 2048, occupied: frozenset[PortRequirement] = frozenset()) -> MachineFacts:
    return MachineFacts("ubuntu", "24.04", 2, ram_mb, 60_000, occupied)


def test_manifests_are_unique_and_references_resolve() -> None:
    registry = builtin_registry()
    identifiers = [manifest.identifier for manifest in registry.all()]
    assert identifiers
    assert len(identifiers) == len(set(identifiers))


def test_profile_and_role_are_independent() -> None:
    plan = build_plan(
        builtin_registry(),
        InstallRequest(ProductProfile.PERSONAL, DeploymentRole.CONTROL_PLANE, ("commercial.subscriptions",)),
        facts(),
    )
    assert plan.valid
    assert {item.identifier for item in plan.modules} == {"core.platform", "commercial.subscriptions"}


def test_vpn_node_does_not_select_control_plane() -> None:
    plan = build_plan(
        builtin_registry(),
        InstallRequest(ProductProfile.COMMERCIAL, DeploymentRole.VPN_NODE, ("proxy.mtproxy",)),
        facts(),
    )
    assert plan.valid
    assert {item.identifier for item in plan.modules} == {"core.node-runtime", "proxy.mtproxy"}
    assert "core.platform" not in {item.identifier for item in plan.modules}


def test_miniapp_requires_https_and_bot() -> None:
    plan = build_plan(
        builtin_registry(),
        InstallRequest(ProductProfile.PERSONAL, DeploymentRole.ALL_IN_ONE, ("interface.miniapp",)),
        facts(),
    )
    assert not plan.valid
    assert "requires a public HTTPS endpoint" in "\n".join(plan.errors)
    assert "interface.telegram-bot" in {item.identifier for item in plan.modules}


def test_port_and_resource_preflight_errors() -> None:
    plan = build_plan(
        builtin_registry(),
        InstallRequest(ProductProfile.PERSONAL, DeploymentRole.VPN_NODE, ("proxy.mtproxy",)),
        facts(ram_mb=100, occupied=frozenset({PortRequirement(443, "tcp")})),
    )
    assert not plan.valid
    assert any("insufficient RAM" in error for error in plan.errors)
    assert any("port conflict" in error for error in plan.errors)


def test_state_is_written_without_secrets() -> None:
    from pathlib import Path

    destination = Path(".pytest_tmp_installer_state.json")
    request = InstallRequest(ProductProfile.PERSONAL, DeploymentRole.ALL_IN_ONE, ("core.platform",))
    try:
        save_state(destination, InstallState.planned(request))
        text = destination.read_text(encoding="utf-8")
        assert '"status": "planned"' in text
        assert "secret" not in text.lower()
    finally:
        destination.unlink(missing_ok=True)
