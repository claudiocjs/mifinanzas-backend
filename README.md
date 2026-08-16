# Mi Finanzas - Backend Mercado Pago V0.1

Backend FastAPI para validar OAuth + PKCE de Mercado Pago sin guardar secretos en el APK.

Endpoints:
- /health
- /mercadopago/status
- /mercadopago/login
- /mercadopago/callback
- POST /mercadopago/disconnect

Variables de entorno:
- MP_CLIENT_ID
- MP_CLIENT_SECRET
- APP_BASE_URL
- APP_SECRET_KEY
- TOKEN_ENCRYPTION_KEY

Redirect URI:
https://TU-SERVICIO.onrender.com/mercadopago/callback

V0.1 guarda el token cifrado solo en memoria para validar primero el flujo OAuth.
La persistencia durable se agrega después de confirmar que la autorización funciona.
