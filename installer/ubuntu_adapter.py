"""Ubuntu adapter for the already-reviewed single-host Core Compose stack.

The adapter deliberately receives paths, never secret values.  The Compose file
references an externally managed ``env_file`` owned by the production host.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .commands import Command, CommandError, CommandRunner


class UbuntuCoreDeploymentAdapter:
    """Idempotently starts and verifies the Core stack through Docker Compose."""

    def __init__(
        self,
        compose_file: Path,
        *,
        runner: CommandRunner | None = None,
        health_attempts: int = 30,
        health_delay_sec: float = 2.0,
    ) -> None:
        self.compose_file = compose_file.resolve()
        self.runner = runner or CommandRunner()
        self.health_attempts = max(1, health_attempts)
        self.health_delay_sec = health_delay_sec

    def _compose(self, *args: str) -> Command:
        return Command(
            ("docker", "compose", "-f", str(self.compose_file), *args),
            cwd=self.compose_file.parent,
            timeout_sec=180,
        )

    async def preflight(self) -> None:
        if not self.compose_file.is_file():
            raise RuntimeError(f"production compose file is missing: {self.compose_file}")
        await self.runner.run(Command(("docker", "info", "--format", "{{.ServerVersion}}"), timeout_sec=30))
        await self.runner.run(self._compose("config", "-q"))

    async def apply(self) -> None:
        await self.runner.run(self._compose("up", "-d"))

    async def healthcheck(self) -> None:
        services = await self.runner.run(self._compose("ps", "--status", "running", "--services"))
        if "app" not in services.stdout.split():
            raise RuntimeError("Core app container is not running")
        # The container needs a few seconds (migrations, bot start) before the endpoint answers.
        probe = Command(("curl", "--fail", "--silent", "--show-error", "http://127.0.0.1:8080/healthz"), timeout_sec=30)
        for attempt in range(self.health_attempts):
            try:
                await self.runner.run(probe)
                return
            except CommandError:
                if attempt == self.health_attempts - 1:
                    raise
                await asyncio.sleep(self.health_delay_sec)

    async def rollback(self) -> None:
        # Intentionally non-destructive: persistent volumes and a failed runtime
        # are retained for diagnosis.  An operator chooses any rollback later.
        return None
