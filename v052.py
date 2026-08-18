from typing import Any

from fastapi import Depends

import main
import main_v050 as base
import v051

APP_VERSION = "0.5.2"
base.APP_VERSION = APP_VERSION
base.app.version = APP_VERSION
main.APP_VERSION = APP_VERSION
main.app.version = APP_VERSION
v051.main.APP_VERSION = APP_VERSION

app = v051.app
SEARCH_URL = f"{v051.REPORT_URL}/search"

_REPLACED_PATHS = {
    "/mercadopago/account-money/bootstrap-status",
    "/admin/account-money/bootstrap-analysis",
}
app.router.routes = [
    route for route in app.router.routes
    if getattr(route, "path", None) not in _REPLACED_PATHS
]


def _results(data: dict[str, Any]) -> list[dict[str, Any]]:
    value = data.get("results")
    return value if isinstance(value, list) else []


async def _resolve_report_file(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("file_name"):
        return state

    task_status = str(state.get("status") or "").lower()
    if task_status not in {"available", "processed"}:
        return state

    params: dict[str, Any] = {
        "created_from": "manual",
        "format": state.get("format") or "CSV",
        "limit": 30,
    }
    if state.get("report_id"):
        params["id"] = state["report_id"]
    else:
        if state.get("begin_date"):
            params["begin_date"] = state["begin_date"]
        if state.get("end_date"):
            params["end_date"] = state["end_date"]

    response = await main._mp_get(SEARCH_URL, params=params)
    merged = {**state, "report_search_http_status": response.status_code}
    if response.status_code != 200:
        await v051._save_bootstrap_state(merged)
        return merged

    data = main._safe_response_json(response)
    rows = _results(data)
    match = rows[0] if rows else None
    if match:
        merged.update({
            "report_id": match.get("id") or merged.get("report_id"),
            "file_name": match.get("file_name") or merged.get("file_name"),
            "report_status": match.get("status"),
            "report_date_created": match.get("date_created"),
            "report_download_date": match.get("download_date"),
            "report_search_resolved": bool(match.get("file_name")),
        })

    await v051._save_bootstrap_state(merged)
    return merged


async def _refresh_and_resolve() -> dict[str, Any] | None:
    state = await v051._load_bootstrap_state()
    if not state:
        return None
    state = await v051._task_status(state)
    return await _resolve_report_file(state)


def _ready(state: dict[str, Any]) -> bool:
    if not state.get("file_name"):
        return False
    task_status = str(state.get("status") or "").lower()
    report_status = str(state.get("report_status") or "").lower()
    return task_status in {"available", "processed"} or report_status == "processed"


@app.get("/mercadopago/account-money/bootstrap-status")
async def bootstrap_status_v052():
    state = await _refresh_and_resolve()
    if not state:
        return {
            "backend_version": APP_VERSION,
            "created": False,
            "status": "not_started",
            "ready": False,
        }

    return {
        "backend_version": APP_VERSION,
        "created": bool(state.get("task_id")),
        "status": state.get("status"),
        "report_status": state.get("report_status"),
        "ready": _ready(state),
        "http_status": state.get("http_status"),
        "task_check_http_status": state.get("task_check_http_status", 200),
        "report_search_http_status": state.get("report_search_http_status"),
        "begin_date": state.get("begin_date"),
        "end_date": state.get("end_date"),
        "created_from": state.get("created_from"),
        "format": state.get("format"),
        "file_ready": bool(state.get("file_name")),
        "detail": state.get("detail"),
    }


@app.get("/admin/account-money/bootstrap-analysis", dependencies=[Depends(main._apk_auth)])
async def bootstrap_analysis_v052():
    state = await _refresh_and_resolve()
    if not state:
        return {
            "backend_version": APP_VERSION,
            "ready": False,
            "status": "not_started",
        }

    if not _ready(state):
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
    summary = v051._ledger_summary(rows)
    payment_ids = await v051._payment_search_ids(days=30)
    report_ids: set[str] = summary.pop("_source_ids")

    overlap = report_ids & payment_ids
    comparison = {
        "account_money_source_ids": len(report_ids),
        "payment_search_ids": len(payment_ids),
        "overlap_ids": len(overlap),
        "account_money_only_ids": len(report_ids - payment_ids),
        "payment_search_only_ids": len(payment_ids - report_ids),
        "coverage_of_payment_search_pct": (
            round(len(overlap) * 100.0 / len(payment_ids), 2)
            if payment_ids else None
        ),
    }

    return {
        "backend_version": APP_VERSION,
        "ready": True,
        "source": "ACCOUNT_MONEY_REPORT",
        "task_status": state.get("status"),
        "report_status": state.get("report_status"),
        "period": {
            "begin_date": state.get("begin_date"),
            "end_date": state.get("end_date"),
        },
        "ledger": summary,
        "comparison_with_payment_search": comparison,
        "import_policy": {
            "direction_source": "sign(SETTLEMENT_NET_AMOUNT)",
            "positive": "INCOME",
            "negative": "EXPENSE",
            "zero": "NEUTRAL_REVIEW",
            "dedupe_primary": "SOURCE_ID + TRANSACTION_TYPE + TRANSACTION_DATE",
            "auto_import_enabled": False,
        },
    }


# Compatibility shim: Render services that still have `uvicorn v052:app`
# configured in the dashboard will transparently load V0.5.3 after this
# module is fully initialized. If v053 is already importing v052, avoid a
# circular re-import.
import sys
if "v053" not in sys.modules:
    import v053 as _v053  # noqa: F401,E402
