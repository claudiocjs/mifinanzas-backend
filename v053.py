from collections import defaultdict
from typing import Any

from fastapi import Depends

import main
import main_v050 as base
import v051
import v052

APP_VERSION = "0.5.3"
base.APP_VERSION = APP_VERSION
base.app.version = APP_VERSION
main.APP_VERSION = APP_VERSION
main.app.version = APP_VERSION
v051.main.APP_VERSION = APP_VERSION
v052.APP_VERSION = APP_VERSION

app = v052.app

_REPLACED_PATHS = {
    "/admin/account-money/bootstrap-analysis",
}
app.router.routes = [
    route for route in app.router.routes
    if getattr(route, "path", None) not in _REPLACED_PATHS
]


def _semantic_kind(transaction_type: str, payment_method_type: str, net_amount: float) -> str:
    tx = (transaction_type or "").upper()
    pmt = (payment_method_type or "").lower()

    if tx in {"PAYOUT", "PAYOUTS", "WITHDRAWAL"}:
        return "TRANSFER_OUT"
    if tx == "WITHDRAWAL_CANCEL":
        return "TRANSFER_IN"
    if tx in {"REFUND", "CASHBACK"}:
        return "REFUND_IN" if net_amount >= 0 else "REFUND_OUT"
    if tx in {"CHARGEBACK", "DISPUTE"}:
        return "ADJUSTMENT"
    if tx == "SETTLEMENT":
        if pmt == "bank_transfer":
            return "TRANSFER_IN_CANDIDATE" if net_amount >= 0 else "TRANSFER_OUT_CANDIDATE"
        return "BALANCE_IN_CANDIDATE" if net_amount >= 0 else "BALANCE_OUT_CANDIDATE"
    return "REVIEW"


def _normalized_groups(rows: list[dict[str, str]]) -> dict[str, Any]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    raw_rows_without_source_id = 0

    for idx, row in enumerate(rows):
        source_id = str(row.get("SOURCE_ID") or "").strip()
        tx_type = str(row.get("TRANSACTION_TYPE") or "UNKNOWN").strip().upper()
        net = v051._parse_number(row.get("SETTLEMENT_NET_AMOUNT"))
        gross = v051._parse_number(row.get("TRANSACTION_AMOUNT"))
        fee = v051._parse_number(row.get("FEE_AMOUNT"))

        if not source_id:
            raw_rows_without_source_id += 1
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
        g["net_amount"] += net
        g["gross_amount"] += gross
        g["fee_amount"] += fee

        date = row.get("TRANSACTION_DATE") or row.get("SETTLEMENT_DATE")
        if date:
            g["first_date"] = date if not g["first_date"] or date < g["first_date"] else g["first_date"]
            g["last_date"] = date if not g["last_date"] or date > g["last_date"] else g["last_date"]

        if row.get("PAYMENT_METHOD_TYPE"):
            g["payment_method_types"].add(str(row["PAYMENT_METHOD_TYPE"]))
        if row.get("PAYMENT_METHOD"):
            g["payment_methods"].add(str(row["PAYMENT_METHOD"]))

    normalized = []
    semantic_counts: dict[str, int] = defaultdict(int)
    multirow_groups = 0
    conflicting_method_groups = 0

    for g in groups.values():
        pmt_types = sorted(g.pop("payment_method_types"))
        pmt_methods = sorted(g.pop("payment_methods"))
        if g["raw_row_count"] > 1:
            multirow_groups += 1
        if len(pmt_types) > 1:
            conflicting_method_groups += 1

        primary_pmt_type = pmt_types[0] if len(pmt_types) == 1 else ""
        semantic = _semantic_kind(
            g["transaction_type"],
            primary_pmt_type,
            g["net_amount"],
        )
        semantic_counts[semantic] += 1

        g["net_amount"] = round(g["net_amount"], 2)
        g["gross_amount"] = round(g["gross_amount"], 2)
        g["fee_amount"] = round(g["fee_amount"], 2)
        g["payment_method_types"] = pmt_types
        g["payment_methods"] = pmt_methods
        g["semantic_kind"] = semantic
        g["auto_import_safe"] = False
        normalized.append(g)

    normalized.sort(key=lambda x: (x.get("last_date") or "", x["source_id"]), reverse=True)

    return {
        "raw_rows": len(rows),
        "normalized_groups": len(normalized),
        "collapsed_rows": len(rows) - len(normalized),
        "multirow_groups": multirow_groups,
        "groups_with_multiple_payment_method_types": conflicting_method_groups,
        "raw_rows_without_source_id": raw_rows_without_source_id,
        "semantic_counts": dict(semantic_counts),
        "sample_groups": normalized[:30],
    }


@app.get(
    "/admin/account-money/bootstrap-analysis",
    dependencies=[Depends(main._apk_auth)],
)
async def bootstrap_analysis_v053():
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

    raw_summary = v051._ledger_summary(rows)
    raw_source_ids: set[str] = raw_summary.pop("_source_ids")
    normalization = _normalized_groups(rows)

    payment_ids = await v051._payment_search_ids(days=30)
    overlap = raw_source_ids & payment_ids

    return {
        "backend_version": APP_VERSION,
        "ready": True,
        "source": "ACCOUNT_MONEY_REPORT",
        "period": {
            "begin_date": state.get("begin_date"),
            "end_date": state.get("end_date"),
        },
        "raw_ledger": raw_summary,
        "normalization": normalization,
        "comparison_with_payment_search": {
            "account_money_source_ids": len(raw_source_ids),
            "payment_search_ids": len(payment_ids),
            "overlap_ids": len(overlap),
            "account_money_only_ids": len(raw_source_ids - payment_ids),
            "payment_search_only_ids": len(payment_ids - raw_source_ids),
            "coverage_of_payment_search_pct": (
                round(len(overlap) * 100.0 / len(payment_ids), 2)
                if payment_ids else None
            ),
        },
        "policy": {
            "account_money_role": "balance_truth",
            "payment_search_role": "metadata_enrichment",
            "payouts_are_expenses": False,
            "grouping_key": "SOURCE_ID + TRANSACTION_TYPE",
            "direction_uses_settlement_net_amount_sign": True,
            "auto_import_enabled": False,
            "reason": "diagnostic normalization stage before Android sync",
        },
    }
