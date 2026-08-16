# Mi Finanzas Backend V0.5 — Account Money Report activation

V0.5 activa de forma idempotente la configuración de `Todas las transacciones`
(Account Money Report) cuando Mercado Pago aún responde 404.

- No genera reportes históricos todavía.
- No activa reportes programados/automáticos.
- Incluye retiros (`include_withdraw=true`).
- Usa `SOURCE_ID`, `DESCRIPTION`, `TRANSACTION_TYPE`,
  `TRANSACTION_AMOUNT`, `SETTLEMENT_NET_AMOUNT`, `REAL_AMOUNT`,
  fechas, moneda, comisiones y medio de pago.
- Expone estado sanitizado en:
  `GET /mercadopago/account-money/status`

Validación esperada después del deploy:
- `backend_version: 0.5.0`
- `configured: true`
- `scheduled: false`
- `http_status: 200`

Si Mercado Pago rechaza el POST por permisos, el endpoint mostrará el
código HTTP sin exponer Access Token ni credenciales.
