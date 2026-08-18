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
import v058

APP_VERSION = "0.5.9"
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

app = v058.app

# Replace only the reconciliation feed. The rest of V0.5.8 remains intact.
app.router.routes = [
    route for route in app.router.routes
    if getattr(route, "path", None) != "/device/mercadopago/reconciliation"
]

_GENERIC_NAMES = {
    "",
    "var",
    "varios",
    "mercado pago",
    "movimiento con tarjeta",
    "producto",
    "pago",
}


def _clean_display(value: str | None) -> str:
    text = str(value or "").strip()
    prefixes = (
        "Producto de ",
        "Compra en ",
        "Pago a ",
        "Transferencia a ",
    )
    for prefix in prefixes:
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
            break
    return " ".join(text.split())


def _name_from_user(data: dict[str, Any] | None) -> str | None:
    if not isinstance(data, dict):
        return None
    first = str(data.get("first_name") or data.get("name") or "").strip()
    last = str(data.get("last_name") or data.get("surname") or "").strip()
    full = " ".join(x for x in (first, last) if x).strip()
    if full:
        return full
    nickname = str(data.get("nickname") or "").strip()
    return nickname or None


async def _user_name(user_id: Any) -> str | None:
    try:
        clean = str(int(user_id))
    except Exception:
        return None
    try:
        r = await base._mp_get(f"https://api.mercadolibre.com/users/{clean}")
        if r.status_code >= 400:
            return None
        data = r.json() if r.content else {}
        return _name_from_user(data if isinstance(data, dict) else None)
    except Exception:
        return None


async def _counterparty_for_payment(source_id: str, account_id: int | None) -> dict[str, Any]:
    try:
        r = await base._mp_get(f"https://api.mercadopago.com/v1/payments/{source_id}")
        if r.status_code >= 400:
            return {}
        data = r.json() if r.content else {}
        if not isinstance(data, dict):
            return {}
    except Exception:
        return {}

    payer = data.get("payer") if isinstance(data.get("payer"), dict) else {}
    collector = data.get("collector") if isinstance(data.get("collector"), dict) else {}
    payer_id = payer.get("id")
    collector_id = data.get("collector_id") or collector.get("id")

    payer_name = _name_from_user(payer)
    collector_name = _name_from_user(collector)

    counterparty_id = None
    counterparty_name = None

    if account_id is not None:
        if str(payer_id or "") == str(account_id) and str(collector_id or "") != str(account_id):
            counterparty_id = collector_id
            counterparty_name = collector_name
        elif str(collector_id or "") == str(account_id) and str(payer_id or "") != str(account_id):
            counterparty_id = payer_id
            counterparty_name = payer_name

    if counterparty_name is None and counterparty_id is not None:
        counterparty_name = await _user_name(counterparty_id)

    description = _clean_display(data.get("description"))
    item_titles: list[str] = []
    additional_info = data.get("additional_info") if isinstance(data.get("additional_info"), dict) else {}
    raw_items = additional_info.get("items")
    if isinstance(raw_items, list):
        for item in raw_items[:10]:
            if isinstance(item, dict):
                title = _clean_display(item.get("title") or item.get("description"))
                if title:
                    item_titles.append(title)

    candidates = [description, item_titles[0] if item_titles else "", counterparty_name or ""]
    display_name = ""
    for candidate in candidates:
        clean = _clean_display(candidate)
        if clean and clean.lower() not in _GENERIC_NAMES:
            display_name = clean
            break

    return {
        "display_name": display_name or (counterparty_name or description or "Movimiento con tarjeta"),
        "counterparty_name": counterparty_name,
        "counterparty_id": counterparty_id,
        "payer_id": payer_id,
        "collector_id": collector_id,
    }


@app.get("/device/mercadopago/reconciliation")
async def reconciliation_feed_v059(
    days: int = 30,
    device=Depends(main._device_auth),
):
    payload = await v058.reconciliation_feed_v058(days=days, device=device)
    if not isinstance(payload, dict) or not payload.get("ready"):
        if isinstance(payload, dict):
            payload["backend_version"] = APP_VERSION
        return payload

    card_activity = payload.get("card_activity")
    if not isinstance(card_activity, list) or not card_activity:
        payload["backend_version"] = APP_VERSION
        return payload

    account_id = await base._account_id()
    source_ids = [
        str(item.get("source_id") or "").strip()
        for item in card_activity
        if isinstance(item, dict) and str(item.get("source_id") or "").strip().isdigit()
    ]
    source_ids = list(dict.fromkeys(source_ids))[:50]

    semaphore = asyncio.Semaphore(6)

    async def one(source_id: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            return source_id, await _counterparty_for_payment(source_id, account_id)

    resolved = dict(await asyncio.gather(*(one(source_id) for source_id in source_ids)))

    for item in card_activity:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id") or "").strip()
        extra = resolved.get(source_id) or {}
        item.update(extra)
        current = _clean_display(item.get("description"))
        display = _clean_display(extra.get("display_name"))
        if display and (not current or current.lower() in _GENERIC_NAMES):
            item["description"] = display
        item["display_name"] = display or current or "Movimiento con tarjeta"

    payload["backend_version"] = APP_VERSION
    payload.setdefault("policy", {})["counterparty_name_best_effort"] = True
    payload["policy"]["pdf_description_can_override_generic_mercado_pago_name"] = True
    return payload
