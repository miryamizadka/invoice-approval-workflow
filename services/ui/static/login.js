(function () {
  "use strict";

  const TOKEN_KEY = "approvalflow_token";
  const ROLE_KEY = "approvalflow_role";
  const EMAIL_KEY = "approvalflow_email";

  // The already-logged-in redirect lives in login.html's own inline <head>
  // script (runs before <body> is parsed, avoiding a flash of this form) -
  // not duplicated here.

  const loginForm = document.getElementById("login-form");
  const loginButton = document.getElementById("login-button");
  const loginMessage = document.getElementById("login-message");
  const registerForm = document.getElementById("register-form");
  const registerButton = document.getElementById("register-button");

  function showError(message) {
    loginMessage.textContent = message;
    loginMessage.className = "message error";
    loginMessage.hidden = false;
  }

  function showSuccess(message) {
    loginMessage.textContent = message;
    loginMessage.className = "message success";
    loginMessage.hidden = false;
  }

  function roleFromToken(token) {
    // Payload only, never signature-verified client-side - the server is
    // the only place a JWT is actually checked (shared/auth.py). This is
    // purely for display (nav bar / pre-filled submitter field).
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.role;
  }

  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    loginButton.disabled = true;
    loginButton.textContent = "Logging in...";
    loginMessage.hidden = true;
    try {
      const email = document.getElementById("email").value;
      const password = document.getElementById("password").value;
      const response = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!response.ok) {
        throw new Error("Invalid email or password.");
      }
      const body = await response.json();
      localStorage.setItem(TOKEN_KEY, body.access_token);
      localStorage.setItem(ROLE_KEY, roleFromToken(body.access_token));
      localStorage.setItem(EMAIL_KEY, email);
      window.location.replace("index.html");
    } catch (err) {
      showError(err.message);
      loginButton.disabled = false;
      loginButton.textContent = "Log In";
    }
  });

  registerForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    registerButton.disabled = true;
    registerButton.textContent = "Registering...";
    loginMessage.hidden = true;
    try {
      const email = document.getElementById("register-email").value;
      const password = document.getElementById("register-password").value;
      const response = await fetch("/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (response.status === 409) {
        throw new Error("An account with that email already exists.");
      }
      if (!response.ok) {
        throw new Error("Registration failed.");
      }
      showSuccess("Registered - you can now log in as a Submitter above.");
      registerForm.reset();
    } catch (err) {
      showError(err.message);
    } finally {
      registerButton.disabled = false;
      registerButton.textContent = "Register";
    }
  });
})();
