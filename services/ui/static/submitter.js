(function () {
  "use strict";

  const POLL_INTERVAL_MS = 2000;
  const POLL_TIMEOUT_MS = 5 * 60 * 1000; // safety net - never poll forever

  const form = document.getElementById("submit-form");
  const submitButton = document.getElementById("submit-button");
  const submitMessage = document.getElementById("submit-message");
  const statusSection = document.getElementById("status-section");
  const trackingIdDisplay = document.getElementById("tracking-id-display");
  const statusMessage = document.getElementById("status-message");
  const waitingInfoSection = document.getElementById("waiting-info-section");
  const waitingInfoReason = document.getElementById("waiting-info-reason");
  const additionalInfoText = document.getElementById("additional-info-text");
  const submitAdditionalInfoButton = document.getElementById("submit-additional-info");
  const lookupInput = document.getElementById("lookup-tracking-id");
  const lookupButton = document.getElementById("lookup-button");

  let pollTimer = null;
  let pollDeadline = null;
  let currentTrackingId = null;

  const STATUS_CLASSES = ["status-approved", "status-rejected", "status-waiting", "status-pending"];

  function setStatusMessage(text, statusClass) {
    statusMessage.textContent = text;
    statusMessage.classList.remove(...STATUS_CLASSES);
    if (statusClass) {
      statusMessage.classList.add(statusClass);
    }
  }

  function stopPolling() {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function buildInvoicePayload() {
    const data = new FormData(form);
    return {
      id: data.get("id"),
      submitter: data.get("submitter"),
      department: data.get("department"),
      vendor: data.get("vendor"),
      vendorKnown: document.getElementById("vendorKnown").checked,
      invoiceNumber: data.get("invoiceNumber"),
      currency: data.get("currency"),
      category: data.get("category"),
      attendees: data.get("attendees") ? Number(data.get("attendees")) : null,
      lineItems: [
        {
          description: data.get("lineItemDescription"),
          quantity: data.get("lineItemQuantity"),
          unitPrice: data.get("lineItemUnitPrice"),
        },
      ],
      taxAmount: data.get("taxAmount"),
      total: data.get("total"),
      receiptPresent: document.getElementById("receiptPresent").checked,
      date: data.get("date"),
      notes: data.get("notes") || null,
    };
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    stopPolling(); // a new submission always stops any prior polling
    submitButton.disabled = true;
    submitButton.textContent = "Submitting...";
    clearMessage(submitMessage);
    try {
      const payload = buildInvoicePayload();
      const result = await apiPost("/invoices", payload);
      showSuccess(submitMessage, `Submitted. Tracking ID: ${result.tracking_id}`);
      startTracking(result.tracking_id);
    } catch (err) {
      showError(submitMessage, err.message);
    } finally {
      submitButton.disabled = false;
      submitButton.textContent = "Submit Invoice";
    }
  });

  function startTracking(trackingId) {
    currentTrackingId = trackingId;
    statusSection.hidden = false;
    trackingIdDisplay.textContent = trackingId;
    waitingInfoSection.hidden = true;
    setStatusMessage("Checking status...", "status-pending");
    pollDeadline = Date.now() + POLL_TIMEOUT_MS;
    stopPolling();
    pollTimer = setInterval(() => checkStatus(trackingId), POLL_INTERVAL_MS);
    checkStatus(trackingId); // check immediately, don't wait for the first interval
  }

  async function checkStatus(trackingId) {
    if (Date.now() > pollDeadline) {
      stopPolling();
      setStatusMessage("Still processing - check back later.", "status-pending");
      return;
    }
    try {
      const submission = await apiGet(`/invoices/${trackingId}`);
      // Intake's own status/decision is frozen at whatever Decision
      // originally output - it never learns the eventual approval outcome
      // (Intake doesn't subscribe to approval.completed). Only escalated
      // (human_review) items have an Approval record at all - checking
      // /approvals/{id} for every other route would just 404 every poll
      // for no reason, so it's only called when we know it's relevant.
      const isEscalated = submission.decision && submission.decision.route === "human_review";
      let approval = null;
      if (isEscalated) {
        approval = await checkApprovalStatus(trackingId);
      } else {
        waitingInfoSection.hidden = true;
      }
      renderSubmissionStatus(submission, approval);

      const escalationResolved =
        approval !== null && (approval.status === "approved" || approval.status === "rejected");
      if (submission.status === "failed" ||
          (submission.status === "completed" && (!isEscalated || escalationResolved))) {
        stopPolling();
      }
    } catch (err) {
      setStatusMessage(err.message, "status-rejected");
    }
  }

  function renderSubmissionStatus(submission, approval) {
    if (submission.status === "failed") {
      setStatusMessage(
        `Status: failed - ${submission.reason || "internal error"}`,
        "status-rejected"
      );
      return;
    }
    if (submission.status !== "completed" || !submission.decision) {
      setStatusMessage(`Status: ${submission.status}...`, "status-pending");
      return;
    }
    if (submission.decision.route === "human_review") {
      if (approval && approval.status === "approved") {
        setStatusMessage(
          `Status: approved by approver (${submission.decision.reason})`,
          "status-approved"
        );
      } else if (approval && approval.status === "rejected") {
        setStatusMessage(
          `Status: rejected by approver (${submission.decision.reason})`,
          "status-rejected"
        );
      } else if (approval && approval.status === "waiting_info") {
        setStatusMessage("Status: waiting for your response (see below)", "status-waiting");
      } else {
        setStatusMessage(
          `Status: pending human review - ${submission.decision.reason}`,
          "status-pending"
        );
      }
      return;
    }
    const isPositive = submission.decision.route === "auto_approve";
    setStatusMessage(
      `Status: completed - ${submission.decision.route} (${submission.decision.reason})`,
      isPositive ? "status-approved" : "status-rejected"
    );
  }

  async function checkApprovalStatus(trackingId) {
    try {
      const approval = await apiGet(`/approvals/${trackingId}`);
      if (approval.status === "waiting_info") {
        waitingInfoSection.hidden = false;
        waitingInfoReason.textContent = approval.decision.reason;
      } else {
        waitingInfoSection.hidden = true;
      }
      return approval;
    } catch (err) {
      // A 404 here is expected only very briefly (Intake and Approval both
      // subscribe to decision.completed independently - no ordering
      // guarantee between them) - resolves within one poll cycle once
      // Approval catches up.
      waitingInfoSection.hidden = true;
      return null;
    }
  }

  submitAdditionalInfoButton.addEventListener("click", async () => {
    if (!currentTrackingId) {
      return;
    }
    submitAdditionalInfoButton.disabled = true;
    submitAdditionalInfoButton.textContent = "Sending...";
    try {
      await apiPost(`/approvals/${currentTrackingId}/additional-info`, {
        info: additionalInfoText.value,
      });
      waitingInfoSection.hidden = true;
      additionalInfoText.value = "";
      setStatusMessage(
        "Additional information sent - waiting for the approver to review it again.",
        "status-pending"
      );
    } catch (err) {
      showError(submitMessage, err.message);
    } finally {
      submitAdditionalInfoButton.disabled = false;
      submitAdditionalInfoButton.textContent = "Send additional information";
    }
  });

  lookupButton.addEventListener("click", () => {
    const trackingId = lookupInput.value.trim();
    if (!trackingId) {
      showError(submitMessage, "Enter a tracking ID.");
      return;
    }
    clearMessage(submitMessage);
    startTracking(trackingId);
  });
})();
