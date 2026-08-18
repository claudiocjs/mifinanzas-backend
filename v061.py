from __future__ import annotations

from typing import Any

from fastapi import Depends

import main
import main_v050 as base
import v051
import v052
import v053
import v054
import v055
import v056
import v057
import v058
import v059
import v060

APP_VERSION = "0.6.1"
base.APP_VERSION = APP_VERSION
base.app.version = APP_VERSION
main.APP_VERSION = APP_VERSION
main.app.version = APP_VERSION
v051.main.APP_VERSION = APP_VERSION
v052.APP_VERSION = APP_VERSION
v053.APP_VERSION = APP_VERSION
v054.APP_VERSION = APP_VERSION
v055.APP_VERSION = APP_VERSION
v056.APP_VERSION = APP_VERSION
v057.APP_VERSION = APP_VERSION
v058.APP_VERSION = APP_VERSION
v059.APP_VERSION = APP_VERSION
v060.APP_VERSION = APP_VERSION

app = v060.app

# Replace only reconciliation. All auth/pairing/payment-detail routes are inherited.
app.router.routes = [
    route for route in app.router.routes
    if getattr(route, "path", None) != "/device/mercadopago/reconciliation"
]


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _generic_transfer_item(group: dict[str, Any], payment: dict[str, Any] | None) -> dict[str, Any] | None:
    net = _safe_float(group.get("net_amount"))
    if net >= 0:
        return None

    semantic = _clean_text(group.get("semantic_kind")).upper()
    transaction_type = _clean_text(group.get("transaction_type")).upper()
    ledger_class = _clean_text(group.get("ledger_class")).upper()
    p = payment or {}
    operation_type = _clean_text(p.get("operation_type") or group.get("operation_type")).lower()
    payment_type = _clean_text(p.get("payment_type_id") or group.get("payment_type_id")).lower()

    # Never turn an ordinary approved merchant purchase into a transfer candidate.
    if operation_type == "regular_payment":
        return None

    transfer_like = (
        semantic == "TRANSFER_OUT"
        or transaction_type in {"PAYOUTS", "WITHDRAWAL", "WITHDRAWAL_CANCEL"}
        or operation_type == "money_transfer"
        or ledger_class == "TRANSFER_OR_BALANCE_MOVE"
    )
    if not transfer_like:
        return None

    source_id = _clean_text(group.get("source_id"))
    if not source_id:
        return None

    description = _clean_text(p.get("description") or group.get("description"))
    if not description or description.lower() in {"varios", "mercado pago"}:
        description = "Transferencia saliente"

    return {
        "external_id": f"mp:transfer:{source_id}",
        "source_id": source_id,
        "date": group.get("first_date") or group.get("last_date"),
        "amount": round(abs(net), 2),
        "currency": p.get("currency") or group.get("currency") or "ARS",
        "transaction_type": transaction_type or "TRANSFER",
        "semantic_kind": semantic or "TRANSFER_OUT_CANDIDATE",
        "ledger_class": ledger_class,
        "operation_type": operation_type,
        "payment_type_id": payment_type,
        "description": description,
        "counterparty_name": None,
        "destination_hint": None,
        "account_money_only": not bool(group.get("payment_search_match")),
        "automatic_finance_posting": False,
        "requires_reconciliation": True,
        "classification_required": True,
        "allowed_purposes": [
            "PAYMENT_SERVICE_PRODUCT_MERCHANT",
            "OWN_ACCOUNT_TRANSFER",
            "INVESTMENT_SAVINGS",
            "LOAN_TO_THIRD_PARTY",
            "CARD_DEBT_PAYMENT",
            "OTHER_TRANSFER",
        ],
    }


@app.get("/device/mercadopago/reconciliation")
async def reconciliation_feed_v061(
    days: int = 30,
    device=Depends(main._device_auth),
):
    payload = await v059.reconciliation_feed_v059(days=days, device=device)
    if not isinstance(payload, dict):
        return payload

    payload["backend_version"] = APP_VERSION
    if not payload.get("ready"):
        payload["outbound_transfers"] = []
        return payload

    _state, groups, payment_by_id = await v055._canonical_context()
    outbound: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        source_id = _clean_text(group.get("source_id"))
        payment = payment_by_id.get(source_id)
        item = _generic_transfer_item(group, payment)
        if item and item["external_id"] not in seen:
            seen.add(item["external_id"])
            outbound.append(item)

    outbound.sort(key=lambda x: (x.get("date") or "", x.get("source_id") or ""), reverse=True)
    payload["outbound_transfers"] = outbound
    payload["outbound_transfer_summary"] = {
        "count": len(outbound),
        "total": round(sum(_safe_float(x.get("amount")) for x in outbound), 2),
    }
    payload.setdefault("policy", {})["all_transfer_like_outflows_require_local_classification"] = True
    payload["policy"]["outbound_transfer_is_not_automatically_an_expense"] = True
    payload["policy"]["loan_repayment_match_rule"] = "exact amount + incoming candidate within 24 hours; auto-link only when unique, otherwise ask"
    payload["policy"]["investment_or_savings_requires_platform"] = True
    payload["policy"]["own_account_transfer_requires_purpose"] = True
    return payload
