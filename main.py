"""Mi Finanzas backend compatibility entrypoint.

This shim keeps both Render start commands working:
- uvicorn main:app
- uvicorn v051:app

The validated V0.5.0 implementation lives unchanged in main_v050.py.
V0.5.1 adds the one-shot 30-day Account Money bootstrap ledger.
"""

import sys
import main_v050 as _base

# Make every V0.5 route report the effective runtime version too.
_base.APP_VERSION = "0.5.1"
_base.app.version = "0.5.1"

APP_VERSION = "0.5.1"
app = _base.app


def __getattr__(name):
    """Proxy private/public helpers used by v051 to the validated V0.5 module."""
    return getattr(_base, name)


# If Render still runs `uvicorn main:app`, load the V0.5.1 extension here.
# If Render already runs `uvicorn v051:app`, v051 is currently importing this
# module, so let it resume naturally and register its routes exactly once.
if "v051" not in sys.modules:
    import v051 as _v051  # noqa: F401,E402
