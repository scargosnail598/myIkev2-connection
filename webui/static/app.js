const services = document.querySelector("#services");
const refreshButton = document.querySelector("#refresh");
const installationState = document.querySelector("#installation-state");
const installationCopy = document.querySelector("#installation-copy");
const lastUpdated = document.querySelector("#last-updated");
const appShell = document.querySelector("#app-shell");
const loginView = document.querySelector("#login-view");
const loginForm = document.querySelector("#login-form");
const loginError = document.querySelector("#login-error");
const logoutButton = document.querySelector("#logout");
const sessionUser = document.querySelector("#session-user");
const modeBadge = document.querySelector("#mode-badge");
const toast = document.querySelector("#toast");
const brokerNotice = document.querySelector("#broker-notice");
let csrfToken = null;
let toastTimer = null;

function showLogin(message = "") {
  csrfToken = null;
  appShell.hidden = true;
  loginView.hidden = false;
  loginError.textContent = message;
  loginError.hidden = !message;
  document.querySelector("#username").focus();
}

function showApp(session) {
  csrfToken = session.csrf_token;
  sessionUser.textContent = session.username;
  loginView.hidden = true;
  appShell.hidden = false;
}

function showToast(message, isError = false) {
  window.clearTimeout(toastTimer);
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.hidden = false;
  toastTimer = window.setTimeout(() => { toast.hidden = true; }, 5000);
}

function renderServices(items, canControl) {
  services.replaceChildren(...items.map((service) => {
    const card = document.createElement("article");
    card.className = "service-card";

    const top = document.createElement("div");
    top.className = "service-top";
    const title = document.createElement("h3");
    title.textContent = service.label;
    const status = document.createElement("span");
    status.className = `status ${service.state}`;
    status.textContent = service.state;
    top.append(title, status);

    const unit = document.createElement("code");
    unit.textContent = service.unit;
    card.append(top, unit);
    if (["ikev2", "proxy"].includes(service.id)) {
      const actions = document.createElement("div");
      actions.className = "service-actions";
      ["start", "stop", "restart"].forEach((operation) => {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.target = service.id;
        button.dataset.operation = operation;
        button.textContent = operation[0].toUpperCase() + operation.slice(1);
        button.disabled = !canControl;
        if (!canControl) button.title = "Privileged broker is offline";
        actions.append(button);
      });
      card.append(actions);
    }
    return card;
  }));
}

async function controlService(target, operation) {
  const disruptive = ["stop", "restart"].includes(operation);
  const warning = disruptive ? " Active VPN sessions may be disconnected." : "";
  if (!window.confirm(`${operation.toUpperCase()} ${target}?${warning}`)) return;
  services.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try {
    const response = await fetch(`/api/services/${target}/${operation}`, {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken, Accept: "application/json" },
    });
    const data = await response.json();
    if (response.status === 401) {
      showLogin("Your session has expired. Sign in again.");
      return;
    }
    if (!response.ok) throw new Error(data.message || "Service action failed.");
    showToast(`${target} ${operation} completed.`);
    await loadDashboard();
  } catch (error) {
    showToast(error.message || "Service action failed.", true);
    await loadDashboard();
  }
}

async function loadDashboard() {
  refreshButton.disabled = true;
  try {
    const response = await fetch("/api/dashboard", { headers: { Accept: "application/json" } });
    if (response.status === 401) {
      showLogin("Your session has expired. Sign in again.");
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    installationState.textContent = data.installed ? "Detected" : "Not detected";
    installationCopy.textContent = data.installed
      ? "The managed installer state is present on this server."
      : "No managed installer state was found. The dashboard remains safely read-only.";
    const canControl = data.capabilities.service_actions;
    brokerNotice.hidden = canControl;
    modeBadge.classList.toggle("controlled", canControl);
    modeBadge.querySelector("b").textContent = canControl ? "CONTROL ENABLED" : "READ-ONLY MODE";
    renderServices(data.services, canControl);
    lastUpdated.textContent = `UPDATED ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
  } catch (error) {
    installationState.textContent = "Unavailable";
    installationCopy.textContent = "The local status API could not be reached.";
    renderServices([], false);
    lastUpdated.textContent = "STATUS UPDATE FAILED";
  } finally {
    refreshButton.disabled = false;
  }
}

refreshButton.addEventListener("click", loadDashboard);
services.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-operation]");
  if (!button) return;
  controlService(button.dataset.target, button.dataset.operation);
});

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = loginForm.querySelector("button[type=submit]");
  submit.disabled = true;
  loginError.hidden = true;
  try {
    const form = new FormData(loginForm);
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ username: form.get("username"), password: form.get("password") }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "Sign in failed.");
    loginForm.reset();
    showApp(data);
    await loadDashboard();
  } catch (error) {
    showLogin(error.message || "Sign in failed.");
  } finally {
    submit.disabled = false;
  }
});

logoutButton.addEventListener("click", async () => {
  try {
    await fetch("/api/auth/logout", {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken, Accept: "application/json" },
    });
  } finally {
    showLogin();
  }
});

async function initialize() {
  try {
    const response = await fetch("/api/session", { headers: { Accept: "application/json" } });
    const session = await response.json();
    if (!session.authenticated) {
      showLogin();
      return;
    }
    showApp(session);
    await loadDashboard();
  } catch (_error) {
    showLogin("The local Web UI is unavailable.");
  }
}

initialize();
