from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Depends
from pydantic import BaseModel, Field

import main
import main_v050 as base
import v051
import v052
import v053
import v054
import v055
import v056

APP_VERSION = "0.5.7"
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

app = v056.app


class PaymentDetailsRequest(BaseModel):
    source_ids: list[str] = Field(default_factory=list, max_length=50)


@app.post("/device/mercadopago/payment-details")
async def payment_details_batch(
    request: PaymentDetailsRequest,
    device=Depends(main._device_auth),
):
    clean_ids: list[str] = []
    seen: set[str] = set()
    for raw in request.source_ids:
        clean = str(raw or "").strip()
        if not clean.isdigit() or clean in seen:
            continue
        seen.add(clean)
        clean_ids.append(clean)
        if len(clean_ids) >= 50:
            break

    semaphore = asyncio.Semaphore(6)

    async def one(source_id: str) -> dict[str, Any]:
        async with semaphore:
            try:
                detail = await v056.payment_detail(source_id=source_id, device=device)
                return {"ok": True, "source_id": source_id, "detail": detail}
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                return {
                    "ok": False,
                    "source_id": source_id,
                    "http_status": status,
                    "error": str(getattr(exc, "detail", None) or exc),
                }

    results = await asyncio.gather(*(one(source_id) for source_id in clean_ids))
    return {
        "backend_version": APP_VERSION,
        "requested": len(clean_ids),
        "succeeded": sum(1 for item in results if item.get("ok")),
        "failed": sum(1 for item in results if not item.get("ok")),
        "results": results,
        "policy": {
            "automatic_enrichment_only": True,
            "does_not_import_to_finance_ledger": True,
            "installment_projection_requires_local_card_reconciliation": True,
        },
    }
