"""Transport-agnostic Intake orchestration - no FastAPI/HTTP knowledge here.

BackgroundTasks (used by the caller, services/intake/app.py) is a temporary
stand-in for Dapr pub/sub, not a production queue: it provides no durability
across restarts, no automatic retry, and no scaling beyond one worker. If
the process crashes between submit() and process() running, the submission
stays stuck at RECEIVED forever with no automatic recovery. Accepted for
this phase; Dapr pub/sub (out of scope here) will provide real delivery
guarantees when it replaces this.
"""

from __future__ import annotations

import logging
import uuid

from services.intake.decision_client import DecisionClientError, DecisionServiceClient
from services.intake.models import Submission, SubmissionStatus
from services.intake.repository import InvoiceRepository
from shared.contracts.models import Invoice, compute_dedup_key


class IntakeService:
    def __init__(
        self, repository: InvoiceRepository, decision_client: DecisionServiceClient
    ) -> None:
        self._repository = repository
        self._decision_client = decision_client
        self._logger = logging.getLogger(__name__)

    async def submit(self, invoice: Invoice) -> str:
        """Synchronous half: dedup check + store as received. Returns tracking_id immediately."""
        tracking_id = str(uuid.uuid4())
        dedup_key = compute_dedup_key(invoice)
        existing = await self._repository.find_by_dedup_key(dedup_key)
        submission = Submission(
            tracking_id=tracking_id,
            invoice=invoice,
            dedup_key=dedup_key,
            is_duplicate=existing is not None,
            status=SubmissionStatus.RECEIVED,
        )
        await self._repository.save(submission)
        self._logger.info(
            "invoice_received",
            extra={"correlation_id": tracking_id, "is_duplicate": submission.is_duplicate},
        )
        return tracking_id

    async def process(self, tracking_id: str) -> None:
        """Background half: call Decision Service, update status. Never leaves a
        submission stuck at 'processing' silently - the same fail-clean contract
        already established for Decider/AgentError, applied at this boundary."""
        submission = await self._repository.get(tracking_id)
        assert submission is not None  # scheduled right after save(); must exist
        await self._repository.save(
            submission.model_copy(update={"status": SubmissionStatus.PROCESSING})
        )
        try:
            decision = await self._decision_client.decide(
                submission.invoice,
                correlation_id=tracking_id,
                is_duplicate=submission.is_duplicate,
            )
        except DecisionClientError as exc:
            self._logger.error(
                "processing_failed", extra={"correlation_id": tracking_id, "error": str(exc)}
            )
            await self._repository.save(
                submission.model_copy(update={"status": SubmissionStatus.FAILED, "error": str(exc)})
            )
            return
        await self._repository.save(
            submission.model_copy(
                update={"status": SubmissionStatus.COMPLETED, "decision": decision}
            )
        )
        self._logger.info(
            "processing_completed",
            extra={"correlation_id": tracking_id, "route": decision.route.value},
        )

    async def get_status(self, tracking_id: str) -> Submission | None:
        return await self._repository.get(tracking_id)


def build_intake_service(
    repository: InvoiceRepository, decision_client: DecisionServiceClient
) -> IntakeService:
    """Composition seam, same reason as build_decider: keeps FastAPI out of this."""
    return IntakeService(repository, decision_client)
