"""Command line entry point.

python -m app                      run (bot + web server + background jobs, per ROLE)
python -m app --role worker        run only one part of the application
python -m app migrate              apply database migrations
python -m app check [--offline]    validate configuration, database and the bot token
python -m app create-admin-link    one-time login link for the web admin
python -m app backup               create a backup now (in BACKUP_DIR, encrypted with BACKUP_PASSWORD if set)
python -m app restore FILE --yes   restore a backup (replaces all data)
python -m app reality-config ...   print an Xray config with a new Reality inbound (installer)
python -m app bootstrap-local ...  register the servers installed next to the app (used by the installer)
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import logging
import sys
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import ValidationError

from app.config import Settings
from app.core import __version__

log = logging.getLogger("app")

ROLES = ("all", "bot", "web", "worker")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app", description="VPN subscription bot")
    parser.add_argument("--version", action="version", version=f"Netoryx {__version__}")
    parser.add_argument("--role", choices=ROLES, help="process role (overrides ROLE)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run the application (default)")
    migrate = sub.add_parser("migrate", help="apply database migrations")
    migrate.add_argument("revision", nargs="?", default="head")
    check = sub.add_parser("check", help="validate configuration and connectivity")
    check.add_argument("--offline", action="store_true", help="skip the Telegram API check")
    link = sub.add_parser("create-admin-link", help="print a one-time web admin login link")
    link.add_argument("--tg-id", type=int, help="admin Telegram id (default: first of ADMIN_IDS)")
    sub.add_parser("backup", help="create a backup now")
    restore = sub.add_parser("restore", help="restore a backup file")
    restore.add_argument("file")
    restore.add_argument("--yes", action="store_true", help="confirm that current data will be replaced")
    restore.add_argument("--password", help="password of an encrypted backup (default: BACKUP_PASSWORD)")
    rc = sub.add_parser("reality-config", help="print an Xray configuration with a new VLESS Reality inbound")
    rc.add_argument("--port", type=int, required=True)
    rc.add_argument("--sni", required=True)
    rc.add_argument("--dest", required=True)
    local = sub.add_parser("bootstrap-local", help="register the local 3x-ui / Telegram proxy (idempotent)")
    local.add_argument("--location-name", default="Main")
    local.add_argument("--location-flag", default="🌍")
    local.add_argument("--xui-url", help="3x-ui URL including the web base path")
    local.add_argument("--xui-user")
    local.add_argument("--xui-pass-file", help="file holding the 3x-ui password")
    local.add_argument("--reality-port", type=int, help="port of the Reality inbound on the host")
    local.add_argument("--reality-sni", help="server name of the Reality inbound, e.g. vpn.example.com")
    local.add_argument("--reality-dest", default="127.0.0.1:8444")
    local.add_argument("--endpoint-address", help="public address written into links")
    local.add_argument("--endpoint-port", type=int, default=443)
    local.add_argument("--socks-port", type=int, help="create the Telegram SOCKS5 inbound on this port")
    local.add_argument("--socks-host", help="public address of the SOCKS5 proxy (default: the endpoint address)")
    local.add_argument("--ss-port", type=int, help="create a Shadowsocks-2022 inbound on this port (if supported)")
    local.add_argument(
        "--panel-kind",
        choices=["remnawave", "marzban", "pasarguard", "marzneshin"],
        help="register and prepare a panel deployed by the installer",
    )
    local.add_argument("--panel-url", help="API address of that panel inside the Docker network")
    local.add_argument("--panel-user", help="administrator login the installer created on the panel")
    local.add_argument("--panel-pass-file", help="file holding that administrator's password")
    local.add_argument("--panel-node-address", default="", help="container name of the panel's node")
    local.add_argument("--panel-node-secret-out", help="Remnawave: file that receives the node SECRET_KEY")
    local.add_argument("--panel-node-cert-file", help="PasarGuard: file holding the node certificate")
    local.add_argument("--panel-node-key-file", help="PasarGuard: file holding the node API key")
    local.add_argument("--telemt-url", help="control API URL of the telemt Telegram proxy")
    local.add_argument("--telemt-token-file", help="file holding the telemt API auth_header value")
    local.add_argument("--telemt-host", help="public address of the Telegram proxy")
    local.add_argument("--telemt-port", type=int, default=443)
    core = sub.add_parser("core", help="manage the independent platform core")
    core_sub = core.add_subparsers(dest="core_command", required=True)
    core_status = core_sub.add_parser("status", help="show core and plugin state")
    core_status.add_argument("--state-db", default="data/core.db")
    module = sub.add_parser("module", help="manage trusted built-in modules")
    module_sub = module.add_subparsers(dest="module_command", required=True)
    for name in ("list", "info", "plan", "install", "enable", "disable", "health", "update", "uninstall"):
        command = module_sub.add_parser(name)
        command.add_argument("module_id", nargs="?" if name == "list" else None)
        command.add_argument("--state-db", default="data/core.db")
        if name == "uninstall":
            command.add_argument("--purge-data", action="store_true")
        if name == "update":
            command.add_argument("target_version")
    configure = module_sub.add_parser("configure")
    configure.add_argument("module_id")
    configure.add_argument("--set", action="append", default=[])
    configure.add_argument("--state-db", default="data/core.db")
    return parser


def load_settings(**overrides: Any) -> Settings:
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        lines = ["Invalid configuration:"]
        for error in exc.errors(include_url=False):
            where = ".".join(str(p) for p in error.get("loc", ())) or "settings"
            lines.append(f"  {where.upper()}: {error.get('msg')}")
        raise SystemExit("\n".join(lines)) from None


async def cmd_run(settings: Settings) -> int:
    from app.runtime import Runtime, init_sentry, setup_logging

    handles = setup_logging(settings)
    init_sentry(settings)
    runtime = Runtime(settings, log_buffer=handles.buffer)
    return await runtime.run()


async def cmd_migrate(settings: Settings, revision: str) -> int:
    from app.db.session import create_engine
    from app.runtime import upgrade_database

    engine = create_engine(settings.database_url)
    try:
        current = await upgrade_database(engine, revision)
    finally:
        await engine.dispose()
    print(f"database schema at revision {current}")
    return 0


async def cmd_check(settings: Settings, *, offline: bool) -> int:
    from app.runtime import run_checks

    results = await run_checks(settings, telegram=not offline)
    marks = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    for result in results:
        print(f"[{marks[result.status]}] {result.name}: {result.detail}")
    return 1 if any(r.status == "fail" for r in results) else 0


def _call_with(func: Callable[..., Any], available: dict[str, Any], *args: Any) -> Any:
    """Call ``func`` passing only the keyword arguments it declares."""
    params = inspect.signature(func).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return func(*args, **available)
    return func(*args, **{k: v for k, v in available.items() if k in params})


async def _with_services(settings: Settings, action: Callable[[dict[str, Any]], Any]) -> Any:
    """Run ``action`` with a database session factory, settings and the secret box available."""
    from app.core.settings_provider import SettingsProvider
    from app.db.session import create_engine, create_session_factory
    from app.services import kv
    from app.utils.crypto import SecretBox

    engine = create_engine(settings.database_url)
    try:
        session_factory = create_session_factory(engine)
        secret_box = SecretBox(await kv.get_or_create_secret(session_factory, settings))
        provider = SettingsProvider(settings, session_factory, secret_box)
        current = await provider.load()
        available = {
            "engine": engine,
            "session_factory": session_factory,
            "settings": current,
            "secret_box": secret_box,
            "settings_provider": provider,
        }
        result = action(available)
        if inspect.isawaitable(result):
            result = await result
        return result
    finally:
        await engine.dispose()


async def cmd_create_admin_link(settings: Settings, tg_id: int | None) -> int:
    target = tg_id or (settings.admin_ids[0] if settings.admin_ids else None)
    if target is None:
        print("ADMIN_IDS is empty: pass --tg-id", file=sys.stderr)
        return 2
    auth = importlib.import_module("app.web.auth")
    link = await _with_services(settings, lambda deps: _call_with(auth.create_login_link, {**deps, "tg_id": target}))
    print(link)
    return 0


async def cmd_backup(settings: Settings) -> int:
    backups = importlib.import_module("app.services.backups")

    async def create(deps: dict[str, Any]) -> Any:
        current = deps["settings"]  # BACKUP_DIR / BACKUP_PASSWORD may be changed in the admin settings
        return await backups.create(
            deps["engine"], backups.directory_from(current), password=backups.password_from(current)
        )

    info = await _with_services(settings, create)
    print(f"backup created: {info.path}")
    return 0


async def cmd_restore(settings: Settings, file: str, *, confirmed: bool, password: str | None = None) -> int:
    from pathlib import Path

    from app.db.session import create_engine
    from app.runtime import upgrade_database

    path = Path(file)
    if not await asyncio.to_thread(path.is_file):
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    if not confirmed:
        print("restoring replaces ALL current data; repeat with --yes to continue", file=sys.stderr)
        return 2
    backups = importlib.import_module("app.services.backups")
    # Straight into the database: nothing may be written before (a generated secret would make it non-empty, and
    # the backup brings its own).
    engine = create_engine(settings.database_url)
    try:
        if settings.auto_migrate:
            await upgrade_database(engine)
        report = await backups.restore(engine, path, password=password or backups.password_from(settings), wipe=True)
    except backups.BackupError as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(f"backup restored: {report.total} rows")
    return 0


def _read_secret_file(path: str) -> str:
    from pathlib import Path

    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc.strerror or exc}") from None
    if not value:
        raise SystemExit(f"{path} is empty")
    return value


def build_local_setup(args: argparse.Namespace) -> Any:
    """:class:`~app.services.local_setup.LocalSetup` from the ``bootstrap-local`` arguments."""
    from app.db.models import PanelKind
    from app.services.local_setup import LocalSetup, TelemtSetup, XuiSetup
    from app.services.panel_setup import PanelInstall

    xui = None
    if args.xui_url:
        missing = [
            name
            for name, value in (
                ("--xui-user", args.xui_user),
                ("--xui-pass-file", args.xui_pass_file),
                ("--reality-port", args.reality_port),
                ("--reality-sni", args.reality_sni),
                ("--endpoint-address", args.endpoint_address),
            )
            if not value
        ]
        if missing:
            raise SystemExit("with --xui-url these are required too: " + ", ".join(missing))
        xui = XuiSetup(
            url=args.xui_url,
            username=args.xui_user,
            password=_read_secret_file(args.xui_pass_file),
            reality_port=args.reality_port,
            reality_sni=args.reality_sni,
            reality_dest=args.reality_dest,
            endpoint_address=args.endpoint_address,
            endpoint_port=args.endpoint_port,
            socks_port=args.socks_port,
            socks_host=args.socks_host,
            ss_port=args.ss_port,
        )
    telemt = None
    if args.telemt_url:
        if not (args.telemt_token_file and args.telemt_host):
            raise SystemExit("with --telemt-url these are required too: --telemt-token-file, --telemt-host")
        telemt = TelemtSetup(
            url=args.telemt_url,
            api_token=_read_secret_file(args.telemt_token_file),
            public_host=args.telemt_host,
            public_port=args.telemt_port,
        )
    panel = None
    if args.panel_kind:
        if xui is not None:
            raise SystemExit("--panel-kind and --xui-url cannot be combined")
        missing = [
            name
            for name, value in (
                ("--panel-url", args.panel_url),
                ("--panel-user", args.panel_user),
                ("--panel-pass-file", args.panel_pass_file),
                ("--reality-port", args.reality_port),
                ("--reality-sni", args.reality_sni),
                ("--endpoint-address", args.endpoint_address),
            )
            if not value
        ]
        if missing:
            raise SystemExit("with --panel-kind these are required too: " + ", ".join(missing))
        panel = PanelInstall(
            kind=PanelKind(args.panel_kind),
            url=args.panel_url,
            admin_user=args.panel_user,
            admin_password=_read_secret_file(args.panel_pass_file),
            reality_port=args.reality_port,
            reality_sni=args.reality_sni,
            reality_dest=args.reality_dest,
            endpoint_address=args.endpoint_address,
            endpoint_port=args.endpoint_port,
            node_address=args.panel_node_address,
            node_secret_out=args.panel_node_secret_out,
            node_cert=_read_secret_file(args.panel_node_cert_file) if args.panel_node_cert_file else None,
            node_api_key=_read_secret_file(args.panel_node_key_file) if args.panel_node_key_file else None,
        )
    if xui is None and telemt is None and panel is None:
        raise SystemExit("nothing to do: pass --xui-url, --panel-kind and/or --telemt-url")
    return LocalSetup(args.location_name, args.location_flag, xui, telemt, panel)


async def cmd_bootstrap_local(settings: Settings, setup: Any) -> int:
    from app.services.local_setup import SetupError, bootstrap_local
    from app.services.panels.registry import PanelRegistry

    async def run(deps: dict[str, Any]) -> Any:
        registry = PanelRegistry(deps["session_factory"], deps["settings"], deps["secret_box"])
        try:
            return await bootstrap_local(deps["session_factory"], registry, deps["secret_box"], setup)
        finally:
            await registry.close()

    try:
        result = await _with_services(settings, run)
    except SetupError as exc:
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True, ensure_ascii=False))
    return 0


async def dispatch(args: argparse.Namespace) -> int:
    if args.command in {"core", "module"}:
        return await cmd_core(args)
    if args.command == "reality-config":
        from app.services.panel_setup import new_reality_config

        print(json.dumps(new_reality_config(args.port, args.sni, args.dest), indent=2))
        return 0
    overrides: dict[str, Any] = {"role": args.role} if args.role else {}
    settings = load_settings(**overrides)
    command = args.command or "run"
    if command != "run":
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if command == "run":
        return await cmd_run(settings)
    if command == "migrate":
        return await cmd_migrate(settings, args.revision)
    if command == "check":
        return await cmd_check(settings, offline=args.offline)
    if command == "create-admin-link":
        return await cmd_create_admin_link(settings, args.tg_id)
    if command == "backup":
        return await cmd_backup(settings)
    if command == "bootstrap-local":
        return await cmd_bootstrap_local(settings, build_local_setup(args))
    if command == "restore":
        return await cmd_restore(settings, args.file, confirmed=args.yes, password=args.password)
    raise SystemExit(f"unknown command {command}")


async def cmd_core(args: argparse.Namespace) -> int:
    """Run Core/plugin commands without loading the application settings or Telegram token."""
    from pathlib import Path

    from app.core.plugins import CoreRuntime, UnsupportedOperation
    from app.core.plugins.ownership import CoreOwnership, OwnershipBusy

    readonly = args.command == "core" or args.module_command in {"list", "info", "plan"}
    ownership = CoreOwnership(Path(args.state_db))
    if not readonly:
        try:
            ownership.acquire()
        except OwnershipBusy as exc:
            print(f"core command failed: {exc}", file=sys.stderr)
            return 2
    runtime = CoreRuntime(Path(args.state_db), read_only=readonly)
    try:
        if args.command == "core":
            print(json.dumps(runtime.status(), indent=2))
            return 0
        command = args.module_command
        if command == "list":
            for manifest in runtime.registry.available():
                state = runtime.store.state(manifest.identifier)
                print(f"{manifest.identifier}\t{state.status.value if state else 'available'}\t{manifest.title}")
            return 0
        plugin = runtime.registry.get(args.module_id)
        if command == "info":
            print(
                json.dumps(
                    {
                        "id": plugin.manifest.identifier,
                        "version": plugin.manifest.version,
                        "permissions": plugin.manifest.permissions,
                        "description": plugin.manifest.description,
                    },
                    indent=2,
                )
            )
            return 0
        lifecycle = runtime.lifecycle
        if command == "plan":
            print(json.dumps(lifecycle.preview(args.module_id, "inspect"), indent=2))
            return 0
        if command == "install":
            await lifecycle.install(args.module_id)
        elif command == "enable":
            await lifecycle.enable(args.module_id)
        elif command == "disable":
            await lifecycle.disable(args.module_id)
        elif command == "health":
            report = await lifecycle.healthcheck(args.module_id)
            print(json.dumps({"healthy": report.healthy, "detail": report.detail}))
            return 0 if report.healthy else 1
        elif command == "update":
            await lifecycle.update(args.module_id, args.target_version)
        elif command == "uninstall":
            await lifecycle.uninstall(args.module_id, purge_data=args.purge_data)
        elif command == "configure":
            values: dict[str, str] = {}
            for item in args.set:
                key, separator, value = item.partition("=")
                if not separator or not key:
                    raise SystemExit("--set requires KEY=VALUE")
                values[key] = value
            await lifecycle.configure(args.module_id, values)
        print(f"{args.module_id}: {command} completed")
        return 0
    except (LookupError, RuntimeError, UnsupportedOperation, ValueError) as exc:
        print(f"core command failed: {exc}", file=sys.stderr)
        return 2
    finally:
        runtime.close()
        ownership.release()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(dispatch(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
