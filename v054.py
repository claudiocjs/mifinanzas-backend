from collections import Counter
from typing import Any

from fastapi import Depends

import main
import main_v050 as base
import v051
import v052
import v053

APP_VERSION = "0.5.4"
base.APP_VERSION = APP_VERSION
base.app.version = APP_VERSION
main.APP_VERSION = APP_VERSION
main.app.version = APP_VERSION
v051.main.APP_VERSION = APP_VERSION
v052.APP_VERSION = APP_VERSION
v053.APP_VERSION = APP_VERSION

app = v053.app

_REPLACED_PATHS = {
    "/admin/account-money/bootstrap-analysis",
}
app.router.routes = [
    route for route in app.router.routes
    if getattr(route, "path", None) not in _REPLACED_PATHS
]


async def _all_payment_candidates(days: int = 30, max_rows: int = 500) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while offset < max_rows:
        paging, batch = await main._payment_candidates(
            days=days,
            limit=min(50, max_rows - offset),
            offset=offset,
        )
        items.extend(batch)
        total = int((paging or {}).get("total") or len(items))
        offset += len(batch)
        if not batch or offset >= total:
            break
    return items


def _full_normalized_groups(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}

    for idx, row in enumerate(rows):
        source_id = str(row.get("SOURCE_ID") or "").strip()
        tx_type = str(row.get("TRANSACTION_TYPE") or "UNKNOWN").strip().upper()
        if not source_id:
            source_id = f"__NO_SOURCE__:{row.get('TRANSACTION_DATE') or ''}:{idx}"

        key = (source_id, tx_type)
        g = groups.setdefault(key, {
            "source_id": source_id,
            "transaction_type": tx_type,
            "raw_row_count": 0,
            "net_amount": 0.0,
            "gross_amount": 0.0,
            "fee_amount": 0.0,
            "first_date": None,
            "last_date": None,
            "payment_method_types": set(),
            "payment_methods": set(),
        })

        g["raw_row_count"] += 1
        g["net_amount"] += v051._parse_number(row.get("SETTLEMENT_NET_AMOUNT"))
        g["gross_amount"] += v051._parse_number(row.get("TRANSACTION_AMOUNT"))
        g["fee_amount"] += v051._parse_number(row.get("FEE_AMOUNT"))

        date = row.get("TRANSACTION_DATE") or row.get("SETTLEMENT_DATE")
        if date:
            g["first_date"] = date if not g["first_date"] or date < g["first_date"] else g["first_date"]
            g["last_date"] = date if not g["last_date"] or date > g["last_date"] else g["last_date"]

        if row.get("PAYMENT_METHOD_TYPE"):
            g["payment_method_types"].add(str(row["PAYMENT_METHOD_TYPE"]))
        if row.get("PAYMENT_METHOD"):
            g["payment_methods"].add(str(row["PAYMENT_METHOD"]))

    result: list[dict[str, Any]] = []
    for g in groups.values():
        pmt_types = sorted(g.pop("payment_method_types"))
        pmt_methods = sorted(g.pop("payment_methods"))
        primary_pmt_type = pmt_types[0] if len(pmt_types) == 1 else ""

        g["net_amount"] = round(g["net_amount"], 2)
        g["gross_amount"] = round(g["gross_amount"], 2)
        g["fee_amount"] = round(g["fee_amount"], 2)
        g["payment_method_types"] = pmt_types
        g["payment_methods"] = pmt_methods
        g["semantic_kind"] = v053._semantic_kind(
            g["transaction_type"],
            primary_pmt_type,
            g["net_amount"],
        )
        result.append(g)

    result.sort(key=lambda x: (x.get("last_date") or "", x["source_id"]), reverse=True)
    return result


def _ledger_class(group: dict[str, Any], payment: dict[str, Any] | None) -> str:
    tx_type = str(group.get("transaction_type") or "").upper()
    net = float(group.get("net_amount") or 0.0)

    if tx_type in {"PAYOUT", "PAYOUTS", "WITHDRAWAL"}:
        return "TRANSFER_OUT"
    if tx_type == "WITHDRAWAL_CANCEL":
        return "TRANSFER_IN"
    if tx_type in {"REFUND", "CASHBACK"}:
        return "REFUND"
    if tx_type in {"CHARGEBACK", "DISPUTE"}:
        return "ADJUSTMENT_REVIEW"

    if payment:
        op = str(payment.get("operation_type") or "").lower()
        if op == "regular_payment":
            return "PURCHASE_CANDIDATE" if net < 0 else "PAYMENT_IN_CANDIDATE"
        if op in {"money_transfer", "account_fund", "partition_transfer"}:
            return "TRANSFER_OR_BALANCE_MOVE"
        return "PAYMENT_ENRICHED_REVIEW"

    pmt_types = set(group.get("payment_method_types") or [])
    if "bank_transfer" in pmt_types:
        return "BANK_TRANSFER_CANDIDATE"
    return "UNMATCHED_BALANCE_MOVE"


@app.get(
    "/admin/account-money/bootstrap-analysis",
    dependencies=[Depends(main._apk_auth)],
)
async def bootstrap_analysis_v054():
    state = await v052._refresh_and_resolve()
    if not state:
        return {
            "backend_version": APP_VERSION,
            "ready": False,
            "status": "not_started",
        }

    if not v052._ready(state):
        return {
            "backend_version": APP_VERSION,
            "ready": False,
            "status": state.get("status"),
            "report_status": state.get("report_status"),
            "file_ready": bool(state.get("file_name")),
            "begin_date": state.get("begin_date"),
            "end_date": state.get("end_date"),
        }

    content = await v051._download_report(state["file_name"])
    rows = v051._parse_report_csv(content)
    groups = _full_normalized_groups(rows)

    payments = await _all_payment_candidates(days=30)
    payment_by_id = {
        str(item.get("payment_id")): item
        for item in payments
        if item.get("payment_id") is not None
    }

    class_counts: Counter[str] = Counter()
    operation_type_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    direction_agreement = Counter()
    matched = 0
    enriched_samples: list[dict[str, Any]] = []

    for group in groups:
        payment = payment_by_id.get(str(group["source_id"]))
        ledger_class = _ledger_class(group, payment)
        class_counts[ledger_class] += 1

        account_direction = (
            "INCOME" if group["net_amount"] > 0
            else "EXPENSE" if group["net_amount"] < 0
            else "NEUTRAL"
        )

        item = {
            **group,
            "balance_direction": account_direction,
            "ledger_class": ledger_class,
            "payment_search_match": payment is not None,
            "description": None,
            "suggested_category": None,
            "operation_type": None,
            "payment_search_direction": None,
            "auto_import_safe": False,
        }

        if payment:
            matched += 1
            op = str(payment.get("operation_type") or "UNKNOWN")
            category = str(payment.get("suggested_category") or "Otros")
            operation_type_counts[op] += 1
            category_counts[category] += 1

            ps_direction = str(payment.get("direction") or "UNKNOWN")
            if ps_direction == "UNKNOWN":
                direction_agreement["payment_search_unknown"] += 1
            elif ps_direction == account_direction:
                direction_agreement["agree"] += 1
            else:
                direction_agreement["disagree"] += 1

            item.update({
                "description": payment.get("description"),
                "suggested_category": payment.get("suggested_category"),
                "operation_type": payment.get("operation_type"),
                "payment_search_direction": payment.get("direction"),
                "payment_status": payment.get("status"),
                "payment_method_id": payment.get("payment_method_id"),
                "payment_type_id": payment.get("payment_type_id"),
            })

        if len(enriched_samples) < 50:
            enriched_samples.append(item)

    return {
        "backend_version": APP_VERSION,
        "ready": True,
        "source": "ACCOUNT_MONEY_REPORT_PLUS_PAYMENT_SEARCH",
        "period": {
            "begin_date": state.get("begin_date"),
            "end_date": state.get("end_date"),
        },
        "canonical_ledger": {
            "raw_rows": len(rows),
            "canonical_groups": len(groups),
            "collapsed_rows": len(rows) - len(groups),
            "matched_with_payment_search": matched,
            "unmatched_with_payment_search": len(groups) - matched,
            "match_rate_pct": round(matched * 100.0 / len(groups), 2) if groups else None,
            "ledger_class_counts": dict(class_counts),
            "matched_operation_type_counts": dict(operation_type_counts),
            "matched_category_counts": dict(category_counts),
            "direction_crosscheck": dict(direction_agreement),
            "sample_groups": enriched_samples,
        },
        "policy": {
            "account_money_role": "balance_truth",
            "payment_search_role": "metadata_enrichment_only",
            "payouts_are_expenses": False,
            "grouping_key": "SOURCE_ID + TRANSACTION_TYPE",
            "direction_source": "sign(SETTLEMENT_NET_AMOUNT)",
            "payment_search_direction_is_authoritative": False,
            "auto_import_enabled": False,
            "next_gate": "validate enriched canonical ledger before Android V0.8 sync",
        },
    }
