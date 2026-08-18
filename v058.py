from __future__ import annotations

import asyncio
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

APP_VERSION = "0.5.8"
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

app = v057.app

# Replace only the reconciliation feed. Existing pairing, device auth, payment-detail
# and batch enrichment routes remain untouched.
app.router.routes = [
    route for route in app.router.routes
    if getattr(route, "path", None) != "/device/mercadopago/reconciliation"
]


def _source_id_from_external(value: str | None) -> str:
    text = str(value or "")
    for part in reversed(text.split(":")):
        if part.isdigit():
            return part
    return ""


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


async def _payment_details_map(source_ids: list[str], device: Any) -> dict[str, dict[str, Any]]:
    clean = list(dict.fromkeys(x for x in source_ids if x and x.isdigit()))[:50]
    semaphore = asyncio.Semaphore(6)

    async def one(source_id: str) -> tuple[str, dict[str, Any] | None]:
        async with semaphore:
            try:
                detail = await v056.payment_detail(source_id=source_id, device=device)
                return source_id, detail if isinstance(detail, dict) else None
            except Exception:
                return source_id, None

    pairs = await asyncio.gather(*(one(source_id) for source_id in clean))
    return {source_id: detail for source_id, detail in pairs if detail}


def _card_kind(operation_type: str | None) -> str:
    op = str(operation_type or "").lower()
    if op == "regular_payment":
        return "CARD_PURCHASE"
    if op == "money_transfer":
        return "CARD_FUNDED_TRANSFER"
    if op in {"recurring_payment", "subscription_payment"}:
        return "CARD_RECURRING_PAYMENT"
    return "CARD_ACTIVITY_REVIEW"


def _card_activity_item(
    payment: dict[str, Any],
    detail: dict[str, Any] | None,
    account_group: dict[str, Any] | None,
) -> dict[str, Any]:
    source_id = str(payment.get("payment_id") or "").strip()
    d = detail or {}

    transaction_amount = _safe_float(d.get("transaction_amount"))
    if transaction_amount is None:
        transaction_amount = _safe_float(payment.get("amount")) or 0.0

    total_paid_amount = _safe_float(d.get("total_paid_amount"))
    installment_amount = _safe_float(d.get("installment_amount"))
    installments = int(d.get("installments") or payment.get("installments") or 0)

    # total_paid_amount is the best candidate for what reached the card statement
    # when Mercado Pago returns it. We expose every amount separately as well so
    # Android can reconcile against the PDF instead of guessing.
    statement_candidate = total_paid_amount if total_paid_amount and total_paid_amount > 0 else transaction_amount

    operation_type = d.get("operation_type") or payment.get("operation_type")
    description = str(d.get("description") or payment.get("description") or "Movimiento con tarjeta").strip()

    group_net = _safe_float(account_group.get("net_amount")) if account_group else None
    group_gross = _safe_float(account_group.get("gross_amount")) if account_group else None
    group_fee = _safe_float(account_group.get("fee_amount")) if account_group else None

    return {
        "external_id": f"mp:card:{source_id}",
        "source_id": source_id,
        "date": d.get("date_approved") or d.get("date_created") or payment.get("date"),
        "description": description or "Movimiento con tarjeta",
        "status": d.get("status") or payment.get("status"),
        "operation_type": operation_type,
        "card_kind": _card_kind(operation_type),
        "currency": d.get("currency") or payment.get("currency") or "ARS",
        "transaction_amount": round(transaction_amount, 2),
        "total_paid_amount": round(total_paid_amount, 2) if total_paid_amount is not None else None,
        "statement_amount_candidate": round(statement_candidate, 2),
        "installments": installments,
        "installment_amount": round(installment_amount, 2) if installment_amount is not None else None,
        "payment_method_id": d.get("payment_method_id") or payment.get("payment_method_id"),
        "payment_type_id": d.get("payment_type_id") or payment.get("payment_type_id"),
        "issuer_id": d.get("issuer_id"),
        "card_first_six_digits": d.get("card_first_six_digits"),
        "card_last_four_digits": d.get("card_last_four_digits"),
        "cardholder_name": d.get("cardholder_name"),
        "payer_name": d.get("payer_name"),
        "item_titles": d.get("item_titles") or [],
        "account_money_overlap": account_group is not None,
        "account_money_net_amount": round(group_net, 2) if group_net is not None else None,
        "account_money_gross_amount": round(group_gross, 2) if group_gross is not None else None,
        "account_money_fee_amount": round(group_fee, 2) if group_fee is not None else None,
        "automatic_card_detection": True,
        "automatic_finance_posting": False,
        "requires_pdf_reconciliation": True,
    }


@app.get("/device/mercadopago/reconciliation")
async def reconciliation_feed_v058(
    days: int = 30,
    device=Depends(main._device_auth),
):
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
            "card_activity": [],
        }

    # Payment Search is authoritative for discovering the funding instrument.
    # Account Money remains the accounting source for balance-funded movements.
    payments = await v054._all_payment_candidates(days=safe_days, max_rows=500)
    credit_payments = [
        p for p in payments
        if str(p.get("payment_type_id") or "").lower() == "credit_card"
        and str(p.get("status") or "").lower() == "approved"
    ]
    credit_source_ids = {
        str(p.get("payment_id"))
        for p in credit_payments
        if p.get("payment_id") is not None
    }

    groups_by_source = {
        str(group.get("source_id")): group
        for group in groups
        if group.get("source_id") is not None
    }

    detail_map = await _payment_details_map(sorted(credit_source_ids), device)
    card_activity = [
        _card_activity_item(
            payment=p,
            detail=detail_map.get(str(p.get("payment_id"))),
            account_group=groups_by_source.get(str(p.get("payment_id"))),
        )
        for p in credit_payments
    ]
    card_activity.sort(key=lambda x: (x.get("date") or "", x.get("source_id") or ""), reverse=True)

    expenses: list[dict[str, Any]] = []
    incomes: list[dict[str, Any]] = []
    for group in groups:
        payment = payment_by_id.get(str(group.get("source_id")))

        # A credit-card-funded transaction is a card commitment, not a cash/saldo
        # expense. It belongs in card_activity and must not be posted twice.
        if str(group.get("source_id")) not in credit_source_ids and payment:
            expense = v055._safe_expense_item(group, payment)
            if expense:
                expenses.append(expense)

        income = v056._income_candidate_item(group, payment)
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
        "pair_suggestions": v056._exact_amount_pair_suggestions(expenses, incomes),
        "card_activity": card_activity,
        "card_activity_summary": {
            "count": len(card_activity),
            "regular_purchases": sum(1 for x in card_activity if x.get("card_kind") == "CARD_PURCHASE"),
            "card_funded_transfers": sum(1 for x in card_activity if x.get("card_kind") == "CARD_FUNDED_TRANSFER"),
            "other_review": sum(1 for x in card_activity if x.get("card_kind") == "CARD_ACTIVITY_REVIEW"),
        },
        "policy": {
            "account_money_role": "balance_funded_movements_and_reconciliation",
            "payment_search_role": "funding_instrument_discovery_and_metadata",
            "credit_card_activity_is_not_cash_expense": True,
            "credit_card_activity_is_card_commitment": True,
            "credit_card_money_transfer_is_visible_for_review": True,
            "card_statement_amount_prefers_total_paid_amount_when_available": True,
            "card_pdf_is_final_reconciliation_source": True,
            "automatic_finance_posting": False,
            "reserves_are_internal_transfers": True,
            "third_party_contributions_must_be_confirmed": True,
            "merchant_session_grouping_is_local_android_rule": "same merchant + consecutive gap <= 10 minutes; amount may differ",
        },
    }
