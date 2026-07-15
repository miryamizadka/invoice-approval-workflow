// Shared fetch helpers for both pages - loaded before submitter.js/approver.js
// via plain <script> tags (no build step, no modules), so these are
// deliberately plain global functions/classes, not an IIFE.

// N1: token/role/email persisted in localStorage after login.html - plain
// localStorage, not an HttpOnly cookie (documented tradeoff in README: an
// XSS bug on this page could exfiltrate the token; acceptable given this
// project's scope, no third-party scripts are ever loaded here).
const AUTH_TOKEN_KEY = "approvalflow_token";
const AUTH_ROLE_KEY = "approvalflow_role";
const AUTH_EMAIL_KEY = "approvalflow_email";

function getToken() {
  return localStorage.getItem(AUTH_TOKEN_KEY);
}

function getRole() {
  return localStorage.getItem(AUTH_ROLE_KEY);
}

function getEmail() {
  return localStorage.getItem(AUTH_EMAIL_KEY);
}

function storeSession(token, role, email) {
  localStorage.setItem(AUTH_TOKEN_KEY, token);
  localStorage.setItem(AUTH_ROLE_KEY, role);
  localStorage.setItem(AUTH_EMAIL_KEY, email);
}

function clearSession() {
  localStorage.removeItem(AUTH_TOKEN_KEY);
  localStorage.removeItem(AUTH_ROLE_KEY);
  localStorage.removeItem(AUTH_EMAIL_KEY);
}

function authHeaders() {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// The redirect-if-no-token check and the role-gated nav hiding both live in
// each page's own inline <head> script now (runs before <body>/<nav> is
// even parsed - see index.html) instead of here, so neither this page's
// content nor a nav tab this role shouldn't see can flash on screen even
// for a moment before this bottom-of-body script gets a chance to run.

function renderUserBar() {
  const nav = document.querySelector("nav");
  if (!nav) {
    return;
  }
  const bar = document.createElement("span");
  bar.className = "user-bar";
  bar.textContent = `${getEmail()} (${getRole()}) `;
  const logout = document.createElement("a");
  logout.href = "#";
  logout.textContent = "Logout";
  logout.addEventListener("click", (event) => {
    event.preventDefault();
    clearSession();
    window.location.replace("login.html");
  });
  bar.appendChild(logout);
  nav.appendChild(bar);
}

if (getToken()) {
  renderUserBar();
}

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function handleResponse(response) {
  if (response.status === 401) {
    // Token missing/expired/invalid - the server is the only place that
    // ever makes this call (shared/auth.py); nothing left to do here but
    // send the user back to log in again.
    clearSession();
    window.location.replace("login.html");
    throw new ApiError("Session expired - please log in again.", 401);
  }
  if (response.status === 403) {
    throw new ApiError("You don't have permission to do that.", 403);
  }
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
    response = await fetch(path, { method: "GET", headers: authHeaders() });
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
      headers: {
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...authHeaders(),
      },
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
