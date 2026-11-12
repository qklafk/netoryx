from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

import pytest

from installer import cli
from installer.artifact import create_artifact, load_artifact, save_artifact
from installer.commands import Command, CommandError, CommandRunner
from installer.configuration import InstallationConfiguration, LocalSecretProvider, save_public_configuration
from installer.detection import InstallationPresence, detect_installation
from installer.engine import AdapterRegistry, ExecutionEngine, ExecutionError, StepContract
from installer.journal import JournalError, StepStatus, load_journal, save_journal
from installer.models import DeploymentRole, InstallRequest, MachineFacts, ProductProfile
from installer.planner import build_plan
from installer.preflight import CheckStatus, ubuntu_preflight
from installer.registry import builtin_registry
from installer.ubuntu_adapter import UbuntuCoreDeploymentAdapter


class Adapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def preflight(self) -> None:
        self.calls.append("preflight")

    async def apply(self) -> None:
        self.calls.append("apply")
        if self.fail:
            raise RuntimeError("boom")

    async def healthcheck(self) -> None:
        self.calls.append("healthcheck")

    async def rollback(self) -> None:
        self.calls.append("rollback")


class RecordingRunner:
    def __init__(self, *, fail_on: str | None = None, services: str = "app\n") -> None:
        self.calls: list[Command] = []
        self.fail_on = fail_on
        self.services = services

    async def run(self, command: Command):
        self.calls.append(command)
        rendered = " ".join(command.argv)
        if self.fail_on and self.fail_on in rendered:
            raise CommandError("injected failure")
        if "--services" in command.argv:
            from installer.commands import CommandResult

            return CommandResult(command.argv, 0, self.services, "")
        from installer.commands import CommandResult

        return CommandResult(command.argv, 0, "ok", "")


def _hold_install_lock(
    directory: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    from filelock import FileLock

    lock = FileLock(str(Path(directory).resolve() / ".netoryx-installer.lock"))
    lock.acquire(timeout=0)
    ready.set()
    release.wait(10)
    lock.release()


def plan():
    return build_plan(
        builtin_registry(), InstallRequest(ProductProfile.PERSONAL, DeploymentRole.ALL_IN_ONE, ("core.platform",))
    )


async def test_dry_run_has_no_journal_or_adapter_side_effects(tmp_path: Path) -> None:
    adapter = Adapter()
    engine = ExecutionEngine(
        tmp_path / "journal.json", AdapterRegistry({"core.platform": adapter}), install_directory=tmp_path / "install"
    )
    journal = await engine.execute(plan(), (StepContract("core.deploy", "core.platform"),), dry_run=True)
    assert journal.steps[0].status is StepStatus.PENDING
    assert not (tmp_path / "journal.json").exists()
    assert adapter.calls == []


async def test_execution_persists_and_is_idempotent_on_resume(tmp_path: Path) -> None:
    adapter = Adapter()
    journal_path = tmp_path / "journal.json"
    engine = ExecutionEngine(
        journal_path, AdapterRegistry({"core.platform": adapter}), install_directory=tmp_path / "install"
    )
    steps = (StepContract("core.deploy", "core.platform"),)
    completed = await engine.execute(plan(), steps)
    assert completed.steps[0].status is StepStatus.SUCCEEDED
    assert adapter.calls == ["preflight", "apply", "healthcheck"]
    resumed = await engine.execute(plan(), steps, resume=True)
    assert resumed.steps[0].status is StepStatus.SUCCEEDED
    assert adapter.calls == ["preflight", "apply", "healthcheck"]


async def test_missing_adapter_and_failed_step_are_recorded(tmp_path: Path) -> None:
    engine = ExecutionEngine(tmp_path / "journal.json", AdapterRegistry(), install_directory=tmp_path / "install")
    with pytest.raises(ExecutionError, match="no deployment adapter"):
        await engine.execute(plan(), (StepContract("core.deploy", "core.platform"),))
    assert load_journal(tmp_path / "journal.json").steps[0].status is StepStatus.UNAVAILABLE

    failing = ExecutionEngine(
        tmp_path / "failed.json",
        AdapterRegistry({"core.platform": Adapter(fail=True)}),
        install_directory=tmp_path / "other",
    )
    with pytest.raises(ExecutionError, match=r"core\.deploy"):
        await failing.execute(plan(), (StepContract("core.deploy", "core.platform", rollback_supported=True),))
    assert load_journal(tmp_path / "failed.json").steps[0].status is StepStatus.ROLLBACK_REQUIRED


async def test_command_runner_redacts_output_and_enforces_timeout() -> None:
    runner = CommandRunner(secrets=("top-secret",))
    result = await runner.run(Command((sys.executable, "-c", "print('top-secret')")))
    assert result.stdout.strip() == "[REDACTED]"
    with pytest.raises(CommandError, match="timed out"):
        await runner.run(Command((sys.executable, "-c", "import time; time.sleep(1)"), timeout_sec=0.01))


def test_secrets_and_existing_installation_are_separate(tmp_path: Path) -> None:
    provider = LocalSecretProvider(tmp_path / "private" / "secrets.json")
    provider.write_missing({"bot_token": "top-secret"})
    provider.write_missing({"bot_token": "replacement"})
    assert provider.read() == {"bot_token": "top-secret"}
    public = tmp_path / "install.json"
    save_public_configuration(public, InstallationConfiguration(secret_references={"bot_token": "local:bot_token"}))
    assert "top-secret" not in public.read_text(encoding="utf-8")
    assert detect_installation(tmp_path).presence is InstallationPresence.ABSENT
    (tmp_path / ".env").write_text("existing", encoding="utf-8")
    assert detect_installation(tmp_path).presence is InstallationPresence.PARTIAL


def test_strict_ubuntu_preflight_uses_real_machine_facts() -> None:
    checks = ubuntu_preflight(MachineFacts("ubuntu", "24.04", 2, 2048, 60_000))
    assert all(check.status is CheckStatus.PASSED for check in checks[:4])


def test_fingerprint_uses_public_configuration_but_not_secret_values(tmp_path: Path) -> None:
    engine = ExecutionEngine(tmp_path / "journal.json", AdapterRegistry(), install_directory=tmp_path / "install")
    first = InstallationConfiguration(domain="one.example", secret_references={"token": "local:token"})
    same = InstallationConfiguration(domain="one.example", secret_references={"token": "local:token"})
    changed = InstallationConfiguration(domain="two.example", secret_references={"token": "local:token"})
    assert engine.fingerprint(plan(), first) == engine.fingerprint(plan(), same)
    assert engine.fingerprint(plan(), first) != engine.fingerprint(plan(), changed)
    assert "top-secret" not in engine.fingerprint(plan(), first)


def test_plan_artifact_is_non_secret_and_refuses_overwrite(tmp_path: Path) -> None:
    engine = ExecutionEngine(tmp_path / "journal.json", AdapterRegistry(), install_directory=tmp_path / "install")
    path = tmp_path / "plan.json"
    artifact = create_artifact(plan(), engine)
    save_artifact(path, artifact)
    assert load_artifact(path).fingerprint == engine.fingerprint(plan())
    assert "top-secret" not in path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        save_artifact(path, artifact)


def test_installation_lock_is_exclusive_across_processes_and_reacquirable(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    installation = tmp_path / "install"
    installation.mkdir()
    process = context.Process(target=_hold_install_lock, args=(str(installation), ready, release))
    process.start()
    try:
        assert ready.wait(10)
        competing = ExecutionEngine(tmp_path / "journal.json", AdapterRegistry(), install_directory=installation / ".")
        with pytest.raises(Exception, match="another installer owns"):
            import asyncio

            asyncio.run(competing.execute(plan(), (), resume=False))
    finally:
        release.set()
        process.join(10)
    assert process.exitcode == 0


def test_installation_lock_is_released_after_owner_is_terminated(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    installation = tmp_path / "install"
    installation.mkdir()
    process = context.Process(target=_hold_install_lock, args=(str(installation), ready, release))
    process.start()
    try:
        assert ready.wait(10)
        process.terminate()
        process.join(10)
        assert not process.is_alive()
        engine = ExecutionEngine(tmp_path / "journal.json", AdapterRegistry(), install_directory=installation)
        import asyncio

        journal = asyncio.run(engine.execute(plan(), (), resume=False))
        assert journal.steps == []
    finally:
        if process.is_alive():
            process.terminate()
            process.join()


def test_corrupted_and_unknown_schema_journals_are_not_replaced(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(JournalError):
        load_journal(path)
    assert path.read_text(encoding="utf-8") == "{broken"


def test_atomic_journal_replace_failure_preserves_previous_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "journal.json"
    engine = ExecutionEngine(path, AdapterRegistry(), install_directory=tmp_path / "install")
    journal = engine.create_journal(plan(), ())
    save_journal(path, journal)
    original = path.read_bytes()

    def fail_replace(source: str, destination: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr("installer.journal.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        save_journal(path, journal)
    assert path.read_bytes() == original


def test_cli_plan_is_read_only_and_apply_requires_artifact(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install = tmp_path / "install"
    assert (
        cli.main(
            [
                "plan",
                "--profile",
                "personal",
                "--deployment-role",
                "all-in-one",
                "--install-dir",
                str(install),
            ]
        )
        == 0
    )
    assert not install.exists()
    assert cli.main(["apply", "--yes", "--install-dir", str(install)]) == 2
    captured = capsys.readouterr()
    assert "requires --plan" in captured.err


def test_installer_image_is_separate_and_has_no_application_runtime() -> None:
    dockerfile = Path("Dockerfile.installer").read_text(encoding="utf-8")
    assert "FROM docker:29-cli" in dockerfile
    assert "COPY installer ./installer" in dockerfile
    assert "COPY app" not in dockerfile
    assert "COPY web" not in dockerfile


async def test_ubuntu_core_adapter_runs_compose_and_healthcheck(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {app: {image: example}}\n", encoding="utf-8")
    runner = RecordingRunner()
    adapter = UbuntuCoreDeploymentAdapter(compose, runner=runner)  # type: ignore[arg-type]
    await adapter.preflight()
    await adapter.apply()
    await adapter.healthcheck()
    calls = [" ".join(command.argv) for command in runner.calls]
    assert any("config -q" in call for call in calls)
    assert any("up -d" in call for call in calls)
    assert any("healthz" in call for call in calls)


async def test_ubuntu_core_adapter_failure_is_resume_safe(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {app: {image: example}}\n", encoding="utf-8")
    adapter = UbuntuCoreDeploymentAdapter(compose, runner=RecordingRunner(fail_on="up -d"))  # type: ignore[arg-type]
    engine = ExecutionEngine(
        tmp_path / "journal.json", AdapterRegistry({"core.platform": adapter}), install_directory=tmp_path / "install"
    )
    with pytest.raises(ExecutionError, match=r"core\.deploy"):
        await engine.execute(plan(), (StepContract("core.deploy", "core.platform"),))
    assert load_journal(tmp_path / "journal.json").steps[0].status is StepStatus.FAILED
    recovered = ExecutionEngine(
        tmp_path / "journal.json",
        AdapterRegistry({"core.platform": UbuntuCoreDeploymentAdapter(compose, runner=RecordingRunner())}),  # type: ignore[arg-type]
        install_directory=tmp_path / "install",
    )
    journal = await recovered.execute(plan(), (StepContract("core.deploy", "core.platform"),), resume=True)
    assert journal.steps[0].status is StepStatus.SUCCEEDED
