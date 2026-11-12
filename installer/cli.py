"""Dry-run command line interface for installation planning."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .artifact import create_artifact, load_artifact, save_artifact
from .detection import detect_installation
from .engine import AdapterRegistry, DeploymentAdapter, ExecutionEngine, ExecutionError, StepContract
from .journal import JournalError, load_journal
from .models import DeploymentRole, InstallMode, InstallRequest, ProductProfile
from .planner import build_plan
from .preflight import collect_machine_facts, ubuntu_preflight
from .registry import builtin_registry
from .ubuntu_adapter import UbuntuCoreDeploymentAdapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m installer", description="VPN platform installation planner")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "preflight", "apply", "status", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--profile", choices=[item.value for item in ProductProfile])
        command.add_argument("--deployment-role", choices=[item.value for item in DeploymentRole])
        command.add_argument("--module", action="append", default=[])
        command.add_argument("--public-https", action="store_true")
        command.add_argument("--mode", choices=[item.value for item in InstallMode], default=InstallMode.QUICK.value)
        command.add_argument("--state-file", type=Path, default=Path(".installer-state.json"))
        command.add_argument("--install-dir", type=Path, default=Path("/opt/netoryx"))
        command.add_argument("--yes", action="store_true", help="confirm a supported apply/resume operation")
        command.add_argument("--save", type=Path, help="save a reviewed non-secret plan artifact")
        command.add_argument("--plan", type=Path, help="reviewed plan artifact required by apply/resume")
        command.add_argument("--compose-file", type=Path, help="reviewed production Compose file for Core deployment")
    return parser


def _ask(prompt: str, options: list[str], default: str) -> str:
    selected = input(f"{prompt} [{'/'.join(options)}] ({default}): ").strip().lower()
    return selected or default


def _request(args: argparse.Namespace) -> InstallRequest:
    registry = builtin_registry()
    profile = args.profile or _ask("Product profile", [item.value for item in ProductProfile], "personal")
    role = args.deployment_role or _ask("Deployment role", [item.value for item in DeploymentRole], "all-in-one")
    modules = list(args.module)
    if not modules:
        available = [item.identifier for item in registry.all() if DeploymentRole(role) in item.roles]
        if args.mode == InstallMode.ADVANCED.value:
            print("Available modules:\n  " + "\n  ".join(available))
            raw = input("Modules (comma-separated): ").strip()
            modules = [item.strip() for item in raw.split(",") if item.strip()]
        else:
            modules = ["core.platform", "interface.web-admin"] if role != "vpn-node" else ["core.node-runtime"]
    return InstallRequest(ProductProfile(profile), DeploymentRole(role), tuple(modules), args.public_https)


def _print_plan(plan: object, facts: object) -> None:
    from .models import MachineFacts, Plan

    assert isinstance(plan, Plan)
    assert isinstance(facts, MachineFacts)
    print(
        json.dumps(
            {
                "server": {
                    "os": facts.operating_system,
                    "version": facts.os_version,
                    "cpu_cores": facts.cpu_cores,
                    "ram_mb": facts.ram_mb,
                    "disk_mb": facts.disk_mb,
                },
                "profile": plan.request.profile.value,
                "deployment_role": plan.request.role.value,
                "modules": [item.identifier for item in plan.modules],
                "resources": {
                    "ram_mb": plan.required_resources.ram_mb,
                    "disk_mb": plan.required_resources.disk_mb,
                    "cpu_cores": plan.required_resources.cpu_cores,
                },
                "errors": plan.errors,
                "warnings": plan.warnings,
                "dry_run": True,
            },
            indent=2,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        detection = detect_installation(args.install_dir)
        journal_path = args.install_dir / ".installer-journal.json"
        report: dict[str, object] = {"installation": detection.presence.value, "evidence": detection.evidence}
        if journal_path.exists():
            try:
                journal = load_journal(journal_path)
                report["journal"] = {
                    "recovery": journal.recovery_status,
                    "steps": [
                        {"id": step.identifier, "status": step.status.value, "error": step.error}
                        for step in journal.steps
                    ],
                }
            except JournalError as exc:
                report["journal_error"] = str(exc)
        print(json.dumps(report, indent=2))
        return 0
    artifact = None
    if args.command in {"apply", "resume"}:
        if args.plan is None:
            print("apply/resume requires --plan PATH", file=sys.stderr)
            return 2
        try:
            artifact = load_artifact(args.plan)
            request = InstallRequest(
                ProductProfile(artifact.profile),
                DeploymentRole(artifact.role),
                tuple(str(item["id"]) for item in artifact.modules),
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"invalid plan artifact: {exc}", file=sys.stderr)
            return 2
    else:
        request = _request(args)
    registry = builtin_registry()
    draft = build_plan(registry, request)
    required_ports = {port for module in draft.modules for port in module.ports}
    facts = collect_machine_facts(required_ports=required_ports)
    plan = build_plan(registry, request, facts)
    artifact_engine = ExecutionEngine(
        args.install_dir / ".installer-journal.json", AdapterRegistry(), install_directory=args.install_dir
    )
    if artifact is not None:
        current_modules = tuple(
            {"id": item.identifier, "version": item.version, "depends_on": sorted(item.depends_on)}
            for item in plan.modules
        )
        if artifact.fingerprint != artifact_engine.fingerprint(plan) or artifact.modules != current_modules:
            print("plan artifact is stale; create and review a new plan", file=sys.stderr)
            return 2
    _print_plan(plan, facts)
    checks = ubuntu_preflight(facts)
    print(
        json.dumps(
            {
                "preflight": [
                    {
                        "id": item.identifier,
                        "status": item.status.value,
                        "severity": item.severity.value,
                        "actual": item.actual,
                        "expected": item.expected,
                        "message": item.message,
                    }
                    for item in checks
                ]
            },
            indent=2,
        )
    )
    if args.command in {"plan", "preflight"}:
        if args.save:
            if args.command != "plan" or not plan.valid:
                print("a plan artifact requires a valid plan", file=sys.stderr)
                return 2
            try:
                save_artifact(args.save, create_artifact(plan, artifact_engine))
            except (FileExistsError, ValueError) as exc:
                print(f"cannot save plan artifact: {exc}", file=sys.stderr)
                return 2
            print(f"plan artifact saved: {args.save}")
        return 0 if plan.valid else 2
    if not args.yes:
        print("apply/resume requires explicit --yes", file=sys.stderr)
        return 2
    if detect_installation(args.install_dir).presence.value != "absent" and args.command == "apply":
        print("existing or incomplete installation detected; refusing to overwrite it", file=sys.stderr)
        return 2
    adapters: dict[str, DeploymentAdapter] = {}
    if args.compose_file:
        core_adapter = UbuntuCoreDeploymentAdapter(args.compose_file)
        adapters = {"core.platform": core_adapter, "interface.web-admin": core_adapter}
    missing_adapters = [item.identifier for item in plan.modules if item.identifier not in adapters]
    if missing_adapters:
        print(f"installation blocked: no deployment adapter for {', '.join(missing_adapters)}", file=sys.stderr)
        return 2
    engine = ExecutionEngine(
        args.install_dir / ".installer-journal.json", AdapterRegistry(adapters), install_directory=args.install_dir
    )
    steps = tuple(StepContract(f"module.{item.identifier}", item.identifier) for item in plan.modules)
    try:
        import asyncio

        asyncio.run(engine.execute(plan, steps, resume=args.command == "resume"))
    except (ExecutionError, JournalError) as exc:
        print(f"installation blocked: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
