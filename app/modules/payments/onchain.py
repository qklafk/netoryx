"""Invoice screen of the direct blockchain gateways: address, exact amount, comment and a wallet link."""

from __future__ import annotations

from typing import Any

from app.core.i18n import Translator
from app.core.ui import Screen
from app.db.models import Payment
from app.keyboards import Button, kb
from app.services.payments.onchain import TransferInstructions


def transfer_text(_: Translator, payment: Payment, details: TransferInstructions, hours: int) -> str:
    lines = [
        _(
            "payments.onchain.head",
            amount=_.esc(details.amount),
            currency=_.esc(details.currency),
            network=_.esc(details.network),
            rub=_.money(payment.amount_kop),
        ),
        "",
        _("payments.onchain.address", address=_.esc(details.address)),
        _("payments.onchain.amount", amount=_.esc(details.amount), currency=_.esc(details.currency)),
    ]
    if details.comment:
        lines.append(_("payments.onchain.comment", comment=_.esc(details.comment)))
    lines.append("")
    lines.append(_("payments.onchain.hint_comment" if details.comment else "payments.onchain.hint_amount"))
    if details.deep_link:
        lines += ["", _("payments.onchain.link", link=_.esc(details.deep_link))]
    lines += ["", _("payments.onchain.footer", hours=_.plural(max(1, hours), "plural.hours"))]
    return "\n".join(lines)


def transfer_screen(
    _: Translator,
    payment: Payment,
    details: TransferInstructions,
    *,
    hours: int,
    check: Any,
    footer: list[Any],
) -> Screen:
    """``check`` is the "I've paid" button, ``footer`` the navigation row."""
    rows: list[list[Any]] = []
    if details.wallet_url:
        rows.append([Button.url(_("payments.onchain.open_wallet"), details.wallet_url, style="success")])
    copies = [
        Button.copy(_("payments.onchain.copy_address"), details.address),
        Button.copy(_("payments.onchain.copy_amount"), details.amount),
    ]
    if details.comment:
        copies.append(Button.copy(_("payments.onchain.copy_comment"), details.comment))
    rows += [copies, [check], footer]
    return Screen(transfer_text(_, payment, details, hours), kb(rows))
