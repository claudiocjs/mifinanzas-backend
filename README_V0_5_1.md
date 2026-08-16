# Mi Finanzas Backend V0.5.1 — Bootstrap Ledger

Compatibilidad corregida para Render:
- funciona si el servicio arranca con `uvicorn main:app`;
- funciona si el servicio arranca con `uvicorn v051:app`;
- `main_v050.py` conserva intacta la implementación validada de V0.5.0;
- `main.py` es un shim pequeño que expone V0.5.1 y carga el bootstrap ledger.

El bootstrap genera una sola vez un Account Money Report manual de los últimos 30 días, persiste el task ID en Neon y no activa scheduling automático.
