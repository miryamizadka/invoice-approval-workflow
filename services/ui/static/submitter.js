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
    statusMessage.textContent = "Checking status...";
    pollDeadline = Date.now() + POLL_TIMEOUT_MS;
    stopPolling();
    pollTimer = setInterval(() => checkStatus(trackingId), POLL_INTERVAL_MS);
    checkStatus(trackingId); // check immediately, don't wait for the first interval
  }

  async function checkStatus(trackingId) {
    if (Date.now() > pollDeadline) {
      stopPolling();
      statusMessage.textContent = "Still processing - check back later.";
      return;
    }
    try {
      const submission = await apiGet(`/invoices/${trackingId}`);
      renderSubmissionStatus(submission);
      if (submission.status === "completed" || submission.status === "failed") {
        stopPolling();
      }
      // Intake's own status has no visibility into Approval's internal
      // state machine - check the approval queue separately for waiting_info.
      await checkWaitingInfo(trackingId);
    } catch (err) {
      statusMessage.textContent = err.message;
    }
  }

  function renderSubmissionStatus(submission) {
    if (submission.status === "completed" && submission.decision) {
      statusMessage.textContent =
        `Status: completed - ${submission.decision.route} (${submission.decision.reason})`;
    } else if (submission.status === "failed") {
      statusMessage.textContent = `Status: failed - ${submission.reason || "internal error"}`;
    } else {
      statusMessage.textContent = `Status: ${submission.status}...`;
    }
  }

  async function checkWaitingInfo(trackingId) {
    try {
      const approval = await apiGet(`/approvals/${trackingId}`);
      if (approval.status === "waiting_info") {
        waitingInfoSection.hidden = false;
        waitingInfoReason.textContent = approval.decision.reason;
      } else {
        waitingInfoSection.hidden = true;
      }
    } catch (err) {
      // 404 is expected here - not every invoice is ever escalated
      waitingInfoSection.hidden = true;
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
      statusMessage.textContent =
        "Additional information sent - waiting for the approver to review it again.";
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
