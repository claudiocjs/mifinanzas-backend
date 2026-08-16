# Mi Finanzas Backend V0.3

## Implementado
- Refresh automático del Access Token antes de vencer.
- Reintento automático una vez si Mercado Pago responde HTTP 401.
- Persistencia del refresh token rotado en Neon, cifrado con Fernet.
- `/mercadopago/status` informa si existe refresh token y tiempo restante del Access Token.
- Payment Search acepta ventana de 1 a 364 días.
- Diagnóstico de Account Money Report sin crear/modificar reportes.
- Endpoint protegido `/api/mercadopago/capabilities`.

## Seguridad
Los endpoints con datos de cuenta siguen protegidos con `X-MiFinanzas-Key`.
Esa clave NO se integrará fija dentro del APK final. La siguiente etapa reemplaza
ese mecanismo por emparejamiento por dispositivo antes de activar sincronización Android.

## Fuentes
- Payment Search: pagos de hasta los últimos 12 meses.
- Account Money Report: operaciones que afectaron el balance de la cuenta.
