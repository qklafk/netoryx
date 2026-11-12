"""The one-line installer: dry-run plans per operating system, refusals and the .env helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "os-release"
TOKEN = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"


def _find_bash() -> str | None:
    candidates = [
        os.environ.get("NETORYX_TEST_BASH"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        shutil.which("bash"),
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        try:
            done = subprocess.run(
                [candidate, "-c", "echo ok; test -f install.sh"], cwd=ROOT, capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0 and done.stdout.strip() == "ok":
            return candidate
    return None


BASH = _find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="a POSIX bash is needed to exercise install.sh")


def _run(
    *args: str, os_release: str = "ubuntu-24.04", extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    env = {
        **{k: v for k, v in os.environ.items() if k not in {"LC_ALL", "LC_MESSAGES"}},
        "LANG": "C",
        "NETORYX_OS_RELEASE": f"tests/fixtures/os-release/{os_release}",
        "NETORYX_PUBLIC_IP": "203.0.113.9",
        "NETORYX_DIR": "/tmp/netoryx-dry-run-test",
        **(extra_env or {}),
    }
    return subprocess.run(
        [BASH, "install.sh", "--dry-run", "--yes", "--bot-token", TOKEN, "--admin-id", "42", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


@pytest.mark.parametrize(
    ("sample", "manager", "docker_repo", "firewall", "codename"),
    [
        ("ubuntu-22.04", "apt", "ubuntu", "ufw", "jammy"),
        ("ubuntu-24.04", "apt", "ubuntu", "ufw", "noble"),
        ("ubuntu-26.04", "apt", "ubuntu", "ufw", "resolute"),
        ("debian-12", "apt", "debian", "ufw", "bookworm"),
        ("debian-13", "apt", "debian", "ufw", "trixie"),
        ("almalinux-9", "dnf", "centos", "firewalld", ""),
        ("rocky-9", "dnf", "centos", "firewalld", ""),
    ],
)
def test_supported_systems_get_a_plan(
    sample: str, manager: str, docker_repo: str, firewall: str, codename: str
) -> None:
    done = _run("--with-xui", "--with-socks", os_release=sample)
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert f"package manager {manager}, Docker repository {docker_repo}, firewall {firewall}" in out
    if codename:
        assert f"download.docker.com/linux/{docker_repo} {codename}" in out  # taken from os-release, not hardcoded
    if manager == "dnf":
        assert "dnf install -y" in out and "docker-ce.repo" in out
        assert "firewall-cmd --permanent --zone=trusted --add-source=172.16.0.0/12" in out
    else:
        assert "apt-get install -y docker-ce" in out
        assert "ufw allow from 172.16.0.0/12 to any port 2053" in out
        assert "ufw allow from 172.16.0.0/12 to any port 8445" in out
    assert "dry run: nothing was changed" in out


@pytest.mark.parametrize("arch", ["x86_64", "aarch64", "arm64"])
def test_both_architectures_are_accepted(arch: str) -> None:
    done = _run(extra_env={"NETORYX_ARCH": arch})
    assert done.returncode == 0, done.stderr
    assert ("aarch64" if arch != "x86_64" else "x86_64") in done.stdout


def test_unknown_architecture_is_refused() -> None:
    done = _run(extra_env={"NETORYX_ARCH": "armv7l"})
    assert done.returncode != 0
    assert "unsupported CPU architecture" in done.stderr


@pytest.mark.parametrize("sample", ["ubuntu-20.04", "centos-7", "arch"])
def test_unsupported_systems_are_refused_with_the_supported_list(sample: str) -> None:
    done = _run(os_release=sample)
    assert done.returncode != 0
    assert "unsupported system" in done.stderr
    assert "Ubuntu 22.04 / 24.04 / 26.04" in done.stderr and "Rocky Linux 9" in done.stderr
    assert "--force-os" in done.stderr


def test_force_os_maps_by_id_like() -> None:
    done = _run("--force-os", os_release="centos-7")
    assert done.returncode == 0, done.stderr
    assert "package manager dnf" in done.stdout
    assert "continuing because of --force-os" in done.stderr
    assert _run("--force-os", os_release="arch").returncode != 0  # nothing to map it to


def test_the_plan_covers_the_requirements() -> None:
    done = _run("--with-xui", "--with-socks", "--with-tgproxy", "--socks-port", "2081", "--domain", "vpn.example.org")
    assert done.returncode == 0, done.stderr
    out = done.stdout
    for expected in (
        "ufw allow 22/tcp",
        "ufw allow 443/tcp",
        "ufw allow 2081/tcp",
        "vpn.vpn.example.org -> reality, petrovich.ru -> tgproxy",
        "docker compose up -d --build",
        "bootstrap-local",
        "--reality-dest 127.0.0.1:8444",
        "--socks-port 2081",
        "--xui-pass-file /tmp/netoryx-xui-pass",
        "owned by uid 65532",
    ):
        assert expected in out, expected
    assert TOKEN not in out + done.stderr  # secrets are never echoed


def test_dry_run_changes_nothing() -> None:
    target = Path("/tmp/netoryx-dry-run-test")
    done = _run("--with-xui")
    assert done.returncode == 0, done.stderr
    assert not target.exists()


def test_without_a_panel_only_the_core_is_planned() -> None:
    done = _run("--panel", "none")
    assert done.returncode == 0, done.stderr
    assert "not requested" in done.stdout  # the VPN step is skipped
    assert "bootstrap-local" not in done.stdout
    assert "tgproxy" not in done.stdout and "-> reality" not in done.stdout


def test_unknown_option_is_an_error() -> None:
    done = _run("--nope")
    assert done.returncode != 0 and "unknown option" in done.stderr


def _source(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, "-c", f"source install.sh; INSTALL_DIR='{tmp_path.as_posix()}'; {script}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def test_env_values_are_quoted_and_replaced(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    done = _source(
        "env_set PLAIN abc123; env_set SPACED 'two words & $x'; env_set PLAIN changed; echo \"[$(env_get SPACED)]\"",
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert (tmp_path / ".env").read_text(encoding="utf-8").splitlines() == ["PLAIN=changed", "SPACED='two words & $x'"]
    assert "[two words & $x]" in done.stdout


def test_env_rejects_values_that_cannot_be_quoted(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    assert _source('env_set BAD "it\'s"', tmp_path).returncode != 0


def test_edge_and_telemt_files_are_generated_once(tmp_path: Path) -> None:
    (tmp_path / "deploy" / "haproxy").mkdir(parents=True)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    script = (
        f"LANG_UI=en; load_messages; SECRETS_DIR='{secrets.as_posix()}'; DRY=0; LOG_FILE=/dev/null; "
        "SERVER_IP=203.0.113.9; "
        "TG_DOMAIN=petrovich.ru; DOMAIN=example.org; WITH_XUI=1; HAS_PANEL=1; WITH_TGPROXY=1; chown() { :; }; "
        "write_edge_files >/dev/null; true"
    )
    assert _source(script, tmp_path).returncode == 0
    assert (tmp_path / "deploy" / "haproxy" / "sni.map").read_text(encoding="utf-8").splitlines()[1:] == [
        "vpn.example.org reality",
        "petrovich.ru tgproxy",
    ]
    config = (tmp_path / "deploy" / "telemt" / "config.toml").read_text(encoding="utf-8")
    credentials = (secrets / "telemt-credentials").read_text(encoding="utf-8")
    assert 'tls_domain = "petrovich.ru"' in config and "AUTH_HEADER=" in credentials
    token = next(line for line in credentials.splitlines() if line.startswith("AUTH_HEADER=")).split("'")[1]
    assert f'auth_header = "{token}"' in config
    # a second run keeps the generated secrets
    assert _source(script, tmp_path).returncode == 0
    assert (secrets / "telemt-credentials").read_text(encoding="utf-8") == credentials
    assert (tmp_path / "deploy" / "telemt" / "config.toml").read_text(encoding="utf-8") == config


def test_messages_exist_in_both_languages() -> None:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    ru_start = text.index('if [ "$LANG_UI" = ru ]; then')
    en_start = text.index("  else\n    M_S_CHECK=")
    end = text.index("  fi\n}\n", en_start)
    keys = {
        lang: {line.split("=", 1)[0].strip() for line in block.splitlines() if line.strip().startswith("M_")}
        for lang, block in (("ru", text[ru_start:en_start]), ("en", text[en_start:end]))
    }
    assert keys["ru"] == keys["en"] and len(keys["ru"]) > 50


def test_russian_output_and_numbered_steps() -> None:
    done = _run("--lang", "ru")
    assert done.returncode == 0, done.stderr
    assert "[1/9] Проверяю систему" in done.stdout and "[9/9] Завершаю установку" in done.stdout
    assert "(пробный запуск: ничего не изменено)" in done.stdout


def test_language_follows_lang_variable() -> None:
    done = _run(extra_env={"LANG": "ru_RU.UTF-8"})
    assert "[1/9] Проверяю систему" in done.stdout
    english = _run()
    assert "[1/9] Checking the system" in english.stdout and "[9/9] Finishing" in english.stdout
    assert _run("--lang", "de").returncode != 0


def test_service_name_is_validated_and_defaults_to_vpn() -> None:
    assert _run("--brand-name", "My VPN").returncode == 0
    assert _run("--brand-name", "x" * 41).returncode != 0


def test_answers_are_validated_with_friendly_messages(tmp_path: Path) -> None:
    done = _source(
        'set +e; Q=$(printf "\\047"); LANG_UI=en; load_messages; validate_brand "it${Q}s" >/dev/null; echo "rc=$?"; '
        'validate_admin abc; echo "rc=$?"; validate_admin 123456789,12345; echo "rc=$?"; '
        'validate_token 12345:short; echo "rc=$?"; validate_brand ""; echo "rc=$?"; validate_brand MyVPN; echo "rc=$?"',
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "A number is needed" in out and "does not look like a token" in out and "1 to 40 characters" in out
    assert out.count("rc=1") == 4 and out.count("rc=0") == 2


def test_progress_marks_and_failure_message(tmp_path: Path) -> None:
    done = _source(
        "LANG_UI=en; load_messages; DRY=1; step 'One'; step 'Two'; step_ok; STEP_OPEN=1; STEP_TITLE=Two; "
        "(false) || true; on_error 7",
        tmp_path,
    )
    assert "[1/9] One..." in done.stdout and "[2/9] Two..." in done.stdout
    assert "This step failed" in done.stderr and "run the installer again" in done.stderr


PANEL_HOSTS = {
    "remnawave": ("http://remnawave:3000", "remnanode"),
    "marzban": ("http://marzban:8880", "marzban"),
    "pasarguard": ("http://pasarguard:8000", "pg-node.internal"),
    "marzneshin": ("http://marzneshin:8000", "marznode"),
}


def test_the_default_panel_is_3x_ui() -> None:
    done = _run("--domain", "vpn.example.org")
    assert done.returncode == 0, done.stderr
    assert "3x-ui + Reality" in done.stdout
    assert "--xui-url" in done.stdout and "--panel-kind" not in done.stdout


@pytest.mark.parametrize("panel", sorted(PANEL_HOSTS))
def test_each_panel_has_a_complete_dry_run_plan(panel: str) -> None:
    url, reality_host = PANEL_HOSTS[panel]
    done = _run("--panel", panel, "--domain", "vpn.example.org")
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert f"COMPOSE_FILE=docker-compose.yml:deploy/panels/{panel}/compose.yml" in out
    assert f"REALITY_HOST={reality_host}" in out
    assert "-> reality" in out
    assert f"--panel-kind {panel} --panel-url {url}" in out
    assert "--reality-dest caddy:8444" in out
    assert "--xui-url" not in out
    assert f"/root/netoryx/{panel}-credentials" in out
    # the secrets are only handed over as files, never as values on a command line
    assert "--panel-pass-file /tmp/netoryx-panel-pass" in out
    assert "--panel-pass " not in out
    assert (ROOT / "deploy" / "panels" / panel / "compose.yml").is_file()


@pytest.mark.parametrize("panel", sorted(PANEL_HOSTS))
def test_socks_and_shadowsocks_are_xui_only(panel: str) -> None:
    done = _run("--panel", panel, "--with-socks", "--with-shadowsocks")
    assert done.returncode == 0, done.stderr
    assert "work with the 3x-ui panel only" in done.stderr
    assert "--socks-port" not in done.stdout and "--ss-port" not in done.stdout
    assert "SOCKS5 on" not in done.stdout


def test_socks_selects_3x_ui_when_no_panel_is_named() -> None:
    done = _run("--with-socks")
    assert done.returncode == 0, done.stderr
    assert "--socks-port 2080" in done.stdout


def test_panel_none_registers_nothing_and_unknown_panels_are_refused() -> None:
    done = _run("--panel", "none", "--with-tgproxy")
    assert done.returncode == 0, done.stderr
    assert "--panel-kind" not in done.stdout and "--xui-url" not in done.stdout
    assert "--telemt-url" in done.stdout
    refused = _run("--panel", "hiddify")
    assert refused.returncode != 0
    assert "--panel must be one of" in refused.stderr


def test_remnawave_node_is_started_after_the_key_exists() -> None:
    done = _run("--panel", "remnawave")
    assert "--panel-node-secret-out /tmp/netoryx-rw-node-key" in done.stdout
    assert "start the remnanode container" in done.stdout


def test_ad_blocking_note_for_other_panels() -> None:
    done = _run("--panel", "marzban")
    assert "Ad blocking works with the 3x-ui panel only" in done.stdout


def test_panel_compose_files_pin_versions_and_publish_nothing_publicly() -> None:
    for panel in PANEL_HOSTS:
        text = (ROOT / "deploy" / "panels" / panel / "compose.yml").read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip().startswith("image:"):
                assert not line.rstrip().endswith((":latest", "-alpine")) or "valkey" in line, line
                assert ":" in line.split("image:")[1], line
        for line in text.splitlines():
            if line.strip().startswith('- "') and ":" in line and "ports" not in line:
                assert '"127.0.0.1:' in line, line


@pytest.mark.parametrize("panel", ["xui", "remnawave", "marzneshin", "pasarguard"])
def test_russian_dry_run_has_no_english_detail_lines(panel: str) -> None:
    done = _run("--lang", "ru", "--panel", panel, "--with-tgproxy", "--with-socks")
    assert done.returncode == 0, done.stderr
    english = (
        "already installed",
        "already configured",
        "copying from",
        "bot and web",
        "kept open",
        "bypass ufw",
        "would create",
        "would write",
        "would set",
        "would store",
        "would wait",
        "not requested",
        "no swap file needed",
        "free disk",
        "Installer log",
    )
    text = done.stdout + done.stderr
    for phrase in english:
        assert phrase not in text, phrase
    assert "Состав: бот и веб-часть" in text
