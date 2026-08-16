# V0.2-HF1 GREENLET

Corrige el fallo de arranque en Render:

`ValueError: the greenlet library is required to use this function`

Cambios:
- SQLAlchemy se instala con el extra `[asyncio]`.
- `greenlet` se declara también de forma explícita.
- Se agrega marca de versión `0.2.1-HF1` en `/health` y `/mercadopago/status`.

Validación esperada:
- `/health` => `{"status":"ok","version":"0.2.1-HF1"}`
- `/mercadopago/status` incluye `version` y `database_configured`.
- `/mercadopago/connect` deja de responder 404.
