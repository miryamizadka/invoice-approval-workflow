"""FastAPI transport layer for the UI service - a static file mount, "logic
free" exactly like the Gateway (ARCHITECTURE.md §4). No business logic here:
the browser's own JS calls the existing REST API directly through the same
gateway (`/invoices`, `/approvals`, ...) - this app's only job is serving
the HTML/CSS/JS files themselves.

`/health` is registered before the static mount so it isn't shadowed by it -
Starlette matches routes in registration order, and a specific path always
wins over a catch-all `Mount("/")` if registered first.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="ApprovalFlow UI Service")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "ui-service"}

    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")

    return app


app = create_app()  # module-level singleton for `uvicorn services.ui.app:app`
