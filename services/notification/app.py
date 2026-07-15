"""FastAPI transport layer - the only place in this service that knows HTTP
(and, via DaprApp, Dapr's pub/sub subscription wiring). Endpoints only call
NotificationService; all business logic lives there.
"""

from __future__ import annotations

import logging
from typing import Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from services.notification.accessors.logging_channel import LoggingNotificationChannel
from services.notification.accessors.notification_channel import NotificationChannel
from services.notification.dapr_state_repository import DaprStateNotificationRepository
from services.notification.logging_config import configure_logging
from services.notification.repository import NotificationRepository
from services.notification.service import NotificationService, build_notification_service
from shared.auth import AuthenticatedUser, Role, require_role
from shared.contracts.models import (
    ApprovalCompletedEvent,
    DecisionCompletedEvent,
    PaymentCompletedEvent,
)


def create_app(
    repository: NotificationRepository | None = None,
    channel: NotificationChannel | None = None,
) -> FastAPI:
    configure_logging()
    notification_service = build_notification_service(
        repository or DaprStateNotificationRepository(),
        channel or LoggingNotificationChannel(),
    )

    app = FastAPI(title="ApprovalFlow Notification Service")
    app.state.notification_service = notification_service
    dapr_app = DaprApp(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "notification-service"}

    @app.get("/notifications/{tracking_id}")
    async def get_notification_status(
        tracking_id: str,
        request: Request,
        user: AuthenticatedUser = Depends(require_role(Role.ADMIN)),
    ) -> dict[str, Any]:
        """Ops/debug endpoint - not a public API. Always returns 200: "not
        yet notified" is a valid, non-error state, not a 404 (unlike
        Payment's GET /payments/{id}, where an unknown tracking_id is a
        real error). Not guaranteed to be retained forever - if a future
        TTL/periodic-deletion policy is added to the underlying Dapr state,
        this endpoint remains correct (an expired/deleted key just reads
        back as `notified: false`)."""
        service: NotificationService = request.app.state.notification_service
        notified = await service.already_notified(tracking_id)
        return {"tracking_id": tracking_id, "notified": notified}

    @dapr_app.subscribe(
        pubsub="pubsub", topic="decision.completed", route="/events/decision-completed"
    )
    async def handle_decision_completed(request: Request) -> dict[str, str]:
        body: dict[str, Any] = await request.json()
        event = DecisionCompletedEvent.model_validate(body["data"])
        service: NotificationService = request.app.state.notification_service
        await service.handle_decision_completed(event)
        return {"status": "SUCCESS"}

    @dapr_app.subscribe(
        pubsub="pubsub", topic="approval.completed", route="/events/approval-completed"
    )
    async def handle_approval_completed(request: Request) -> dict[str, str]:
        body: dict[str, Any] = await request.json()
        event = ApprovalCompletedEvent.model_validate(body["data"])
        service: NotificationService = request.app.state.notification_service
        await service.handle_approval_completed(event)
        return {"status": "SUCCESS"}

    @dapr_app.subscribe(
        pubsub="pubsub", topic="payment.completed", route="/events/payment-completed"
    )
    async def handle_payment_completed(request: Request) -> dict[str, str]:
        body: dict[str, Any] = await request.json()
        event = PaymentCompletedEvent.model_validate(body["data"])
        service: NotificationService = request.app.state.notification_service
        await service.handle_payment_completed(event)
        return {"status": "SUCCESS"}

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # Catches infra failures deliberately never caught inside
        # NotificationService (NotificationRepositoryError, NotificationChannelError) -
        # logs with correlation_id when available, returns a structured 500.
        # Dapr still sees a non-2xx and retries later.
        correlation_id = request.headers.get("X-Correlation-Id", "unknown")
        logging.getLogger(__name__).exception(
            "unhandled_exception", extra={"correlation_id": correlation_id}
        )
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "correlation_id": correlation_id},
        )

    return app


app = create_app()  # module-level singleton for `uvicorn services.notification.app:app`
