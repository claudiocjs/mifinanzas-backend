from collections import Counter
from typing import Any

from fastapi import Depends

import main
import main_v050 as base
import v051
import v052
import v053
import v054

APP_VERSION = "0.5.5"
base.APP_VERSION = APP_VERSION
base.app.version = APP_VERSION
main.APP_VERSION = APP_VERSION
main.app.version = APP_VERSION
v051.main.APP_VERSION = APP_VERSION
v052.APP_VERSION = APP_VERSION
v053.APP_VERSION = APP_VERSION
v054.APP_VERSION = APP_VERSION

app = v054.app

_REPLACED_PATHS = {
    "/device/mercadopago/movements",
}
app.router.routes = [
    route for route in app.router.routes
    if getattr(route, "path", None) not in _REPLACED_PATHS
]


async def _canonical_context() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    state = await v052._refresh_and_resolve()
    if not state or not v052._ready(state):
        return state or {}, [], {}

    content = await v051._download_report(state["file_name"])
    rows = v051._parse_report_csv(content)
    groups = v054._full_normalized_groups(rows)
    payments = await v054._all_payment_candidates(days=30)

    payment_by_id = {
        str(item.get("payment_id")): item
        for item in payments
        if item.get("payment_id") is not None
    }
    return state, groups, payment_by_id


def _safe_expense_item(
    group: dict[str, Any],
    payment: dict[str, Any],
) -> dict[str, Any] | None:
    ledger_class = v054._ledger_class(group, payment)
    if ledger_class != "PURCHASE_CANDIDATE":
        return None

    if float(group.get("net_amount") or 0.0) >= 0:
        return None

    if str(payment.get("operation_type") or "").lower() != "regular_payment":
        return None

    if str(payment.get("status") or "").lower() != "approved":
        return None

    source_id = str(group.get("source_id") or "").strip()
    if not source_id:
        return None

    amount = round(abs(float(group.get("net_amount") or 0.0)), 2)
    description = str(payment.get("description") or "Mercado Pago").strip() or "Mercado Pago"
    category = str(payment.get("suggested_category") or "Otros").strip() or "Otros"

    # Use the same stable ID shape Android V0.8 already knows from Payment Search.
    # This replaces old inbox rows instead of duplicating them.
    return {
        "external_id": f"mp:payment:{source_id}",
        "date": group.get("last_date") or group.get("first_date"),
        "amount": amount,
        "refunded_amount": 0.0,
        "net_candidate": amount,
        "direction": "EXPENSE",
        "description": description,
        "suggested_category": category,
        "operation_type": payment.get("operation_type") or "regular_payment",
        "status": payment.get("status") or "approved",
        "currency": "ARS",
        "source_id": source_id,
        "ledger_class": ledger_class,
        "balance_truth": round(float(group.get("net_amount") or 0.0), 2),
        "auto_import_safe": True,
    }


def _review_class(
    group: dict[str, Any],
    payment: dict[str, Any] | None,
) -> str:
    return v054._ledger_class(group, payment)


@app.get(
    "/admin/account-money/safe-import-preview",
    dependencies=[Depends(main._apk_auth)],
)
async def safe_import_preview():
    state, groups, payment_by_id = await _canonical_context()
    if not groups:
        return {
            "backend_version": APP_VERSION,
            "ready": False,
            "status": state.get("status") or "not_ready",
            "report_status": state.get("report_status"),
        }

    safe_items: list[dict[str, Any]] = []
    review_counts: Counter[str] = Counter()
    rejected_purchase_candidates = 0

    for group in groups:
        payment = payment_by_id.get(str(group.get("source_id")))
        ledger_class = _review_class(group, payment)

        if payment:
            item = _safe_expense_item(group, payment)
            if item:
                safe_items.append(item)
                continue
            if ledger_class == "PURCHASE_CANDIDATE":
                rejected_purchase_candidates += 1

        review_counts[ledger_class] += 1

    safe_items.sort(key=lambda x: (x.get("date") or "", x["external_id"]), reverse=True)
    safe_total = round(sum(float(x["amount"]) for x in safe_items), 2)

    return {
        "backend_version": APP_VERSION,
        "ready": True,
        "period": {
            "begin_date": state.get("begin_date"),
            "end_date": state.get("end_date"),
        },
        "validated_expenses": {
            "count": len(safe_items),
            "total": safe_total,
            "rejected_purchase_candidates": rejected_purchase_candidates,
            "rules": [
                "canonical ledger class = PURCHASE_CANDIDATE",
                "SETTLEMENT_NET_AMOUNT < 0",
                "Payment Search operation_type = regular_payment",
                "Payment Search status = approved",
            ],
            "sample": safe_items[:30],
        },
        "held_for_review": {
            "count": len(groups) - len(safe_items),
            "by_class": dict(review_counts),
        },
        "policy": {
            "account_money_role": "balance_truth",
            "payment_search_role": "metadata_enrichment_only",
            "payouts_are_expenses": False,
            "safe_expense_feed_enabled": True,
            "automatic_posting_to_finance_ledger": False,
            "android_v08_inbox_only": True,
        },
    }


@app.get(
    "/device/mercadopago/movements",
)
async def device_movements_v055(
    days: int = 30,
    limit: int = 50,
    device=Depends(main._device_auth),
):
    # V0.8 calls this existing route. We preserve its JSON contract but now
    # expose only canonical expenses that passed the conservative safety gate.
    safe_days = max(1, min(int(days), 30))
    safe_limit = max(1, min(int(limit), 50))

    state, groups, payment_by_id = await _canonical_context()
    if not groups:
        return {
            "source": "MERCADO_PAGO_ACCOUNT_MONEY_SAFE",
            "backend_version": APP_VERSION,
            "window_days": safe_days,
            "items": [],
            "ready": False,
        }

    items: list[dict[str, Any]] = []
    for group in groups:
        payment = payment_by_id.get(str(group.get("source_id")))
        if not payment:
            continue
        item = _safe_expense_item(group, payment)
        if item:
            items.append(item)

    items.sort(key=lambda x: (x.get("date") or "", x["external_id"]), reverse=True)

    return {
        "source": "MERCADO_PAGO_ACCOUNT_MONEY_SAFE",
        "backend_version": APP_VERSION,
        "window_days": safe_days,
        "period": {
            "begin_date": state.get("begin_date"),
            "end_date": state.get("end_date"),
        },
        "policy": {
            "account_money_role": "balance_truth",
            "payouts_are_expenses": False,
            "only_validated_expenses_exposed": True,
            "automatic_posting_to_finance_ledger": False,
        },
        "items": items[:safe_limit],
    }
