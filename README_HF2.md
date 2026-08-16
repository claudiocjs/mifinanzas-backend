# Mi Finanzas Backend V0.2-HF2 — Neon / asyncpg SSL

## Error corregido
Render fallaba al iniciar con:

`TypeError: connect() got an unexpected keyword argument 'sslmode'`

La cadena de conexión de Neon incluye normalmente parámetros como
`sslmode=require` y `channel_binding=require`. En la integración
SQLAlchemy + asyncpg esos parámetros no deben terminar como kwargs directos
en `asyncpg.connect()`.

## Corrección
- Se conservan usuario, contraseña, host, base y parámetros compatibles.
- Se eliminan `sslmode` y `channel_binding` de la query antes de crear el engine.
- Se obliga SSL con `connect_args={"ssl": "require"}`.
- Se conserva el hotfix anterior de `greenlet` / `SQLAlchemy[asyncio]`.
- Marca de versión: `0.2.2-HF2`.

## Validación esperada
`/health`:
`{"status":"ok","version":"0.2.2-HF2"}`

`/mercadopago/status`:
debe incluir `"database_configured": true`.

`/mercadopago/connect`:
debe redirigir a Mercado Pago en lugar de responder 404.
