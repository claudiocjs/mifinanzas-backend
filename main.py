import base64
import hashlib
import os
import secrets
import time
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import BigInteger, String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

APP_NAME = "Mi Finanzas Backend"
MP_AUTH_URL = "https://auth.mercadopago.com.ar/authorization"
MP_TOKEN_URL = "https://api.mercadopago.com/oauth/token"

APP_VERSION = "0.3.0"

app = FastAPI(title=APP_NAME, version=APP_VERSION)

CLIENT_ID = os.getenv("MP_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("MP_CLIENT_SECRET", "").strip()
BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "").strip()
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
APK_API_KEY = os.getenv("APK_API_KEY", "").strip()


def _db_url():
    """
    Normaliza la URL de Neon para SQLAlchemy + asyncpg.

    Neon entrega normalmente ?sslmode=require&channel_binding=require.
    SQLAlchemy convierte la URL a argumentos DBAPI y asyncpg no acepta
    'sslmode'/'channel_binding' como kwargs directos. Se eliminan de la URL
    y SSL se exige mediante connect_args={"ssl": "require"}.
    """
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
        now = int(__import__("time").time())
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


def _apk_auth(x_mifinanzas_key: Optional[str] = __import__("fastapi").Header(default=None)):
    if not APK_API_KEY:
        raise HTTPException(status_code=503, detail="APK_API_KEY no configurada")
    if not x_mifinanzas_key or not secrets.compare_digest(x_mifinanzas_key, APK_API_KEY):
        raise HTTPException(status_code=401, detail="No autorizado")


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
async def callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
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
        r = await client.post(
            MP_TOKEN_URL,
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

    if r.status_code >= 400:
        return HTMLResponse(
            f"<h2>No se pudo obtener el token</h2><p>HTTP {r.status_code}</p><pre>{r.text[:1500]}</pre>",
            status_code=502,
        )

    token_json = r.json()
    token_json["_obtained_at"] = int(__import__("time").time())
    await _save_token(token_json)
    _encrypted_token = _fernet().encrypt(r.text.encode("utf-8"))

    response = HTMLResponse(
        """
        <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
        <body style="font-family:sans-serif;padding:32px">
        <h1>✅ Mercado Pago vinculado</h1>
        <p>Mi Finanzas recibió y cifró el token en el backend.</p>
        <p>Ya podés cerrar esta ventana.</p>
        </body></html>
        """
    )
    response.delete_cookie("mf_mp_state", path="/")
    response.delete_cookie("mf_mp_verifier", path="/")
    return response


async def _refresh_oauth_token(token: dict, force: bool = False) -> dict:
    """
    Renueva el Access Token usando el refresh_token persistido.
    Mercado Pago puede devolver un refresh_token nuevo, por eso siempre
    persistimos la respuesta completa.
    """
    expires_in = int(token.get("expires_in", 0) or 0)
    obtained_at = int(token.get("_obtained_at", 0) or 0)
    now = int(time.time())
    expires_soon = (
        expires_in > 0
        and obtained_at > 0
        and now >= (obtained_at + expires_in - 300)
    )

    if not force and not expires_soon:
        return token

    refresh_token = token.get("refresh_token")
    if not refresh_token:
        if force:
            raise HTTPException(
                status_code=409,
                detail="La autorización no contiene refresh_token. Es necesario volver a vincular Mercado Pago.",
            )
        return token

    payload = {
        "client_secret": CLIENT_SECRET,
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            MP_TOKEN_URL,
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo renovar el token de Mercado Pago (HTTP {response.status_code}).",
        )

    refreshed = response.json()
    refreshed["_obtained_at"] = int(time.time())

    # Si MP no rotara alguno de los campos, preservamos el valor anterior.
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


async def _mp_get(
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
) -> httpx.Response:
    """
    GET autenticado. Si MP responde 401, fuerza una renovación y reintenta una vez.
    """
    access = await _current_access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
        )

    if response.status_code == 401:
        access = await _current_access_token(force_refresh=True)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
            )
    return response


@app.get("/api/mercadopago/account", dependencies=[Depends(_apk_auth)])
async def account():
    r = await _mp_get("https://api.mercadolibre.com/users/me")
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"users/me HTTP {r.status_code}")
    d = r.json()
    return {
        "connected": True,
        "id": d.get("id"),
        "nickname": d.get("nickname"),
        "first_name": d.get("first_name"),
        "last_name": d.get("last_name"),
        "email": d.get("email"),
        "country_id": d.get("country_id"),
    }


@app.get("/api/mercadopago/payments", dependencies=[Depends(_apk_auth)])
async def payments(limit: int = 20, offset: int = 0, days: int = 30):
    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    days = max(1, min(days, 364))

    params = {
        "limit": limit,
        "offset": offset,
        "sort": "date_created",
        "criteria": "desc",
        "range": "date_created",
        "begin_date": f"NOW-{days}DAYS",
        "end_date": "NOW",
    }

    r = await _mp_get(
        "https://api.mercadopago.com/v1/payments/search",
        params=params,
    )

    if r.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Payment Search HTTP {r.status_code}",
        )

    data = r.json()
    return {
        "source": "payment_search",
        "window_days": days,
        "paging": data.get("paging", {}),
        "results": [
            {
                "id": x.get("id"),
                "date_created": x.get("date_created"),
                "date_approved": x.get("date_approved"),
                "date_last_updated": x.get("date_last_updated"),
                "status": x.get("status"),
                "status_detail": x.get("status_detail"),
                "operation_type": x.get("operation_type"),
                "transaction_amount": x.get("transaction_amount"),
                "transaction_amount_refunded": x.get("transaction_amount_refunded"),
                "currency_id": x.get("currency_id"),
                "description": x.get("description"),
                "payment_method_id": x.get("payment_method_id"),
                "payment_type_id": x.get("payment_type_id"),
                "installments": x.get("installments"),
                "external_reference": x.get("external_reference"),
            } for x in data.get("results", [])
        ],
    }


@app.get("/api/mercadopago/account-money/config", dependencies=[Depends(_apk_auth)])
async def account_money_config():
    """
    Diagnóstico de disponibilidad del Reporte de Todas las transacciones.
    Es sólo lectura: no crea ni modifica reportes.
    """
    r = await _mp_get(
        "https://api.mercadopago.com/v1/account/settlement_report/config"
    )

    if r.status_code == 404:
        return {
            "available": False,
            "configured": False,
            "http_status": 404,
            "detail": "La cuenta todavía no tiene configuración del reporte.",
        }

    if r.status_code >= 400:
        return {
            "available": False,
            "configured": False,
            "http_status": r.status_code,
            "detail": "Mercado Pago no habilitó este recurso con la autorización actual.",
        }

    data = r.json()
    return {
        "available": True,
        "configured": True,
        "http_status": 200,
        "scheduled": data.get("scheduled"),
        "file_name_prefix": data.get("file_name_prefix"),
        "include_withdraw": data.get("include_withdraw"),
        "display_timezone": data.get("display_timezone"),
    }


@app.get("/api/mercadopago/capabilities", dependencies=[Depends(_apk_auth)])
async def capabilities():
    """
    Prueba no destructiva: comprueba token/refresh y disponibilidad de las dos
    fuentes iniciales sin devolver datos financieros sensibles.
    """
    payments_r = await _mp_get(
        "https://api.mercadopago.com/v1/payments/search",
        params={
            "limit": 1,
            "offset": 0,
            "sort": "date_created",
            "criteria": "desc",
            "range": "date_created",
            "begin_date": "NOW-30DAYS",
            "end_date": "NOW",
        },
    )
    report_r = await _mp_get(
        "https://api.mercadopago.com/v1/account/settlement_report/config"
    )

    token = await _current_token()
    return {
        "oauth": {
            "connected": True,
            "refresh_available": bool(token.get("refresh_token")),
            "scope": token.get("scope"),
        },
        "payment_search": {
            "http_status": payments_r.status_code,
            "available": payments_r.status_code == 200,
        },
        "account_money_report": {
            "http_status": report_r.status_code,
            "available": report_r.status_code == 200,
            "configured": report_r.status_code == 200,
        },
    }


@app.post("/api/mercadopago/disconnect", dependencies=[Depends(_apk_auth)])
async def disconnect():
    global _encrypted_token
    await _delete_token()
    _encrypted_token = None
    return {"connected": False}
