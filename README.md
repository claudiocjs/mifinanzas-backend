# Mi Finanzas Backend V0.2 Durable

Agrega persistencia cifrada del OAuth en PostgreSQL, alias `/mercadopago/connect`,
endpoints protegidos para la APK y consulta inicial de cuenta/pagos.

Nuevas variables:
- DATABASE_URL
- APK_API_KEY

El APK nunca recibe `MP_CLIENT_SECRET` ni el Access Token OAuth.

Importante: Payment Search devuelve pagos; no representa necesariamente toda la
actividad de la billetera. El Account Money Report queda para la siguiente fase.
