from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import Depends, HTTPException

import main
import main_v050 as base
import v051
import v052
import v053
import v054
import v055

APP_VERSION = "0.5.6"
base.APP_VERSION = APP_VERSION
base.app.version = APP_VERSION
main.APP_VERSION = APP_VERSION
main.app.version = APP_VERSION
v051.main.APP_VERSION = APP_VERSION
v052.APP_VERSION = APP_VERSION
v053.APP_VERSION = APP_VERSION
v054.APP_VERSION = APP_VERSION
v055.APP_VERSION = APP_VERSION

app = v055.app


def _iso_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _income_candidate_item(
    group: dict[str, Any],
    payment: dict[str, Any] | None,
) -> dict[str, Any] | None:
    net = round(float(group.get("net_amount") or 0.0), 2)
    if net <= 0:
        return None

    source_id = str(group.get("source_id") or "").strip()
    if not source_id or source_id.startswith("__NO_SOURCE__"):
        return None

    ledger_class = v054._ledger_class(group, payment)
    if ledger_class in {"TRANSFER_OUT", "ADJUSTMENT_REVIEW"}:
        return None

    description = None
    operation_type = None
    status = None
    if payment:
        description = payment.get("description")
        operation_type = payment.get("operation_type")
        status = payment.get("status")

    return {
        "external_id": f"mp:balance:{source_id}:{str(group.get('transaction_type') or 'SETTLEMENT').lower()}",
        "source_id": source_id,
        "date": group.get("last_date") or group.get("first_date"),
        "amount": abs(net),
        "direction": "INCOME_CANDIDATE",
        "description": description or "Ingreso / movimiento de saldo",
        "operation_type": operation_type,
        "status": status,
        "currency": "ARS",
        "ledger_class": ledger_class,
        "balance_truth": net,
        "auto_import_safe": False,
    }


def _exact_amount_pair_suggestions(
    expenses: list[dict[str, Any]],
    incomes: list[dict[str, Any]],
    max_hours: int = 24,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    max_seconds = max_hours * 3600
    for expense in expenses:
        expense_epoch = _iso_epoch(expense.get("date"))
        if expense_epoch is None:
            continue
        candidates: list[dict[str, Any]] = []
        for income in incomes:
            if abs(float(income.get("amount") or 0.0) - float(expense.get("amount") or 0.0)) > 0.01:
                continue
            income_epoch = _iso_epoch(income.get("date"))
            if income_epoch is None:
                continue
            delta = abs(expense_epoch - income_epoch)
            if delta > max_seconds:
                continue
            candidates.append({
                "income_external_id": income["external_id"],
                "income_source_id": income["source_id"],
                "income_date": income.get("date"),
                "amount": income["amount"],
                "seconds_apart": int(delta),
                "minutes_apart": round(delta / 60.0, 1),
            })
        if candidates:
            candidates.sort(key=lambda x: x["seconds_apart"])
            result.append({
                "expense_external_id": expense["external_id"],
                "expense_source_id": expense["source_id"],
                "expense_date": expense.get("date"),
                "expense_description": expense.get("description"),
                "amount": expense["amount"],
                "match_rule": "EXACT_AMOUNT_WITHIN_24H",
                "candidates": candidates[:5],
            })
    return result


@app.get("/device/mercadopago/reconciliation")
async def reconciliation_feed(
    days: int = 30,
    device=Depends(main._device_auth),
):
    # days is retained for forward compatibility. The validated bootstrap report is 30d.
    safe_days = max(1, min(int(days), 30))
    state, groups, payment_by_id = await v055._canonical_context()
    if not groups:
        return {
            "backend_version": APP_VERSION,
            "ready": False,
            "window_days": safe_days,
            "expenses": [],
            "income_candidates": [],
            "pair_suggestions": [],
        }

    expenses: list[dict[str, Any]] = []
    incomes: list[dict[str, Any]] = []
    for group in groups:
        payment = payment_by_id.get(str(group.get("source_id")))
        if payment:
            expense = v055._safe_expense_item(group, payment)
            if expense:
                expenses.append(expense)
        income = _income_candidate_item(group, payment)
        if income:
            incomes.append(income)

    expenses.sort(key=lambda x: (x.get("date") or "", x["external_id"]), reverse=True)
    incomes.sort(key=lambda x: (x.get("date") or "", x["external_id"]), reverse=True)

    return {
        "backend_version": APP_VERSION,
        "ready": True,
        "window_days": safe_days,
        "period": {
            "begin_date": state.get("begin_date"),
            "end_date": state.get("end_date"),
        },
        "expenses": expenses,
        "income_candidates": incomes,
        "pair_suggestions": _exact_amount_pair_suggestions(expenses, incomes),
        "policy": {
            "expenses_are_validated": True,
            "income_candidates_are_not_auto_imported": True,
            "exact_amount_pair_is_only_a_suggestion": True,
            "reserves_are_internal_transfers": True,
            "third_party_contributions_must_be_confirmed": True,
            "merchant_session_grouping_is_local_android_rule": "same merchant + consecutive gap <= 10 minutes; amount may differ",
        },
    }


@app.get("/device/mercadopago/payment-detail/{source_id}")
async def payment_detail(
    source_id: str,
    device=Depends(main._device_auth),
):
    clean = source_id.strip()
    if not clean.isdigit():
        raise HTTPException(status_code=400, detail="SOURCE_ID inválido")

    response = await base._mp_get(f"https://api.mercadopago.com/v1/payments/{clean}")
    if response.status_code == 404:
        return {
            "backend_version": APP_VERSION,
            "found": False,
            "source_id": clean,
        }
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Mercado Pago payment detail HTTP {response.status_code}",
        )

    data = response.json() if response.content else {}
    if not isinstance(data, dict):
        data = {}

    card = data.get("card") if isinstance(data.get("card"), dict) else {}
    payment_method = data.get("payment_method") if isinstance(data.get("payment_method"), dict) else {}
    transaction_details = data.get("transaction_details") if isinstance(data.get("transaction_details"), dict) else {}
    payer = data.get("payer") if isinstance(data.get("payer"), dict) else {}
    additional_info = data.get("additional_info") if isinstance(data.get("additional_info"), dict) else {}

    first_name = str(payer.get("first_name") or "").strip()
    last_name = str(payer.get("last_name") or "").strip()
    payer_name = " ".join(x for x in [first_name, last_name] if x).strip() or None

    item_titles: list[str] = []
    raw_items = additional_info.get("items")
    if isinstance(raw_items, list):
        for item in raw_items[:10]:
            if isinstance(item, dict):
                title = str(item.get("title") or item.get("description") or "").strip()
                if title:
                    item_titles.append(title)

    return {
        "backend_version": APP_VERSION,
        "found": True,
        "source_id": clean,
        "id": data.get("id"),
        "description": data.get("description"),
        "status": data.get("status"),
        "operation_type": data.get("operation_type"),
        "date_created": data.get("date_created"),
        "date_approved": data.get("date_approved"),
        "currency": data.get("currency_id"),
        "transaction_amount": data.get("transaction_amount"),
        "installments": data.get("installments"),
        "installment_amount": transaction_details.get("installment_amount"),
        "total_paid_amount": transaction_details.get("total_paid_amount"),
        "payment_method_id": data.get("payment_method_id") or payment_method.get("id"),
        "payment_type_id": data.get("payment_type_id") or payment_method.get("type"),
        "issuer_id": payment_method.get("issuer_id") or data.get("issuer_id"),
        "card_first_six_digits": card.get("first_six_digits"),
        "card_last_four_digits": card.get("last_four_digits"),
        "cardholder_name": (card.get("cardholder") or {}).get("name") if isinstance(card.get("cardholder"), dict) else None,
        "payer_name": payer_name,
        "payer_id": payer.get("id"),
        "external_reference": data.get("external_reference"),
        "item_titles": item_titles,
    }
