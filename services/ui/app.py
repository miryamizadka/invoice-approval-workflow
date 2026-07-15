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

from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="ApprovalFlow UI Service")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "ui-service"}

    @app.middleware("http")
    async def disable_caching(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # There's no build step or versioned filenames here (N1's role-gated
        # nav made this concrete during manual QA: a plain link click, not
        # just a hard refresh, could keep serving a stale pre-deploy HTML/JS
        # page). `no-cache` (not `no-store`) still lets the browser cache the
        # bytes but forces revalidation against StaticFiles' own
        # ETag/Last-Modified before ever reusing them.
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")

    return app


app = create_app()  # module-level singleton for `uvicorn services.ui.app:app`
