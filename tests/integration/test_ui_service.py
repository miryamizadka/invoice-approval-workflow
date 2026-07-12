"""Integration tests for the UI Service - a static file mount, "logic free"
exactly like the Gateway. There is no business logic here for Python to
test beyond "are the files actually served" - the HTML/CSS/JS content
itself is verified by reading it directly and via live docker-compose
verification (see PLAN.md), not browser-automation tests (none set up in
this project - see ADR-010).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from services.ui.app import create_app


def test_health_check() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ui-service"}


def test_index_html_served_at_root() -> None:
    client = TestClient(create_app())

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<form" in response.text


def test_approvals_html_served() -> None:
    client = TestClient(create_app())

    response = client.get("/approvals.html")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_dashboard_html_served() -> None:
    client = TestClient(create_app())

    response = client.get("/dashboard.html")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_static_js_and_css_served() -> None:
    client = TestClient(create_app())

    assert client.get("/api.js").status_code == 200
    assert client.get("/submitter.js").status_code == 200
    assert client.get("/approver.js").status_code == 200
    assert client.get("/dashboard.js").status_code == 200
    assert client.get("/style.css").status_code == 200
