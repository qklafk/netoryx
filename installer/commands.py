"""Narrow, shell-free command primitive for trusted future system adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path


class CommandError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Command:
    argv: tuple[str, ...]
    timeout_sec: float = 60
    cwd: Path | None = None

    def __post_init__(self) -> None:
        if not self.argv or any(not value or "\x00" in value for value in self.argv):
            raise ValueError("command argv must contain non-empty strings")
        if self.timeout_sec <= 0:
            raise ValueError("command timeout must be positive")


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    """Executes an allowlisted adapter-owned argv; manifests never reach this API."""

    def __init__(self, *, max_output_chars: int = 16_384, secrets: tuple[str, ...] = ()) -> None:
        self.max_output_chars = max_output_chars
        self.secrets = tuple(item for item in secrets if item)

    async def run(self, command: Command) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=str(command.cwd) if command.cwd else None,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=command.timeout_sec)
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise CommandError(f"command timed out: {self._render(command.argv)}") from exc
        assert process.returncode is not None
        result = CommandResult(command.argv, process.returncode, self._clean(stdout), self._clean(stderr))
        if result.returncode:
            raise CommandError(f"command failed ({result.returncode}): {self._render(command.argv)}; {result.stderr}")
        return result

    def _clean(self, output: bytes) -> str:
        text = output.decode("utf-8", errors="replace")[: self.max_output_chars]
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def _render(self, argv: tuple[str, ...]) -> str:
        return " ".join(self._clean(item.encode()) for item in argv)
