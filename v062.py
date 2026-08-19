from __future__ import annotations

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
import v061

APP_VERSION = "0.6.2"
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
v061.APP_VERSION = APP_VERSION

app = v061.app

# Replace only reconciliation. All auth/pairing/payment-detail routes remain inherited.
app.router.routes = [
    route for route in app.router.routes
    if getattr(route, "path", None) != "/device/mercadopago/reconciliation"
]


def _source_id(item: dict) -> str:
    return str(item.get("source_id") or "").strip()


@app.get("/device/mercadopago/reconciliation")
async def reconciliation_feed_v062(
    days: int = 30,
    device=Depends(main._device_auth),
):
    payload = await v061.reconciliation_feed_v061(days=days, device=device)
    if not isinstance(payload, dict):
        return payload

    payload["backend_version"] = APP_VERSION
    if not payload.get("ready"):
        return payload

    # CARD HAS PRECEDENCE OVER GENERIC TRANSFER.
    # Mercado Pago can label a credit-card purchase as operation_type=money_transfer.
    # Such an operation is already present in card_activity and must NOT appear again
    # in the outbound-transfer review queue.
    card_activity = payload.get("card_activity") or []
    card_source_ids = {
        _source_id(item)
        for item in card_activity
        if isinstance(item, dict) and _source_id(item)
    }

    outbound = payload.get("outbound_transfers") or []
    filtered = [
        item for item in outbound
        if isinstance(item, dict) and _source_id(item) not in card_source_ids
    ]

    removed = len(outbound) - len(filtered)
    payload["outbound_transfers"] = filtered
    payload["outbound_transfer_summary"] = {
        "count": len(filtered),
        "total": round(sum(float(item.get("amount") or 0.0) for item in filtered), 2),
    }
    payload.setdefault("policy", {})["credit_card_activity_precedes_transfer_classification"] = True
    payload["policy"]["card_activity_removed_from_transfer_queue"] = removed
    payload["policy"]["same_source_id_never_asks_card_and_transfer"] = True
    return payload
