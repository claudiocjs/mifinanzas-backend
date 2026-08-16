import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import BigInteger, Boolean, String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

APP_NAME = "Mi Finanzas Backend"
MP_AUTH_URL = "https://auth.mercadopago.com.ar/authorization"
MP_TOKEN_URL = "https://api.mercadopago.com/oauth/token"

APP_VERSION = "0.4.0"

app = FastAPI(title=APP_NAME, version=APP_VERSION)

CLIENT_ID = os.getenv("MP_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("MP_CLIENT_SECRET", "").strip()
BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "").strip()
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
APK_API_KEY = os.getenv("APK_API_KEY", "").strip()


def _db_url():
    if not DATABASE_URL:
        return ""

    url = DATABASE_URL
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)

    parts = urlsplit(url)
    filtered_query = [
        (k, v)
        for (k, v) in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in {"sslmode", "channel_binding"}
    ]
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(filtered_query),
        parts.fragment,
    ))


class Base(DeclarativeBase):
    pass


class SecretState(Base):
    __tablename__ = "secret_state"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    encrypted_value: Mapped[str] = mapped_column(String, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seen_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PairCode(Base):
    __tablename__ = "pair_codes"
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)


engine = create_async_engine(
    _db_url(),
    pool_pre_ping=True,
    connect_args={"ssl": "require"},
) if _db_url() else None
SessionLocal = async_sessionmaker(engine, expire_on_commit=False) if engine else None

_encrypted_token: Optional[bytes] = None


class StatusResponse(BaseModel):
    backend: str
    version: str
    mercado_pago_configured: bool
    connected: bool
    redirect_uri: str
    database_configured: bool = False
    refresh_available: bool = False
    token_expires_in_seconds: Optional[int] = None


class PairRequest(BaseModel):
    code: str
    device_name: str = "Android"


class PairResponse(BaseModel):
    device_id: str
    device_token: str
    backend_url: str


class DeviceStatusResponse(BaseModel):
    backend_version: str
    device_id: str
    device_name: str
    mercado_pago_connected: bool


def _require_config():
    missing = []
    if not CLIENT_ID: missing.append("MP_CLIENT_ID")
    if not CLIENT_SECRET: missing.append("MP_CLIENT_SECRET")
    if not BASE_URL: missing.append("APP_BASE_URL")
    if not APP_SECRET_KEY: missing.append("APP_SECRET_KEY")
    if not TOKEN_ENCRYPTION_KEY: missing.append("TOKEN_ENCRYPTION_KEY")
    if not DATABASE_URL: missing.append("DATABASE_URL")
    if missing:
        raise HTTPException(status_code=503, detail="Faltan variables: " + ", ".join(missing))


def _redirect_uri():
    return f"{BASE_URL}/mercadopago/callback"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _pkce_pair():
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _fernet():
    try:
        return Fernet(TOKEN_ENCRYPTION_KEY.encode())
    except Exception as exc:
        raise HTTPException(status_code=503, detail="TOKEN_ENCRYPTION_KEY inválida") from exc


async def _save_token(payload: dict):
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL no configurada")
    encrypted = _fernet().encrypt(__import__("json").dumps(payload).encode()).decode()
    async with SessionLocal() as session:
        row = await session.get(SecretState, "mercadopago_oauth")
        now = int(time.time())
        if row:
            row.encrypted_value = encrypted
            row.updated_at_epoch = now
        else:
            session.add(SecretState(key="mercadopago_oauth", encrypted_value=encrypted, updated_at_epoch=now))
        await session.commit()


async def _load_token():
    if SessionLocal is None:
        return None
    async with SessionLocal() as session:
        row = await session.get(SecretState, "mercadopago_oauth")
        if not row:
            return None
        raw = _fernet().decrypt(row.encrypted_value.encode())
        return __import__("json").loads(raw.decode())


async def _delete_token():
    if SessionLocal is None:
        return
    async with SessionLocal() as session:
        row = await session.get(SecretState, "mercadopago_oauth")
        if row:
            await session.delete(row)
            await session.commit()


def _apk_auth(x_mifinanzas_key: Optional[str] = Header(default=None)):
    if not APK_API_KEY:
        raise HTTPException(status_code=503, detail="APK_API_KEY no configurada")
    if not x_mifinanzas_key or not secrets.compare_digest(x_mifinanzas_key, APK_API_KEY):
        raise HTTPException(status_code=401, detail="No autorizado")


def _secret_hash(value: str) -> str:
    key = (APP_SECRET_KEY or "mifinanzas").encode("utf-8")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


async def _device_auth(authorization: Optional[str] = Header(default=None)) -> Device:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token de dispositivo requerido")
    raw = authorization[7:].strip()
    if not raw:
        raise HTTPException(status_code=401, detail="Token de dispositivo inválido")
    token_hash = _secret_hash(raw)
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL no configurada")
    async with SessionLocal() as session:
        result = await session.execute(__import__("sqlalchemy").select(Device).where(Device.token_hash == token_hash))
        device = result.scalar_one_or_none()
        if not device or device.revoked:
            raise HTTPException(status_code=401, detail="Dispositivo no autorizado")
        device.last_seen_epoch = int(time.time())
        await session.commit()
        await session.refresh(device)
        return device


@app.on_event("startup")
async def startup():
    if engine:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)


@app.get("/")
async def root():
    return {"name": APP_NAME, "version": APP_VERSION, "status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/mercadopago/status", response_model=StatusResponse)
async def status():
    configured = all([CLIENT_ID, CLIENT_SECRET, BASE_URL, APP_SECRET_KEY, TOKEN_ENCRYPTION_KEY])
    token = await _load_token()
    expires_left = None
    if token:
        obtained = int(token.get("_obtained_at", 0) or 0)
        expires_in = int(token.get("expires_in", 0) or 0)
        if obtained > 0 and expires_in > 0:
            expires_left = max(0, obtained + expires_in - int(time.time()))
    return StatusResponse(
        backend="online",
        version=APP_VERSION,
        mercado_pago_configured=configured,
        connected=token is not None,
        redirect_uri=_redirect_uri() if BASE_URL else "",
        database_configured=bool(DATABASE_URL),
        refresh_available=bool(token and token.get("refresh_token")),
        token_expires_in_seconds=expires_left,
    )


@app.get("/mercadopago/login")
@app.get("/mercadopago/connect")
async def login():
    _require_config()
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "platform_id": "mp",
        "redirect_uri": _redirect_uri(),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    request = httpx.Request("GET", MP_AUTH_URL, params=params)
    response = RedirectResponse(str(request.url), status_code=302)
    cookie = dict(httponly=True, secure=True, samesite="lax", max_age=600, path="/")
    response.set_cookie("mf_mp_state", state, **cookie)
    response.set_cookie("mf_mp_verifier", verifier, **cookie)
    return response


@app.get("/mercadopago/callback")
async def callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    global _encrypted_token
    _require_config()
    if error:
        return HTMLResponse(f"<h2>Autorización rechazada</h2><p>{error}</p>", status_code=400)
    cookie_state = request.cookies.get("mf_mp_state")
    verifier = request.cookies.get("mf_mp_verifier")
    if not code:
        raise HTTPException(status_code=400, detail="Falta code OAuth")
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        raise HTTPException(status_code=400, detail="State OAuth inválido")
    if not verifier:
        raise HTTPException(status_code=400, detail="Falta code_verifier PKCE")
    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "redirect_uri": _redirect_uri(),
        "code_verifier": verifier,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(MP_TOKEN_URL, json=payload, headers={"Content-Type": "application/json", "Accept": "application/json"})
    if r.status_code >= 400:
        return HTMLResponse(f"<h2>No se pudo obtener el token</h2><p>HTTP {r.status_code}</p><pre>{r.text[:1500]}</pre>", status_code=502)
    token_json = r.json()
    token_json["_obtained_at"] = int(time.time())
    await _save_token(token_json)
    _encrypted_token = _fernet().encrypt(r.text.encode("utf-8"))
    response = HTMLResponse("<h1>✅ Mercado Pago vinculado</h1><p>Mi Finanzas recibió y cifró el token en el backend.</p><p>Ya podés cerrar esta ventana.</p>")
    response.delete_cookie("mf_mp_state", path="/")
    response.delete_cookie("mf_mp_verifier", path="/")
    return response


async def _refresh_oauth_token(token: dict, force: bool = False) -> dict:
    expires_in = int(token.get("expires_in", 0) or 0)
    obtained_at = int(token.get("_obtained_at", 0) or 0)
    now = int(time.time())
    expires_soon = expires_in > 0 and obtained_at > 0 and now >= (obtained_at + expires_in - 300)
    if not force and not expires_soon:
        return token
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        if force:
            raise HTTPException(status_code=409, detail="La autorización no contiene refresh_token. Es necesario volver a vincular Mercado Pago.")
        return token
    payload = {"client_secret": CLIENT_SECRET, "client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": refresh_token}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(MP_TOKEN_URL, json=payload, headers={"Content-Type": "application/json", "Accept": "application/json"})
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"No se pudo renovar el token de Mercado Pago (HTTP {response.status_code}).")
    refreshed = response.json()
    refreshed["_obtained_at"] = int(time.time())
    if not refreshed.get("refresh_token") and token.get("refresh_token"):
        refreshed["refresh_token"] = token["refresh_token"]
    await _save_token(refreshed)
    return refreshed


async def _current_token(force_refresh: bool = False) -> dict:
    token = await _load_token()
    if not token:
        raise HTTPException(status_code=409, detail="Mercado Pago no está vinculado")
    return await _refresh_oauth_token(token, force=force_refresh)


async def _current_access_token(force_refresh: bool = False) -> str:
    token = await _current_token(force_refresh=force_refresh)
    access = token.get("access_token")
    if not access:
        raise HTTPException(status_code=500, detail="OAuth almacenado sin access_token")
    return access


async def _mp_get(url: str, *, params: Optional[dict[str, Any]] = None) -> httpx.Response:
    access = await _current_access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, params=params, headers={"Authorization": f"Bearer {access}", "Accept": "application/json"})
    if response.status_code == 401:
        access = await _current_access_token(force_refresh=True)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, params=params, headers={"Authorization": f"Bearer {access}", "Accept": "application/json"})
    return response


def _category_hint(description: Optional[str]) -> str:
    text = (description or "").lower()
    groups = [
        ("Supermercado", ("chango", "carrefour", "coto", "jumbo", "disco", "vea", "supermercado")),
        ("Auto", ("ypf", "shell", "axion", "combustible", "nafta", "gasolina")),
        ("Teléfono", ("personal", "claro", "movistar", "tuenti")),
        ("Salud", ("farmacia", "sanatorio", "clinica", "clínica")),
        ("Comida", ("kiosco", "restaurante", "delivery", "pedidosya", "rappi")),
    ]
    for category, words in groups:
        if any(word in text for word in words):
            return category
    return "Otros"


async def _account_id() -> Optional[int]:
    r = await _mp_get("https://api.mercadolibre.com/users/me")
    if r.status_code >= 400:
        return None
    try:
        return int(r.json().get("id"))
    except Exception:
        return None


def _normalize_payment(item: dict, account_id: Optional[int]) -> dict:
    payer = item.get("payer") or {}
    collector = item.get("collector") or {}
    payer_id = payer.get("id")
    collector_id = collector.get("id")
    direction = "UNKNOWN"
    if account_id is not None:
        if str(collector_id) == str(account_id) and str(payer_id) != str(account_id):
            direction = "INCOME"
        elif str(payer_id) == str(account_id) and str(collector_id) != str(account_id):
            direction = "EXPENSE"
    amount = float(item.get("transaction_amount") or 0.0)
    refunded = float(item.get("transaction_amount_refunded") or 0.0)
    description = item.get("description") or "Mercado Pago"
    return {
        "external_id": f"mp:payment:{item.get('id')}",
        "payment_id": item.get("id"),
        "date": item.get("date_approved") or item.get("date_created"),
        "last_updated": item.get("date_last_updated"),
        "status": item.get("status"),
        "status_detail": item.get("status_detail"),
        "operation_type": item.get("operation_type"),
        "direction": direction,
        "amount": amount,
        "refunded_amount": refunded,
        "net_candidate": amount - refunded,
        "currency": item.get("currency_id"),
        "description": description,
        "suggested_category": _category_hint(description),
        "payment_method_id": item.get("payment_method_id"),
        "payment_type_id": item.get("payment_type_id"),
        "installments": item.get("installments"),
    }


async def _payment_candidates(days: int, limit: int, offset: int = 0) -> tuple[dict, list[dict]]:
    days = max(1, min(days, 364))
    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    r = await _mp_get(
        "https://api.mercadopago.com/v1/payments/search",
        params={"limit": limit, "offset": offset, "sort": "date_created", "criteria": "desc", "range": "date_created", "begin_date": f"NOW-{days}DAYS", "end_date": "NOW"},
    )
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Payment Search HTTP {r.status_code}")
    data = r.json()
    account_id = await _account_id()
    normalized = [_normalize_payment(x, account_id) for x in data.get("results", [])]
    return data.get("paging", {}), normalized


@app.get("/api/mercadopago/account", dependencies=[Depends(_apk_auth)])
async def account():
    r = await _mp_get("https://api.mercadolibre.com/users/me")
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"users/me HTTP {r.status_code}")
    d = r.json()
    return {"connected": True, "id": d.get("id"), "nickname": d.get("nickname"), "first_name": d.get("first_name"), "last_name": d.get("last_name"), "email": d.get("email"), "country_id": d.get("country_id")}


@app.get("/api/mercadopago/payments", dependencies=[Depends(_apk_auth)])
async def payments(limit: int = 20, offset: int = 0, days: int = 30):
    paging, items = await _payment_candidates(days=days, limit=limit, offset=offset)
    return {"source": "payment_search", "window_days": max(1, min(days, 364)), "paging": paging, "results": items}


@app.get("/api/mercadopago/account-money/config", dependencies=[Depends(_apk_auth)])
async def account_money_config():
    r = await _mp_get("https://api.mercadopago.com/v1/account/settlement_report/config")
    if r.status_code == 404:
        return {"available": False, "configured": False, "http_status": 404, "detail": "La cuenta todavía no tiene configuración del reporte."}
    if r.status_code >= 400:
        return {"available": False, "configured": False, "http_status": r.status_code, "detail": "Mercado Pago no habilitó este recurso con la autorización actual."}
    data = r.json()
    return {"available": True, "configured": True, "http_status": 200, "scheduled": data.get("scheduled"), "file_name_prefix": data.get("file_name_prefix"), "include_withdraw": data.get("include_withdraw"), "display_timezone": data.get("display_timezone")}


@app.get("/api/mercadopago/capabilities", dependencies=[Depends(_apk_auth)])
async def capabilities():
    payments_r = await _mp_get("https://api.mercadopago.com/v1/payments/search", params={"limit": 1, "offset": 0, "sort": "date_created", "criteria": "desc", "range": "date_created", "begin_date": "NOW-30DAYS", "end_date": "NOW"})
    report_r = await _mp_get("https://api.mercadopago.com/v1/account/settlement_report/config")
    token = await _current_token()
    return {"oauth": {"connected": True, "refresh_available": bool(token.get("refresh_token")), "scope": token.get("scope")}, "payment_search": {"http_status": payments_r.status_code, "available": payments_r.status_code == 200}, "account_money_report": {"http_status": report_r.status_code, "available": report_r.status_code == 200, "configured": report_r.status_code == 200}}


@app.get("/api/mercadopago/payment-preview", dependencies=[Depends(_apk_auth)])
async def payment_preview(days: int = 30, limit: int = 20):
    paging, items = await _payment_candidates(days=days, limit=limit)
    counts = {"INCOME": 0, "EXPENSE": 0, "UNKNOWN": 0}
    totals = {"INCOME": 0.0, "EXPENSE": 0.0, "UNKNOWN": 0.0}
    operation_types: dict[str, int] = {}
    for item in items:
        direction = item["direction"]
        counts[direction] = counts.get(direction, 0) + 1
        totals[direction] = totals.get(direction, 0.0) + float(item["net_candidate"] or 0.0)
        op = item.get("operation_type") or "unknown"
        operation_types[op] = operation_types.get(op, 0) + 1
    return {"source": "payment_search", "window_days": max(1, min(days, 364)), "paging": paging, "summary": {"counts": counts, "totals": totals, "operation_types": operation_types}, "items": items}


@app.post("/admin/devices/pair-code", dependencies=[Depends(_apk_auth)])
async def create_pair_code():
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL no configurada")
    code = f"{secrets.randbelow(100_000_000):08d}"
    now = int(time.time())
    expires = now + 600
    code_hash = _secret_hash(code)
    async with SessionLocal() as session:
        await session.execute(__import__("sqlalchemy").delete(PairCode).where((PairCode.expires_at_epoch < now) | (PairCode.used == True)))
        session.add(PairCode(code_hash=code_hash, expires_at_epoch=expires, used=False, created_at_epoch=now))
        await session.commit()
    return {"pair_code": code, "expires_in_seconds": 600}


@app.post("/device/pair", response_model=PairResponse)
async def pair_device(request: PairRequest):
    code = request.code.strip()
    name = request.device_name.strip()[:120] or "Android"
    if len(code) != 8 or not code.isdigit():
        raise HTTPException(status_code=400, detail="Código de emparejamiento inválido")
    now = int(time.time())
    code_hash = _secret_hash(code)
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL no configurada")
    async with SessionLocal() as session:
        pair = await session.get(PairCode, code_hash)
        if not pair or pair.used or pair.expires_at_epoch < now:
            raise HTTPException(status_code=401, detail="Código vencido o inválido")
        raw_token = secrets.token_urlsafe(48)
        device_id = secrets.token_hex(16)
        session.add(Device(id=device_id, name=name, token_hash=_secret_hash(raw_token), created_at_epoch=now, last_seen_epoch=now, revoked=False))
        pair.used = True
        await session.commit()
    return PairResponse(device_id=device_id, device_token=raw_token, backend_url=BASE_URL)


@app.get("/device/status", response_model=DeviceStatusResponse)
async def device_status(device: Device = Depends(_device_auth)):
    token = await _load_token()
    return DeviceStatusResponse(backend_version=APP_VERSION, device_id=device.id, device_name=device.name, mercado_pago_connected=token is not None)


@app.get("/device/mercadopago/movements")
async def device_movements(days: int = 30, limit: int = 50, device: Device = Depends(_device_auth)):
    paging, items = await _payment_candidates(days=days, limit=limit)
    return {"source": "MERCADO_PAGO", "paging": paging, "items": items}


@app.post("/device/revoke")
async def revoke_self(device: Device = Depends(_device_auth)):
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL no configurada")
    async with SessionLocal() as session:
        current = await session.get(Device, device.id)
        if current:
            current.revoked = True
            await session.commit()
    return {"revoked": True}


@app.post("/api/mercadopago/disconnect", dependencies=[Depends(_apk_auth)])
async def disconnect():
    global _encrypted_token
    await _delete_token()
    _encrypted_token = None
    return {"connected": False}
