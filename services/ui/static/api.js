// Shared fetch helpers for both pages - loaded before submitter.js/approver.js
// via plain <script> tags (no build step, no modules), so these are
// deliberately plain global functions/classes, not an IIFE.

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function handleResponse(response) {
  if (response.status === 404) {
    throw new ApiError("Tracking ID not found.", 404);
  }
  if (response.status === 409) {
    throw new ApiError("This action can no longer be performed (already resolved).", 409);
  }
  if (!response.ok) {
    throw new ApiError(`Something went wrong (status ${response.status}).`, response.status);
  }
  if (response.status === 204) {
    return null;
  }
  return response.json();
}

async function apiGet(path) {
  let response;
  try {
    response = await fetch(path, { method: "GET" });
  } catch (err) {
    throw new ApiError("Connection problem - please try again.", 0);
  }
  return handleResponse(response);
}

async function apiPost(path, body) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    throw new ApiError("Connection problem - please try again.", 0);
  }
  return handleResponse(response);
}

function showError(el, message) {
  el.textContent = message;
  el.className = "message error";
  el.hidden = false;
}

function showSuccess(el, message) {
  el.textContent = message;
  el.className = "message success";
  el.hidden = false;
}

function clearMessage(el) {
  el.textContent = "";
  el.hidden = true;
}
