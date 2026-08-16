# Mi Finanzas Backend V0.4 — Device Pairing + Payment Preview

## Resultado del diagnóstico V0.3
- OAuth conectado y refresh disponible.
- Scope: `offline_access payments read`.
- Payment Search: disponible.
- Account Money Report: configuración no encontrada (404).

## Nuevo
- Emparejamiento por código de un solo uso (8 dígitos, 10 minutos).
- El APK ya no necesita `APK_API_KEY`.
- Cada dispositivo recibe un token aleatorio propio; el backend guarda sólo su HMAC.
- Revocación por dispositivo.
- Preview administrativo de Payment Search.
- Normalización con `external_id` estable para deduplicación.
- Dirección INCOME/EXPENSE sólo si payer/collector permiten determinarla respecto de la cuenta.
- Casos ambiguos quedan `UNKNOWN` y no deben autoimportarse.

## Endpoints
Admin (requiere X-MiFinanzas-Key):
- POST `/admin/devices/pair-code`
- GET `/api/mercadopago/payment-preview`

Dispositivo:
- POST `/device/pair`
- GET `/device/status`
- GET `/device/mercadopago/movements`
- POST `/device/revoke`

## Siguiente fase
Conectar Android, guardar el token de dispositivo con Android Keystore y sincronizar
sólo movimientos con dirección inequívoca. UNKNOWN queda para revisión manual.
