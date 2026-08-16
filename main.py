import base64
import hashlib
import os
import secrets
from typing import Optional

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

APP_VERSION = "0.2.1-HF1"

app = FastAPI(title=APP_NAME, version=APP_VERSION)

CLIENT_ID = os.getenv("MP_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("MP_CLIENT_SECRET", "").strip()
BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "").strip()
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
APK_API_KEY = os.getenv("APK_API_KEY", "").strip()


def _db_url():
    if DATABASE_URL.startswith("postgresql://"):
        return DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    if DATABASE_URL.startswith("postgres://"):
        return DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
    return DATABASE_URL


class Base(DeclarativeBase):
    pass


class SecretState(Base):
    __tablename__ = "secret_state"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    encrypted_value: Mapped[str] = mapped_column(String, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)


engine = create_async_engine(_db_url(), pool_pre_ping=True) if _db_url() else None
SessionLocal = async_sessionmaker(engine, expire_on_commit=False) if engine else None

_encrypted_token: Optional[bytes] = None


class StatusResponse(BaseModel):
    backend: str
    version: str
    mercado_pago_configured: bool
    connected: bool
    redirect_uri: str
    database_configured: bool = False


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
    return StatusResponse(
        backend="online",
        version=APP_VERSION,
        mercado_pago_configured=configured,
        connected=token is not None,
        redirect_uri=_redirect_uri() if BASE_URL else "",
        database_configured=bool(DATABASE_URL),
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
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
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


async def _current_access_token():
    token = await _load_token()
    if not token:
        raise HTTPException(status_code=409, detail="Mercado Pago no está vinculado")
    return token.get("access_token")


@app.get("/api/mercadopago/account", dependencies=[Depends(_apk_auth)])
async def account():
    access = await _current_access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://api.mercadolibre.com/users/me",
            headers={"Authorization": f"Bearer {access}", "accept": "application/json"},
        )
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
async def payments(limit: int = 20, offset: int = 0):
    access = await _current_access_token()
    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://api.mercadopago.com/v1/payments/search",
            params={"limit": limit, "offset": offset, "sort": "date_created", "criteria": "desc"},
            headers={"Authorization": f"Bearer {access}", "accept": "application/json"},
        )
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Payment Search HTTP {r.status_code}")
    data = r.json()
    return {
        "paging": data.get("paging", {}),
        "results": [
            {
                "id": x.get("id"),
                "date_created": x.get("date_created"),
                "date_approved": x.get("date_approved"),
                "status": x.get("status"),
                "operation_type": x.get("operation_type"),
                "transaction_amount": x.get("transaction_amount"),
                "currency_id": x.get("currency_id"),
                "description": x.get("description"),
                "payment_method_id": x.get("payment_method_id"),
                "payment_type_id": x.get("payment_type_id"),
            } for x in data.get("results", [])
        ],
    }


@app.post("/api/mercadopago/disconnect", dependencies=[Depends(_apk_auth)])
async def disconnect():
    global _encrypted_token
    await _delete_token()
    _encrypted_token = None
    return {"connected": False}
