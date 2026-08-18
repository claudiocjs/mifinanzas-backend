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

APP_VERSION = "0.6.0"
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

app = v059.app

# Replace only the reconciliation feed. Everything else is inherited.
app.router.routes = [
    route for route in app.router.routes
    if getattr(route, "path", None) != "/device/mercadopago/reconciliation"
]


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _outbound_transfer_item(group: dict[str, Any]) -> dict[str, Any] | None:
    semantic = str(group.get("semantic_kind") or "").upper()
    transaction_type = str(group.get("transaction_type") or "").upper()
    net = _safe_float(group.get("net_amount"))
    if net >= 0:
        return None
    if semantic != "TRANSFER_OUT" and transaction_type not in {"PAYOUTS", "WITHDRAWAL"}:
        return None

    source_id = str(group.get("source_id") or "").strip()
    if not source_id:
        return None

    return {
        "external_id": f"mp:transfer:{source_id}",
        "source_id": source_id,
        "date": group.get("first_date") or group.get("last_date"),
        "amount": round(abs(net), 2),
        "currency": group.get("currency") or "ARS",
        "transaction_type": transaction_type or "PAYOUTS",
        "semantic_kind": semantic or "TRANSFER_OUT",
        "description": "Transferencia saliente",
        "counterparty_name": None,
        "destination_hint": None,
        "account_money_only": not bool(group.get("payment_search_match")),
        "automatic_finance_posting": False,
        "requires_reconciliation": True,
    }


@app.get("/device/mercadopago/reconciliation")
async def reconciliation_feed_v060(
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

    _state, groups, _payment_by_id = await v055._canonical_context()
    outbound = []
    for group in groups:
        item = _outbound_transfer_item(group)
        if item:
            outbound.append(item)
    outbound.sort(key=lambda x: (x.get("date") or "", x.get("source_id") or ""), reverse=True)

    payload["outbound_transfers"] = outbound
    payload["outbound_transfer_summary"] = {
        "count": len(outbound),
        "total": round(sum(_safe_float(x.get("amount")) for x in outbound), 2),
    }
    payload.setdefault("policy", {})["outbound_transfers_are_review_only"] = True
    payload["policy"]["card_or_debt_payment_linking_is_local_android_decision"] = True
    payload["policy"]["outbound_transfer_is_not_automatically_an_expense"] = True
    return payload
