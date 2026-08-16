import base64
import hashlib
import os
import secrets
from typing import Optional

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

APP_NAME = "Mi Finanzas Backend"
MP_AUTH_URL = "https://auth.mercadopago.com.ar/authorization"
MP_TOKEN_URL = "https://api.mercadopago.com/oauth/token"

app = FastAPI(title=APP_NAME, version="0.1.0")

CLIENT_ID = os.getenv("MP_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("MP_CLIENT_SECRET", "").strip()
BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "").strip()
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()

_encrypted_token: Optional[bytes] = None


class StatusResponse(BaseModel):
    backend: str
    mercado_pago_configured: bool
    connected: bool
    redirect_uri: str


def _require_config():
    missing = []
    if not CLIENT_ID: missing.append("MP_CLIENT_ID")
    if not CLIENT_SECRET: missing.append("MP_CLIENT_SECRET")
    if not BASE_URL: missing.append("APP_BASE_URL")
    if not APP_SECRET_KEY: missing.append("APP_SECRET_KEY")
    if not TOKEN_ENCRYPTION_KEY: missing.append("TOKEN_ENCRYPTION_KEY")
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


@app.get("/")
async def root():
    return {"name": APP_NAME, "status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/mercadopago/status", response_model=StatusResponse)
async def status():
    configured = all([CLIENT_ID, CLIENT_SECRET, BASE_URL, APP_SECRET_KEY, TOKEN_ENCRYPTION_KEY])
    return StatusResponse(
        backend="online",
        mercado_pago_configured=configured,
        connected=_encrypted_token is not None,
        redirect_uri=_redirect_uri() if BASE_URL else "",
    )


@app.get("/mercadopago/login")
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


@app.post("/mercadopago/disconnect")
async def disconnect():
    global _encrypted_token
    _encrypted_token = None
    return {"connected": False}
