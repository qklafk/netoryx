"""Locale files of the bot admin modules: complete in both languages and every key used in code exists."""

from __future__ import annotations

import re
from pathlib import Path

from app.core.i18n import MODULES_DIR
from app.db.models import AdminRole, PaymentStatus, TxKind, WithdrawalStatus
from app.services.partner import FraudFlagCode
from tests.test_mod_admin_support import ADMIN_MODULES, make_i18n

_KEY = re.compile(r"""["']((?:admin(?:_[a-z]+)?|nav|common|fmt|error|plural)\.[a-z0-9_.]+)["']""")
# Keys built at runtime from a prefix and a value.
DYNAMIC = {
    "admin.role.": [r.value for r in AdminRole],
    "admin.payment_status.": [s.value for s in PaymentStatus],
    "admin.tx.": [k.value for k in TxKind],
    "admin_withdrawals.status.": [s.value for s in WithdrawalStatus],
    "admin_withdrawals.flag.": [f.value for f in FraudFlagCode],
    "admin_withdrawals.notify.": ["approved", "paid", "rejected"],
    "admin_withdrawals.done.": ["approve", "paid", "reject"],
}


def test_admin_locales_are_consistent(tmp_path: Path) -> None:
    i18n = make_i18n(tmp_path)
    catalog = i18n.keys()
    module_keys = {info.key for info in catalog if info.module in ADMIN_MODULES}
    assert module_keys
    problems = [p for p in i18n.check() if p.split(":", 1)[0] in module_keys or p.startswith("admin")]
    assert problems == []


def test_every_literal_key_exists(tmp_path: Path) -> None:
    i18n = make_i18n(tmp_path)
    missing: list[str] = []
    for name in ADMIN_MODULES:
        for path in (MODULES_DIR / name).rglob("*.py"):
            for key in _KEY.findall(path.read_text(encoding="utf-8")):
                if key.endswith(".") or i18n.has(key) or any(i18n.has(f"{key}.{f}") for f in ("one", "many")):
                    continue
                missing.append(f"{path.name}: {key}")
    assert missing == []


def test_dynamic_keys_exist(tmp_path: Path) -> None:
    i18n = make_i18n(tmp_path)
    missing = [prefix + value for prefix, values in DYNAMIC.items() for value in values if not i18n.has(prefix + value)]
    assert missing == []
