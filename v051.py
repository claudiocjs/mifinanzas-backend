import csv
import io
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import Depends, HTTPException

import main


# V0.5.1 is a thin orchestration layer over the validated V0.5 backend.
main.APP_VERSION = "0.5.1"
main.app.version = "0.5.1"
app = main.app

BOOTSTRAP_STATE_KEY = "account_money_bootstrap_30d_v1"
REPORT_URL = "https://api.mercadopago.com/v1/account/settlement_report"
TASK_URL = "https://api.mercadopago.com/v1/account/settlement_report/task/{task_id}"


async def _save_bootstrap_state(payload: dict[str, Any]) -> None:
    if main.SessionLocal is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL no configurada")

    encrypted = main._fernet().encrypt(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("utf-8")

    async with main.SessionLocal() as session:
        row = await session.get(main.SecretState, BOOTSTRAP_STATE_KEY)
        now = int(time.time())
        if row:
            row.encrypted_value = encrypted
            row.updated_at_epoch = now
        else:
            session.add(
                main.SecretState(
                    key=BOOTSTRAP_STATE_KEY,
                    encrypted_value=encrypted,
                    updated_at_epoch=now,
                )
            )
        await session.commit()


async def _load_bootstrap_state() -> Optional[dict[str, Any]]:
    if main.SessionLocal is None:
        return None

    async with main.SessionLocal() as session:
        row = await session.get(main.SecretState, BOOTSTRAP_STATE_KEY)
        if not row:
            return None

        raw = main._fernet().decrypt(row.encrypted_value.encode("utf-8"))
        return json.loads(raw.decode("utf-8"))


async def _delete_bootstrap_state() -> None:
    if main.SessionLocal is None:
        return

    async with main.SessionLocal() as session:
        row = await session.get(main.SecretState, BOOTSTRAP_STATE_KEY)
        if row:
            await session.delete(row)
            await session.commit()


async def _mp_post_json(url: str, body: dict[str, Any]) -> httpx.Response:
    access = await main._current_access_token()
    headers = {
        "Authorization": f"Bearer {access}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(url, json=body, headers=headers)

    if response.status_code == 401:
        access = await main._current_access_token(force_refresh=True)
        headers["Authorization"] = f"Bearer {access}"
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(url, json=body, headers=headers)

    return response


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _create_30d_report(force: bool = False) -> dict[str, Any]:
    previous = await _load_bootstrap_state()
    if previous and not force:
        return previous

    config = await main._mp_get(main.MP_ACCOUNT_MONEY_CONFIG_URL)
    if config.status_code != 200:
        state = {
            "status": "config_not_ready",
            "http_status": config.status_code,
            "created_at_epoch": int(time.time()),
        }
        await _save_bootstrap_state(state)
        return state

    config_data = main._safe_response_json(config)
    if bool(config_data.get("scheduled", False)):
        state = {
            "status": "blocked_scheduled_true",
            "http_status": 409,
            "created_at_epoch": int(time.time()),
        }
        await _save_bootstrap_state(state)
        return state

    end = datetime.now(timezone.utc)
    begin = end - timedelta(days=30)
    body = {
        "begin_date": _iso_z(begin),
        "end_date": _iso_z(end),
    }

    response = await _mp_post_json(REPORT_URL, body)
    try:
        data = response.json()
    except Exception:
        data = {}

    if response.status_code not in (200, 201, 202):
        state = {
            "status": "create_failed",
            "http_status": response.status_code,
            "begin_date": body["begin_date"],
            "end_date": body["end_date"],
            "detail": data.get("message") or data.get("error") or data.get("cause"),
            "created_at_epoch": int(time.time()),
        }
        await _save_bootstrap_state(state)
        return state

    task_id = data.get("id")
    state = {
        "status": data.get("status") or "pending",
        "http_status": response.status_code,
        "task_id": task_id,
        "report_id": data.get("report_id"),
        "begin_date": data.get("begin_date") or body["begin_date"],
        "end_date": data.get("end_date") or body["end_date"],
        "created_from": data.get("created_from") or "manual",
        "format": data.get("format") or "CSV",
        "created_at_epoch": int(time.time()),
    }
    await _save_bootstrap_state(state)
    return state


async def _task_status(state: dict[str, Any]) -> dict[str, Any]:
    task_id = state.get("task_id")
    if not task_id:
        return state

    response = await main._mp_get(TASK_URL.format(task_id=task_id))
    if response.status_code != 200:
        return {
            **state,
            "task_check_http_status": response.status_code,
        }

    data = main._safe_response_json(response)
    merged = {
        **state,
        "status": data.get("status") or state.get("status"),
        "report_id": data.get("report_id") or state.get("report_id"),
        "file_name": data.get("file_name") or state.get("file_name"),
        "format": data.get("format") or state.get("format"),
        "last_checked_epoch": int(time.time()),
    }
    await _save_bootstrap_state(merged)
    return merged


def _parse_number(raw: Any) -> float:
    if raw is None:
        return 0.0
    text = str(raw).strip().replace(" ", "")
    if not text:
        return 0.0
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    elif "," in text and "." in text and text.rfind(",") > text.rfind("."):
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except Exception:
        return 0.0


def _decode_report(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _parse_report_csv(content: bytes) -> list[dict[str, str]]:
    text = _decode_report(content)
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    rows = []
    for row in reader:
        if not row:
            continue
        clean = {
            str(k or "").strip(): str(v or "").strip()
            for k, v in row.items()
            if k is not None
        }
        if not any(clean.get(k) for k in ("SOURCE_ID", "TRANSACTION_TYPE", "TRANSACTION_DATE")):
            continue
        rows.append(clean)
    return rows


async def _download_report(file_name: str) -> bytes:
    response = await main._mp_get(f"{REPORT_URL}/{file_name}")
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo descargar Account Money Report (HTTP {response.status_code})",
        )
    return response.content


async def _payment_search_ids(days: int = 30, max_rows: int = 500) -> set[str]:
    ids: set[str] = set()
    offset = 0
    while offset < max_rows:
        paging, items = await main._payment_candidates(
            days=days,
            limit=min(50, max_rows - offset),
            offset=offset,
        )
        for item in items:
            payment_id = item.get("payment_id")
            if payment_id is not None:
                ids.add(str(payment_id))
        total = int((paging or {}).get("total") or len(items))
        offset += len(items)
        if not items or offset >= total:
            break
    return ids


def _ledger_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    by_type: dict[str, dict[str, float | int]] = {}
    positive = 0.0
    negative = 0.0
    neutral = 0
    net_sum = 0.0
    source_ids: set[str] = set()
    samples = []

    for row in rows:
        tx_type = row.get("TRANSACTION_TYPE") or "UNKNOWN"
        net = _parse_number(row.get("SETTLEMENT_NET_AMOUNT"))
        gross = _parse_number(row.get("TRANSACTION_AMOUNT"))
        source_id = row.get("SOURCE_ID")
        if source_id:
            source_ids.add(str(source_id))

        bucket = by_type.setdefault(tx_type, {"count": 0, "net_sum": 0.0, "gross_sum": 0.0})
        bucket["count"] += 1
        bucket["net_sum"] = round(float(bucket["net_sum"]) + net, 2)
        bucket["gross_sum"] = round(float(bucket["gross_sum"]) + gross, 2)

        net_sum += net
        if net > 0:
            positive += net
            direction = "INCOME"
        elif net < 0:
            negative += abs(net)
            direction = "EXPENSE"
        else:
            neutral += 1
            direction = "NEUTRAL"

        if len(samples) < 20:
            samples.append({
                "date": row.get("TRANSACTION_DATE") or row.get("SETTLEMENT_DATE"),
                "transaction_type": tx_type,
                "direction": direction,
                "description": row.get("DESCRIPTION"),
                "gross_amount": gross,
                "net_amount": net,
                "currency": row.get("SETTLEMENT_CURRENCY") or row.get("TRANSACTION_CURRENCY"),
                "payment_method": row.get("PAYMENT_METHOD"),
                "payment_method_type": row.get("PAYMENT_METHOD_TYPE"),
            })

    return {
        "rows": len(rows),
        "income_total": round(positive, 2),
        "expense_total": round(negative, 2),
        "net_total": round(net_sum, 2),
        "neutral_rows": neutral,
        "by_transaction_type": by_type,
        "source_id_count": len(source_ids),
        "samples": samples,
        "_source_ids": source_ids,
    }


@app.on_event("startup")
async def v051_bootstrap_account_money_report():
    try:
        await _create_30d_report(force=False)
    except Exception as exc:
        state = {
            "status": "startup_exception",
            "detail": str(exc)[:300],
            "created_at_epoch": int(time.time()),
        }
        try:
            await _save_bootstrap_state(state)
        except Exception:
            pass


@app.get("/mercadopago/account-money/bootstrap-status")
async def bootstrap_status():
    state = await _load_bootstrap_state()
    if not state:
        return {
            "backend_version": main.APP_VERSION,
            "created": False,
            "status": "not_started",
            "ready": False,
        }
    state = await _task_status(state)
    return {
        "backend_version": main.APP_VERSION,
        "created": bool(state.get("task_id")),
        "status": state.get("status"),
        "ready": state.get("status") == "processed" and bool(state.get("file_name")),
        "http_status": state.get("http_status"),
        "begin_date": state.get("begin_date"),
        "end_date": state.get("end_date"),
        "created_from": state.get("created_from"),
        "format": state.get("format"),
        "file_ready": bool(state.get("file_name")),
        "detail": state.get("detail"),
    }


@app.get("/admin/account-money/bootstrap-analysis", dependencies=[Depends(main._apk_auth)])
async def bootstrap_analysis():
    state = await _load_bootstrap_state()
    if not state:
        return {
            "backend_version": main.APP_VERSION,
            "ready": False,
            "status": "not_started",
        }
    state = await _task_status(state)
    if state.get("status") != "processed" or not state.get("file_name"):
        return {
            "backend_version": main.APP_VERSION,
            "ready": False,
            "status": state.get("status"),
            "begin_date": state.get("begin_date"),
            "end_date": state.get("end_date"),
        }

    content = await _download_report(state["file_name"])
    rows = _parse_report_csv(content)
    summary = _ledger_summary(rows)
    payment_ids = await _payment_search_ids(days=30)
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
        "backend_version": main.APP_VERSION,
        "ready": True,
        "source": "ACCOUNT_MONEY_REPORT",
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


@app.post("/admin/account-money/bootstrap-retry", dependencies=[Depends(main._apk_auth)])
async def bootstrap_retry():
    await _delete_bootstrap_state()
    state = await _create_30d_report(force=True)
    return {
        "backend_version": main.APP_VERSION,
        "status": state.get("status"),
        "created": bool(state.get("task_id")),
        "begin_date": state.get("begin_date"),
        "end_date": state.get("end_date"),
    }
