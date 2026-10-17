const $ = (selector) => document.querySelector(selector);
const services = $("#services");
const refreshButton = $("#refresh");
const appShell = $("#app-shell");
const loginView = $("#login-view");
const loginForm = $("#login-form");
const logoutButton = $("#logout");
const connectionView = $("#connection-view");
const connectionRetry = $("#connection-retry");
const actionDialog = $("#action-dialog");
const stateLabels = { active: "Running", inactive: "Stopped", failed: "Failed", activating: "Starting", deactivating: "Stopping", unknown: "Unknown" };
const operationLabels = { start: "Start", stop: "Stop", restart: "Restart" };
let csrfToken = null;
let sessionVersion = 0;
let toastTimer = null;
let dashboard = null;
let dashboardFresh = false;
let dashboardLoading = false;
let pendingAction = null;
let confirmation = null;
let logoutPending = false;

async function requestJson(path, options = {}, timeout = 15000) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, {
      ...options, signal: controller.signal, credentials: "same-origin", cache: "no-store",
      headers: { Accept: "application/json", ...options.headers },
    });
    let data;
    try { data = await response.json(); } catch (_error) { data = null; }
    if (!response.ok || !data || typeof data !== "object") {
      let message = data?.message || "The server could not complete this request. Try again.";
      if (response.status === 429) {
        const seconds = Number(response.headers.get("Retry-After"));
        message = Number.isFinite(seconds) && seconds > 0
          ? `Too many failed attempts. Try again in ${Math.ceil(seconds)} seconds.`
          : "Too many failed attempts. Wait a few minutes and try again.";
      }
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("The request timed out. Check your connection and try again.");
    if (error instanceof TypeError) throw new Error("Cannot reach the server. Check your connection and try again.");
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function finishConfirmation(accepted) {
  const resolve = confirmation;
  confirmation = null;
  syncControls();
  if (actionDialog.open) actionDialog.close();
  if (resolve) resolve(accepted);
}

function showLogin(message = "") {
  sessionVersion += 1;
  csrfToken = null;
  dashboard = null;
  dashboardFresh = false;
  dashboardLoading = false;
  pendingAction = null;
  logoutPending = false;
  finishConfirmation(false);
  appShell.hidden = true;
  connectionView.hidden = true;
  loginView.hidden = false;
  $("#toast").hidden = true;
  $("#login-error").textContent = message;
  $("#login-error").hidden = !message;
  $("#password").value = "";
  $("#username").focus();
}

function showApp(session) {
  sessionVersion += 1;
  csrfToken = session.csrf_token;
  $("#session-user").textContent = session.username;
  dashboard = null;
  dashboardFresh = false;
  dashboardLoading = false;
  logoutPending = false;
  loginView.hidden = true;
  connectionView.hidden = true;
  appShell.hidden = false;
  $("#status-notice").hidden = true;
  $("#broker-notice").hidden = true;
  services.replaceChildren();
  $("#installation-state").textContent = "Checking…";
  $("#installation-copy").textContent = "Loading the latest service status.";
  $("#running-services").textContent = "—";
  $("#attention-services").textContent = "—";
  $("#attention-card").classList.remove("needs-attention");
  $("#services-summary").textContent = "Waiting for status";
  $("#control-state").textContent = "Waiting for status";
  $("#last-updated").textContent = "Awaiting first update";
  setMode("CHECKING STATUS", false);
  syncControls();
}

function showToast(message, isError = false) {
  window.clearTimeout(toastTimer);
  $("#toast").textContent = message;
  $("#toast").classList.toggle("error", isError);
  $("#toast").hidden = false;
  toastTimer = window.setTimeout(() => { $("#toast").hidden = true; }, isError ? 10000 : 5000);
}

function setMode(label, controlled) {
  $("#mode-badge").classList.toggle("controlled", controlled);
  $("#mode-badge b").textContent = label;
}

function syncControls() {
  const busy = Boolean(pendingAction || confirmation || logoutPending);
  const canControl = Boolean(csrfToken && dashboardFresh && dashboard?.capabilities.service_actions);
  refreshButton.disabled = !csrfToken || dashboardLoading || busy;
  refreshButton.textContent = dashboardLoading ? "Refreshing…" : "Refresh status ↻";
  logoutButton.disabled = !csrfToken || busy;
  services.setAttribute("aria-busy", String(dashboardLoading || Boolean(pendingAction)));
  services.querySelectorAll("button[data-operation]").forEach((button) => {
    const service = dashboard?.services.find((item) => item.id === button.dataset.target);
    const transitioning = ["activating", "deactivating"].includes(service?.state);
    button.disabled = !canControl || busy || dashboardLoading || transitioning;
    button.title = !dashboardFresh ? "Refresh status before sending a command."
      : !canControl ? "Service controls are unavailable."
        : transitioning ? "Wait for this service to finish changing state." : "";
  });
  $("#action-progress").hidden = !pendingAction;
  if (pendingAction) $("#action-progress").textContent = `${operationLabels[pendingAction.operation]} request for ${pendingAction.label} is running. Please wait…`;
}

function renderServices(items) {
  services.replaceChildren(...items.map((service) => {
    const card = document.createElement("article");
    card.className = "service-card";
    const top = document.createElement("div");
    top.className = "service-top";
    const title = document.createElement("h3");
    title.textContent = service.label;
    const status = document.createElement("span");
    const state = Object.hasOwn(stateLabels, service.state) ? service.state : "unknown";
    status.className = `status ${state}`;
    status.textContent = stateLabels[state];
    top.append(title, status);
    const unit = document.createElement("code");
    unit.textContent = service.unit;
    card.append(top, unit);
    if (["ikev2", "proxy"].includes(service.id)) {
      const actions = document.createElement("div");
      actions.className = "service-actions";
      Object.entries(operationLabels).forEach(([operation, label]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.target = service.id;
        button.dataset.operation = operation;
        button.textContent = label;
        button.setAttribute("aria-label", `${label} ${service.label}`);
        actions.append(button);
      });
      card.append(actions);
    }
    return card;
  }));
}

function confirmAction(service, operation) {
  const disruptive = operation !== "start";
  $("#action-title").textContent = `${operationLabels[operation]} ${service.label}?`;
  $("#action-copy").textContent = disruptive
    ? service.id === "ikev2"
      ? "Active VPN connections may be disconnected. Continue only when you are ready to interrupt them."
      : "Clients using the SOCKS5 proxy may lose their proxy connections."
    : `This will start ${service.label} on the server.`;
  $("#action-confirm").textContent = `${operationLabels[operation]} service`;
  $("#action-confirm").classList.toggle("destructive", disruptive);
  return new Promise((resolve) => {
    confirmation = resolve;
    syncControls();
    actionDialog.showModal();
  });
}

async function controlService(target, operation) {
  if (!csrfToken || !dashboardFresh || !dashboard?.capabilities.service_actions
      || pendingAction || confirmation || dashboardLoading || logoutPending) return;
  const service = dashboard.services.find((item) => item.id === target);
  if (!service || !["ikev2", "proxy"].includes(target) || !Object.hasOwn(operationLabels, operation)
      || ["activating", "deactivating"].includes(service.state)) return;
  const version = sessionVersion;
  if (!await confirmAction(service, operation) || version !== sessionVersion) return;
  pendingAction = { label: service.label, operation };
  syncControls();
  try {
    const result = await requestJson(`/api/services/${target}/${operation}`, {
      method: "POST", headers: { "X-CSRF-Token": csrfToken },
    }, 75000);
    if (version !== sessionVersion) return;
    if (result.ok !== true) throw new Error("The server did not confirm completion.");
    showToast(`${service.label}: ${operationLabels[operation].toLowerCase()} completed.`);
  } catch (error) {
    if (version !== sessionVersion) return;
    if (error.status === 401) {
      showLogin("Your session has expired. Sign in again.");
      return;
    }
    showToast(`The command was not confirmed. ${error.message} Check service status before trying again.`, true);
  } finally {
    if (version === sessionVersion) {
      pendingAction = null;
      syncControls();
      await loadDashboard();
    }
  }
}

async function loadDashboard() {
  if (!csrfToken || dashboardLoading || pendingAction || confirmation || logoutPending) return;
  const version = sessionVersion;
  dashboardLoading = true;
  syncControls();
  try {
    const data = await requestJson("/api/dashboard");
    if (version !== sessionVersion) return;
    if (!Array.isArray(data.services) || typeof data.capabilities?.service_actions !== "boolean"
        || !data.services.every((item) => item && ["id", "label", "unit", "state"].every((key) => typeof item[key] === "string"))) {
      throw new Error("The server returned an unexpected status response.");
    }
    dashboard = data;
    dashboardFresh = true;
    $("#status-notice").hidden = true;
    $("#installation-state").textContent = data.installed === null ? "Unknown" : data.installed ? "Detected" : "Not detected";
    $("#installation-copy").textContent = data.installed === null
      ? "Installation status is unavailable. Service status is shown below."
      : data.installed ? "Managed VPN configuration is present on this server."
        : "No managed VPN configuration was found on this server.";
    const canControl = data.capabilities.service_actions;
    $("#broker-notice").hidden = canControl;
    setMode(canControl ? "CONTROLS AVAILABLE" : "VIEW ONLY", canControl);
    const active = data.services.filter((item) => item.state === "active").length;
    const attention = data.services.filter((item) => ["failed", "unknown"].includes(item.state)).length;
    $("#running-services").textContent = `${active} / ${data.services.length}`;
    $("#attention-services").textContent = String(attention);
    $("#attention-card").classList.toggle("needs-attention", attention > 0);
    $("#services-summary").textContent = `${data.services.length} monitored services`;
    $("#control-state").textContent = attention ? "Review service status below" : "No failed or unknown services";
    renderServices(data.services);
    const now = new Date();
    $("#last-updated").textContent = `Updated ${now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    $("#last-updated").title = now.toLocaleString();
  } catch (error) {
    if (version !== sessionVersion) return;
    if (error.status === 401) {
      showLogin("Your session has expired. Sign in again.");
      return;
    }
    dashboardFresh = false;
    $("#status-notice").hidden = false;
    $("#status-error").textContent = `${error.message} ${dashboard
      ? "The last received status is shown below. Controls are paused until status is refreshed."
      : "Use Refresh status to try again."}`;
    if (!dashboard) {
      $("#installation-state").textContent = "Unavailable";
      $("#installation-copy").textContent = "Service status could not be loaded.";
      $("#control-state").textContent = "Status unavailable";
    }
    setMode(dashboard ? "STATUS OUT OF DATE" : "STATUS UNAVAILABLE", false);
  } finally {
    if (version === sessionVersion) {
      dashboardLoading = false;
      syncControls();
    }
  }
}

refreshButton.addEventListener("click", loadDashboard);
services.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-operation]");
  if (!button || button.disabled) return;
  controlService(button.dataset.target, button.dataset.operation);
});
$("#action-cancel").addEventListener("click", () => finishConfirmation(false));
$("#action-confirm").addEventListener("click", () => finishConfirmation(true));
actionDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  finishConfirmation(false);
});

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = loginForm.querySelector("button[type=submit]");
  if (submit.disabled) return;
  submit.disabled = true;
  $("#login-error").hidden = true;
  try {
    const form = new FormData(loginForm);
    const data = await requestJson("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: form.get("username"), password: form.get("password") }),
    });
    if (!data.authenticated || !data.csrf_token) throw new Error("Sign in could not be confirmed. Try again.");
    loginForm.reset();
    showApp(data);
    await loadDashboard();
  } catch (error) {
    showLogin(error.message);
  } finally {
    submit.disabled = false;
  }
});

logoutButton.addEventListener("click", async () => {
  if (!csrfToken || logoutPending || pendingAction || confirmation) return;
  const version = sessionVersion;
  logoutPending = true;
  syncControls();
  try {
    const result = await requestJson("/api/auth/logout", {
      method: "POST", headers: { "X-CSRF-Token": csrfToken },
    });
    if (result.authenticated !== false) throw new Error("The server did not confirm sign out.");
    if (version === sessionVersion) showLogin();
  } catch (error) {
    if (version !== sessionVersion) return;
    if (error.status === 401) showLogin();
    else showToast(`Sign out was not confirmed. ${error.message}`, true);
  } finally {
    if (version === sessionVersion) {
      logoutPending = false;
      syncControls();
    }
  }
});

async function initialize() {
  connectionRetry.disabled = true;
  connectionRetry.hidden = true;
  $("#connection-title").textContent = "Connecting…";
  $("#connection-copy").textContent = "Checking your session.";
  try {
    const session = await requestJson("/api/session");
    if (session.authenticated === false) {
      showLogin();
      return;
    }
    if (!session.authenticated || !session.csrf_token) throw new Error("The server returned an unexpected session response.");
    showApp(session);
    await loadDashboard();
  } catch (error) {
    $("#connection-title").textContent = "Connection unavailable";
    $("#connection-copy").textContent = error.message;
    connectionRetry.hidden = false;
  } finally {
    connectionRetry.disabled = false;
  }
}

connectionRetry.addEventListener("click", initialize);
window.setInterval(() => {
  if (document.visibilityState === "visible" && !services.contains(document.activeElement)) loadDashboard();
}, 30000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") loadDashboard();
});

initialize();
