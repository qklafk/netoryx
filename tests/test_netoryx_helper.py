"""The `netoryx` server helper: full backup and restore (run against a stub `docker`)."""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.test_install_script import BASH, ROOT

pytestmark = pytest.mark.skipif(BASH is None, reason="a POSIX bash is needed to exercise the helper")

DOCKER_STUB = r"""#!/bin/sh
echo "$*" >>"$CALLS"
case "$1 $2" in
  "volume ls") printf '%s\n' $STUB_VOLUMES ;;
  "compose config") printf '%s\n' $STUB_KNOWN ;;
esac
if [ "$1" = run ]; then
  host=""
  for a in "$@"; do case "$a" in *:/b) host="${a%:/b}" ;; esac; done
  for a in "$@"; do
    case "$a" in
      /b/*.tar.gz)
        d="$(mktemp -d)"; echo "data of $(basename "$a")" >"$d/file"
        tar -czf "$host/$(basename "$a")" -C "$d" . ; rm -rf "$d" ;;
    esac
  done
fi
exit 0
"""


class Server:
    """A fake installation: install dir with .env, secrets dir, backups dir and a stub docker."""

    def __init__(self, tmp_path: Path, panel: str = "xui") -> None:
        self.root = tmp_path
        self.install = tmp_path / "install"
        self.secrets = tmp_path / "secrets"
        self.backups = tmp_path / "backups"
        self.calls = tmp_path / "calls.txt"
        self.bin = tmp_path / "bin"
        for directory in (
            self.install / "deploy" / "caddy-sites",
            self.install / "deploy" / "haproxy",
            self.secrets,
            self.bin,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        (self.install / ".env").write_text(f"NETORYX_PANEL={panel}\nSECRET_KEY=old\n", encoding="utf-8")
        (self.install / "deploy" / "caddy-sites" / "panel.caddy").write_text("panel {}\n", encoding="utf-8")
        (self.install / "deploy" / "haproxy" / "sni.map").write_text("vpn.example.com reality\n", encoding="utf-8")
        (self.install / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")  # code: not backed up
        (self.secrets / "xui-credentials").write_text("password\n", encoding="utf-8")
        for name, body in (("id", "#!/bin/sh\necho 0\n"), ("docker", DOCKER_STUB)):
            (self.bin / name).write_text(body, encoding="utf-8", newline="\n")
            (self.bin / name).chmod(0o755)

    def run(
        self,
        *args: str | Path,
        volumes: str = "netoryx_db-data netoryx_xui-db",
        known: str = "db-data xui-db",
        stdin: str = "",
    ) -> subprocess.CompletedProcess[str]:
        """Run the helper; paths travel in environment variables (they may hold non-ASCII characters)."""
        assert BASH is not None
        env = {
            **os.environ,
            "W_BIN": str(self.bin),
            "W_INSTALL": str(self.install),
            "W_SECRETS": str(self.secrets),
            "W_BACKUPS": str(self.backups),
            "W_CALLS": str(self.calls),
            "W_HELPER": str(ROOT / "deploy" / "netoryx"),
            "STUB_VOLUMES": volumes,
            "STUB_KNOWN": known,
            "COMPOSE_PROJECT_NAME": "netoryx",
        }
        words = []
        for index, arg in enumerate(args):
            env[f"W_ARG{index}"] = str(arg)
            words.append(f'"$(p "$W_ARG{index}")"' if isinstance(arg, Path) else f'"$W_ARG{index}"')
        script = (
            'p() { cygpath -u "$1" 2>/dev/null || printf \'%s\' "$1"; }; '
            'export PATH="$(p "$W_BIN"):$PATH" NETORYX_DIR="$(p "$W_INSTALL")" NETORYX_SECRETS_DIR="$(p "$W_SECRETS")" '
            'NETORYX_BACKUP_DIR="$(p "$W_BACKUPS")" CALLS="$(p "$W_CALLS")"; '
            f'bash "$(p "$W_HELPER")" {" ".join(words)}'
        )
        return subprocess.run(
            [BASH, "-c", script], input=stdin, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120
        )

    def calls_text(self) -> str:
        return self.calls.read_text(encoding="utf-8") if self.calls.exists() else ""

    def only_backup(self) -> Path:
        found = list(self.backups.glob("netoryx-full-*.tar.gz"))
        assert len(found) == 1, found
        return found[0]


def _members(archive: Path) -> set[str]:
    with tarfile.open(archive) as tar:
        return {m.name.removeprefix("./") for m in tar.getmembers()}


def _inner(archive: Path, name: str) -> set[str]:
    with tarfile.open(archive) as tar:
        found = next(m for m in tar.getmembers() if m.name.removeprefix("./") == name)
        member = tar.extractfile(found)
        assert member is not None
        with tarfile.open(fileobj=io.BytesIO(member.read())) as inner:
            return {m.name.removeprefix("./") for m in inner.getmembers()}


def _archive(path: Path, entries: dict[str, bytes], *, symlink: str | None = None) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if symlink:
            link = tarfile.TarInfo(symlink)
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
    return path


def _backup_file(
    tmp_path: Path,
    *,
    panel: str = "xui",
    files: dict[str, bytes] | None = None,
    volumes: dict[str, bytes] | None = None,
) -> Path:
    files_tgz = _archive(tmp_path / "f.tar.gz", files or {".env": b"NETORYX_PANEL=xui\nSECRET_KEY=restored\n"})
    secrets_tgz = _archive(tmp_path / "s.tar.gz", {"xui-credentials": b"restored\n"})
    entries = {
        "manifest": f"format=1\ncreated=20261113-120000\npanel={panel}\ncommit=abc\n".encode(),
        "files.tar.gz": files_tgz.read_bytes(),
        "secrets.tar.gz": secrets_tgz.read_bytes(),
    }
    for name, data in (volumes or {"db-data": b""}).items():
        entries[f"volumes/{name}.tar.gz"] = data or _archive(tmp_path / f"{name}.tgz", {"file": b"x"}).read_bytes()
    return _archive(tmp_path / "backup.tar.gz", entries)


def test_full_backup_holds_settings_secrets_and_every_volume_and_restarts_the_stack(tmp_path: Path) -> None:
    server = Server(tmp_path)
    done = server.run("backup", "--full")
    assert done.returncode == 0, done.stderr
    archive = server.only_backup()
    assert _members(archive) >= {
        "manifest",
        "files.tar.gz",
        "secrets.tar.gz",
        "volumes/db-data.tar.gz",
        "volumes/xui-db.tar.gz",
    }
    files = _inner(archive, "files.tar.gz")
    assert {".env", "deploy/caddy-sites/panel.caddy", "deploy/haproxy/sni.map"} <= files
    assert "docker-compose.yml" not in files  # the code comes from the repository, not from the backup
    assert "xui-credentials" in _inner(archive, "secrets.tar.gz")
    calls = server.calls_text().splitlines()
    stop = calls.index("compose stop")
    assert any(c.startswith("run ") for c in calls[stop:]) and calls[-1] == "compose up -d"
    assert "Full backup:" in done.stdout


def test_round_trip_restores_settings_and_secrets(tmp_path: Path) -> None:
    server = Server(tmp_path)
    assert server.run("backup", "--full").returncode == 0
    archive = server.only_backup()
    (server.install / ".env").write_text("NETORYX_PANEL=xui\nSECRET_KEY=changed\n", encoding="utf-8")
    (server.secrets / "xui-credentials").write_text("changed\n", encoding="utf-8")
    server.calls.unlink()
    done = server.run("restore", archive, "--yes")
    assert done.returncode == 0, done.stderr
    assert "SECRET_KEY=old" in (server.install / ".env").read_text(encoding="utf-8")
    assert (server.secrets / "xui-credentials").read_text(encoding="utf-8") == "password\n"
    calls = server.calls_text().splitlines()
    assert calls[0].startswith("compose config") and "compose down --remove-orphans" in calls
    assert any("volume create" in c and "netoryx_db-data" in c for c in calls)
    assert calls[-1] == "compose up -d"


def test_restore_asks_for_confirmation(tmp_path: Path) -> None:
    server = Server(tmp_path)
    done = server.run("restore", _backup_file(tmp_path), stdin="no\n")
    assert done.returncode != 0 and "cancelled" in done.stdout
    assert "SECRET_KEY=old" in (server.install / ".env").read_text(encoding="utf-8")
    assert "compose down" not in server.calls_text()


def test_restore_refuses_a_backup_of_another_panel(tmp_path: Path) -> None:
    server = Server(tmp_path, panel="remnawave")
    done = server.run("restore", _backup_file(tmp_path, panel="xui"), "--yes")
    assert done.returncode != 0 and "--panel xui" in done.stderr
    assert "compose down" not in server.calls_text()


@pytest.mark.parametrize(
    "files",
    [
        {"../outside": b"x"},
        {".env": b"x", "app/main.py": b"code"},
        {"deploy/caddy-sites/../../../etc/cron.d/x": b"x"},
        {"/etc/passwd": b"x"},
    ],
)
def test_restore_refuses_files_outside_the_expected_set(tmp_path: Path, files: dict[str, bytes]) -> None:
    server = Server(tmp_path)
    done = server.run("restore", _backup_file(tmp_path, files=files), "--yes")
    assert done.returncode != 0, done.stdout
    assert "compose down" not in server.calls_text()
    assert not (tmp_path / "outside").exists()


def test_restore_refuses_links_and_unknown_entries_in_the_archive(tmp_path: Path) -> None:
    server = Server(tmp_path)
    linked = _archive(tmp_path / "linked.tar.gz", {"manifest": b"format=1\npanel=xui\n"}, symlink="files.tar.gz")
    done = server.run("restore", linked, "--yes")
    assert done.returncode != 0 and "not a plain file" in done.stderr
    extra = _archive(tmp_path / "extra.tar.gz", {"manifest": b"format=1\npanel=xui\n", "run.sh": b"x"})
    done = server.run("restore", extra, "--yes")
    assert done.returncode != 0 and "unexpected entry" in done.stderr
    assert "compose down" not in server.calls_text()


def test_restore_refuses_a_volume_the_installation_does_not_use(tmp_path: Path) -> None:
    server = Server(tmp_path)
    archive = _backup_file(tmp_path, volumes={"db-data": b"", "evil": b""})
    done = server.run("restore", archive, "--yes")
    assert done.returncode != 0 and "'evil'" in done.stderr
    assert "compose down" not in server.calls_text()


def test_restore_refuses_a_file_that_is_not_a_full_backup(tmp_path: Path) -> None:
    server = Server(tmp_path)
    done = server.run("restore", _archive(tmp_path / "x.tar.gz", {"manifest": b"hello\n"}), "--yes")
    assert done.returncode != 0 and "not a full backup" in done.stderr


def test_backup_without_full_keeps_the_database_backup(tmp_path: Path) -> None:
    server = Server(tmp_path)
    done = server.run("backup")
    assert done.returncode == 0, done.stderr
    assert "-m app backup" in server.calls_text() and "backup --full" in done.stdout
    assert not server.backups.exists()
