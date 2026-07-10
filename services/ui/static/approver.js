(function () {
  "use strict";

  const POLL_INTERVAL_MS = 5000;
  const ACTIONABLE_STATUSES = new Set(["pending", "waiting_info"]);

  const queueList = document.getElementById("queue-list");
  const queueMessage = document.getElementById("queue-message");

  async function loadQueue() {
    try {
      const approvals = await apiGet("/approvals");
      renderQueue(approvals);
    } catch (err) {
      showError(queueMessage, err.message);
    }
  }

  function renderQueue(approvals) {
    queueList.innerHTML = "";
    if (approvals.length === 0) {
      queueList.textContent = "No pending approvals.";
      return;
    }
    for (const approval of approvals) {
      queueList.appendChild(renderCard(approval));
    }
  }

  function renderCard(approval) {
    const card = document.createElement("article");
    card.className = "approval-card";

    const title = document.createElement("h2");
    title.textContent = `${approval.invoice.vendor} - ${approval.invoice.currency} ${approval.invoice.total}`;
    card.appendChild(title);

    const meta = document.createElement("p");
    meta.textContent = `Tracking ID: ${approval.tracking_id} | Status: ${approval.status}`;
    card.appendChild(meta);

    const reason = document.createElement("p");
    reason.textContent = `Decision reason: ${approval.decision.reason}`;
    card.appendChild(reason);

    if (approval.recommendation) {
      const rec = document.createElement("p");
      rec.textContent =
        `Agent recommendation: ${approval.recommendation.recommendation} ` +
        `(confidence ${approval.recommendation.confidence}) - ${approval.recommendation.reasoning}`;
      card.appendChild(rec);
    }

    if (approval.additional_info) {
      const info = document.createElement("p");
      info.className = "additional-info";
      info.textContent = `Submitter response: ${approval.additional_info}`;
      card.appendChild(info);
    }

    if (ACTIONABLE_STATUSES.has(approval.status)) {
      card.appendChild(renderActionButton(approval.tracking_id, "Approve", "approve"));
      card.appendChild(renderActionButton(approval.tracking_id, "Reject", "reject"));
      card.appendChild(renderActionButton(approval.tracking_id, "Request Info", "request-info"));
    }

    return card;
  }

  function renderActionButton(trackingId, label, action) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", async () => {
      button.disabled = true;
      const originalText = button.textContent;
      button.textContent = "Processing...";
      try {
        await apiPost(`/approvals/${trackingId}/${action}`);
        clearMessage(queueMessage);
        await loadQueue();
      } catch (err) {
        showError(queueMessage, err.message);
        button.disabled = false;
        button.textContent = originalText;
      }
    });
    return button;
  }

  loadQueue();
  setInterval(loadQueue, POLL_INTERVAL_MS);
})();
