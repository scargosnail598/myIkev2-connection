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
let confirmationTrigger = null;
let logoutPending = false;
let metricsSnapshot = null;
let metricsLoading = false;
let diagnosticsSnapshot = null;
let diagnosticsLoading = false;
let sessionsSnapshot = null;
let sessionsLoading = false;
let usersSnapshot = null;
let usersFresh = false;
let usersLoading = false;
let userPending = false;
let usersGeneration = 0;
let userOperation = null;
let userRevision = null;
const userDialog = $("#user-dialog");
const shareDialog = $("#share-dialog");
const limitsDialog = $("#limits-dialog");
let limitOperation = null;
let limitUsername = null;
let limitAccountRevision = null;
let limitRevision = null;
let shareGeneration = 0;
let shareTimer = null;
let shareUsername = null;
let shareRevision = null;
let securityGeneration = 0;
let securityBusy = false;
let securityEnabled = null;
let securityTimer = null;

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
  if (confirmationTrigger?.isConnected && !confirmationTrigger.disabled) confirmationTrigger.focus();
  confirmationTrigger = null;
  if (resolve) resolve(accepted);
}

function showLogin(message = "") {
  sessionVersion += 1;
  csrfToken = null;
  resetSecurity();
  resetUsers();
  resetSessions();
  resetDiagnostics();
  resetMetrics();
  dashboard = null;
  dashboardFresh = false;
  dashboardLoading = false;
  pendingAction = null;
  logoutPending = false;
  finishConfirmation(false);
  appShell.hidden = true;
  connectionView.hidden = true;
  loginView.hidden = false;
  $(".skip-link").hidden = true;
  $("#toast").hidden = true;
  $("#login-error").textContent = message;
  $("#login-error").hidden = !message;
  $("#password").value = "";
  $("#login-code").value = "";
  $("#username").focus();
}

function showApp(session) {
  sessionVersion += 1;
  csrfToken = session.csrf_token;
  resetSecurity();
  resetUsers();
  resetSessions();
  resetDiagnostics();
  resetMetrics();
  $("#session-user").textContent = session.username;
  dashboard = null;
  dashboardFresh = false;
  dashboardLoading = false;
  logoutPending = false;
  loginView.hidden = true;
  connectionView.hidden = true;
  appShell.hidden = false;
  $(".skip-link").hidden = false;
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
  selectView();
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
  const busy = Boolean(pendingAction || confirmation || logoutPending || userPending || securityBusy || userDialog.open || shareDialog.open || limitsDialog.open);
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
  syncUserControls();
  syncSessionControls();
  syncDiagnosticsControls();
  syncMetricsControls();
  syncSecurityControls();
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
    const icon = document.createElement("span");
    icon.className = "service-icon";
    icon.setAttribute("aria-hidden", "true");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    const iconPaths = {
      ikev2: "M4 3h16v7H4z M4 14h16v7H4z M8 6.5h.01 M8 17.5h.01 M12 6.5h4 M12 17.5h4",
      firewall: "M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7z M8 12l3 3 5-6",
      proxy: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M3 12h18 M12 3c-5 5-5 13 0 18 M12 3c5 5 5 13 0 18",
    };
    path.setAttribute("d", iconPaths[service.id] || iconPaths.ikev2);
    svg.append(path);
    icon.append(svg);
    top.append(icon, status);
    const unit = document.createElement("code");
    unit.textContent = service.unit;
    const description = document.createElement("p");
    description.className = "service-description";
    description.textContent = {
      ikev2: "Encrypted connections for your VPN clients.",
      firewall: "Network access rules and VPN traffic forwarding.",
      proxy: "Private SOCKS5 access through the VPN tunnel.",
    }[service.id] || "Managed server service.";
    card.append(top, title, unit, description);
    if (service.id === "firewall") {
      const note = document.createElement("p");
      note.className = "service-note";
      note.textContent = "Starts with the VPN service";
      card.append(note);
    }
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
  confirmationTrigger = services.querySelector(`button[data-target="${service.id}"][data-operation="${operation}"]`);
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
  if (!csrfToken || dashboardLoading || pendingAction || confirmation || logoutPending || userPending || userDialog.open || shareDialog.open || limitsDialog.open) return;
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

$(".skip-link").addEventListener("click", (event) => {
  event.preventDefault();
  $("#main-content").focus();
  $("#main-content").scrollIntoView({ block: "start" });
});
refreshButton.addEventListener("click", () => { loadDashboard(); loadMetrics(); });
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
      body: JSON.stringify({ username: form.get("username"), password: form.get("password"), code: form.get("code") }),
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
  if (document.visibilityState === "visible") {
    if (["#diagnostics", "#security"].includes(location.hash)) return;
    if (location.hash === "#sessions") loadSessions();
    else if (location.hash === "#users") {
      if (!$("#users-list").contains(document.activeElement) && document.activeElement !== $("#users-search")) loadUsers();
    } else if (!services.contains(document.activeElement)) loadDashboard();
  }
}, 30000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    loadMetrics();
    if (["#diagnostics", "#security"].includes(location.hash)) return;
    if (location.hash === "#sessions") loadSessions();
    else if (location.hash === "#users") loadUsers();
    else loadDashboard();
  }
});

function resetUsers() {
  usersGeneration += 1;
  usersSnapshot = null;
  usersFresh = false;
  usersLoading = false;
  userPending = false;
  closeUserDialog();
  closeShareDialog();
  closeLimitsDialog();
  $("#limits-notice").hidden = true;
  $("#users-list").replaceChildren();
  $("#users-error").hidden = true;
  $("#users-empty").hidden = true;
  $("#users-search").value = "";
  $("#users-page").textContent = "";
  $("#users-summary").textContent = "Loading VPN accounts…";
  $("#users-updated").textContent = "Awaiting first update";
}

function selectView() {
  closeShareDialog();
  closeLimitsDialog();
  syncControls();
  const view = ["#users", "#sessions", "#diagnostics", "#security"].includes(location.hash) ? location.hash.slice(1) : "overview";
  $("#workspace-title").textContent = { overview: "Overview", users: "Users", sessions: "Sessions", diagnostics: "Diagnostics", security: "Security" }[view];
  $("#overview").hidden = view !== "overview";
  $("#users-view").hidden = view !== "users";
  $("#sessions-view").hidden = view !== "sessions";
  $("#diagnostics-view").hidden = view !== "diagnostics";
  $("#security-view").hidden = view !== "security";
  resetSecurity();
  document.querySelectorAll("[data-view]").forEach((link) => {
    const active = link.dataset.view === view;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (!csrfToken) return;
  if (view === "users") loadUsers();
  else if (view === "sessions") loadSessions();
  else if (view === "diagnostics") loadDiagnostics();
  else if (view === "security") loadSecurity();
  else { loadDashboard(); loadMetrics(); }
}

function metricsVisible() {
  return Boolean(csrfToken && !$("#overview").hidden && document.visibilityState === "visible");
}

function resetMetrics() {
  metricsSnapshot = null;
  metricsLoading = false;
  ["#cpu-value", "#memory-value", "#network-rx", "#network-tx", "#network-interface", "#network-scale"].forEach((selector) => { $(selector).textContent = "—"; });
  $("#cpu-copy").textContent = "Waiting for the first sample";
  $("#memory-copy").textContent = "Waiting for memory information";
  $("#network-copy").textContent = "Waiting for interface counters";
  ["cpu", "memory", "network"].forEach((name) => {
    $("#" + name + "-chart").replaceChildren();
    $("#" + name + "-chart").setAttribute("aria-label", `${name === "cpu" ? "CPU" : name === "memory" ? "Memory" : "Network"} readings over the last five minutes. Awaiting samples.`);
    $("#" + name + "-chart-empty").hidden = false;
  });
  $("#metrics-error").hidden = true;
  $("#metrics-updated").textContent = "Awaiting first sample";
}

function syncMetricsControls() {
  $("#metrics-refresh").disabled = !csrfToken || metricsLoading || Boolean(pendingAction || confirmation || logoutPending || userPending || userDialog.open || shareDialog.open || limitsDialog.open);
  $("#metrics-refresh").textContent = metricsLoading ? "Refreshing…" : "Refresh metrics";
  $("#metrics-grid").setAttribute("aria-busy", String(metricsLoading));
}

function drawResourceChart(name, history, series, maximum) {
  const svg = $("#" + name + "-chart");
  svg.replaceChildren();
  const element = (tag, attributes) => {
    const item = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([key, value]) => item.setAttribute(key, String(value)));
    svg.append(item);
    return item;
  };
  [8, 54, 100].forEach((y) => element("path", { d: `M8 ${y}H392`, class: "chart-grid-line" }));
  let any = false;
  series.forEach(([key, className]) => {
    let path = "", last = null, latest = null;
    history.forEach((point) => {
      const value = point[key];
      if (value === null) { last = null; return; }
      const x = 8 + (1 - point.age_seconds / 300) * 384;
      const y = 100 - Math.min(value / maximum, 1) * 92;
      const contiguous = last !== null && last - point.age_seconds <= 15;
      path += `${contiguous ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)} `;
      last = point.age_seconds;
      latest = { x, y };
      any = true;
    });
    if (path) element("path", { d: path, class: "chart-line " + className });
    if (latest) element("circle", { cx: latest.x, cy: latest.y, r: 3, class: "chart-dot " + className });
  });
  $("#" + name + "-chart-empty").hidden = any;
}

function renderMetrics(data) {
  const percent = (value) => value === null ? "—" : `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;
  const rate = (value) => value === null ? "—" : `${formatBytes(value)}/s`;
  $("#cpu-value").textContent = percent(data.cpu.percent);
  $("#cpu-copy").textContent = data.cpu.cores === null ? "CPU information is unavailable" : data.cpu.percent === null
    ? "Collecting CPU samples…" : `Across ${data.cpu.cores} logical CPU${data.cpu.cores === 1 ? "" : "s"}`;
  $("#memory-value").textContent = percent(data.memory.percent);
  $("#memory-copy").textContent = data.memory.total_bytes === null ? "Memory information is unavailable"
    : `${formatBytes(data.memory.used_bytes)} used of ${formatBytes(data.memory.total_bytes)}`;
  const network = data.network;
  $("#network-interface").textContent = network.interface || "Unavailable";
  $("#network-rx").textContent = rate(network.rx_bytes_per_second);
  $("#network-tx").textContent = rate(network.tx_bytes_per_second);
  $("#network-copy").textContent = network.rx_bytes === null ? "Interface counters are unavailable"
    : `${formatBytes(network.rx_bytes)} received · ${formatBytes(network.tx_bytes)} sent since interface counters started`;
  const trafficHistory = data.history.map((point) => point.interface === network.interface ? point
    : { ...point, rx_bytes_per_second: null, tx_bytes_per_second: null });
  const peak = Math.max(1024, ...trafficHistory.flatMap((point) => [point.rx_bytes_per_second || 0, point.tx_bytes_per_second || 0]));
  const maximum = 2 ** Math.ceil(Math.log2(peak));
  $("#network-scale").textContent = rate(maximum);
  drawResourceChart("cpu", data.history, [["cpu_percent", "chart-cpu"]], 100);
  drawResourceChart("memory", data.history, [["memory_percent", "chart-memory"]], 100);
  drawResourceChart("network", trafficHistory, [["rx_bytes_per_second", "chart-rx"], ["tx_bytes_per_second", "chart-tx"]], maximum);
  ["cpu", "memory"].forEach((name) => $("#" + name + "-chart").setAttribute("aria-label", `${name === "cpu" ? "CPU" : "Memory"} usage: ${percent(data[name].percent)}. Last five minutes; gaps indicate missing samples.`));
  $("#network-chart").setAttribute("aria-label", `Traffic on ${network.interface || "unavailable interface"}: ${rate(network.rx_bytes_per_second)} received, ${rate(network.tx_bytes_per_second)} sent. Last five minutes.`);
  $("#metrics-updated").textContent = `Sampled ${new Date(data.sampled_at * 1000).toLocaleTimeString()}`;
  const incomplete = data.cpu.cores === null || data.memory.total_bytes === null || network.rx_bytes === null;
  $("#metrics-error").hidden = !incomplete;
  if (incomplete) $("#metrics-error").textContent = "Some server readings are unavailable. Available readings are shown below.";
}

async function loadMetrics() {
  if (!metricsVisible() || metricsLoading || pendingAction || confirmation || logoutPending || userPending || userDialog.open || shareDialog.open || limitsDialog.open) return;
  const version = sessionVersion;
  metricsLoading = true;
  syncMetricsControls();
  try {
    const data = await requestJson("/api/metrics");
    if (version !== sessionVersion || !metricsVisible()) return;
    const number = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
    const optional = (value) => value === null || number(value);
    const percentage = (value) => value === null || (number(value) && value <= 100);
    if (data.ok !== true || !number(data.sampled_at) || data.interval_seconds !== 5 || data.window_seconds !== 300
        || !data.cpu || !data.memory || !data.network || !percentage(data.cpu.percent) || !optional(data.cpu.cores)
        || !percentage(data.memory.percent) || !optional(data.memory.used_bytes) || !optional(data.memory.total_bytes)
        || (data.network.interface !== null && typeof data.network.interface !== "string")
        || !["rx_bytes", "tx_bytes", "rx_bytes_per_second", "tx_bytes_per_second"].every((key) => optional(data.network[key]))
        || !Array.isArray(data.history) || !data.history.length || data.history.length > 61
        || !data.history.every((point) => point && number(point.age_seconds) && point.age_seconds <= 300
          && percentage(point.cpu_percent) && percentage(point.memory_percent)
          && (point.interface === null || typeof point.interface === "string")
          && optional(point.rx_bytes_per_second) && optional(point.tx_bytes_per_second))) {
      throw new Error("The server returned an unexpected metrics response.");
    }
    metricsSnapshot = data;
    renderMetrics(data);
  } catch (error) {
    if (version !== sessionVersion || !metricsVisible()) return;
    if (error.status === 401) { showLogin("Your session has expired. Sign in again."); return; }
    $("#metrics-error").hidden = false;
    $("#metrics-error").textContent = `${error.message} ${metricsSnapshot ? "Last readings are shown and may be out of date." : "Choose Refresh metrics to try again."}`;
  } finally {
    if (version === sessionVersion) { metricsLoading = false; syncMetricsControls(); }
  }
}

$("#metrics-refresh").addEventListener("click", loadMetrics);
window.setInterval(loadMetrics, 5000);

const diagnosticLabels = { pass: "Passed", warn: "Warning", fail: "Failed", unknown: "Unknown" };

function resetDiagnostics() {
  diagnosticsSnapshot = null;
  diagnosticsLoading = false;
  $("#diagnostics-list").replaceChildren();
  $("#diagnostics-counts").hidden = true;
  $("#diagnostics-error").hidden = true;
  $("#diagnostics-progress").hidden = true;
  $("#diagnostics-summary").textContent = "Run checks to inspect server health.";
  $("#diagnostics-updated").textContent = "Awaiting first check";
}

function syncDiagnosticsControls() {
  $("#diagnostics-refresh").disabled = !csrfToken || diagnosticsLoading || Boolean(pendingAction || confirmation || logoutPending || userPending || userDialog.open || shareDialog.open || limitsDialog.open);
  $("#diagnostics-refresh").textContent = diagnosticsLoading ? "Checking…" : "Run checks";
  $("#diagnostics-progress").hidden = !diagnosticsLoading;
  $("#diagnostics-list").setAttribute("aria-busy", String(diagnosticsLoading));
}

async function loadDiagnostics() {
  if (!csrfToken || diagnosticsLoading || pendingAction || confirmation || logoutPending || userPending || userDialog.open || shareDialog.open || limitsDialog.open) return;
  const version = sessionVersion;
  diagnosticsLoading = true;
  syncDiagnosticsControls();
  try {
    const data = await requestJson("/api/diagnostics", {}, 35000);
    if (version !== sessionVersion) return;
    if (data.ok !== true || !Array.isArray(data.checks) || data.checks.length === 0 || data.checks.length > 20
        || typeof data.checked_at !== "string" || !Number.isFinite(Date.parse(data.checked_at))
        || !data.checks.every((item) => item && Object.hasOwn(diagnosticLabels, item.status)
          && ["id", "label", "detail", "guidance"].every((key) => typeof item[key] === "string"))) {
      throw new Error("The server returned an unexpected diagnostics response.");
    }
    diagnosticsSnapshot = data;
    $("#diagnostics-error").hidden = true;
    const counts = Object.fromEntries(Object.keys(diagnosticLabels).map((status) => [status, data.checks.filter((item) => item.status === status).length]));
    for (const status of Object.keys(counts)) $("#diagnostics-" + status).textContent = String(counts[status]);
    $("#diagnostics-counts").hidden = false;
    $("#diagnostics-summary").textContent = `${data.checks.length} checks completed · ${counts.warn + counts.fail + counts.unknown} to review`;
    $("#diagnostics-list").replaceChildren(...data.checks.map((item) => {
      const card = document.createElement("article");
      card.className = "diagnostic-check";
      const heading = document.createElement("div");
      heading.className = "diagnostic-heading";
      const title = document.createElement("h2");
      title.textContent = item.label;
      const status = document.createElement("span");
      status.className = `diagnostic-status ${item.status}`;
      status.textContent = diagnosticLabels[item.status];
      heading.append(title, status);
      const detail = document.createElement("p");
      detail.textContent = item.detail;
      card.append(heading, detail);
      if (item.guidance) {
        const guidance = document.createElement("p");
        guidance.className = "diagnostic-guidance";
        guidance.textContent = item.guidance;
        card.append(guidance);
      }
      return card;
    }));
    const checked = new Date(data.checked_at);
    $("#diagnostics-updated").textContent = `Checked ${checked.toLocaleTimeString()}`;
    $("#diagnostics-updated").title = checked.toLocaleString();
  } catch (error) {
    if (version !== sessionVersion) return;
    if (error.status === 401) { showLogin("Your session has expired. Sign in again."); return; }
    $("#diagnostics-error").hidden = false;
    $("#diagnostics-error").textContent = `${error.message} ${diagnosticsSnapshot ? "Previous results are shown and may be out of date." : "Choose Run checks to try again."}`;
    if (!diagnosticsSnapshot) $("#diagnostics-summary").textContent = "Server diagnostics unavailable";
  } finally {
    if (version === sessionVersion) { diagnosticsLoading = false; syncDiagnosticsControls(); }
  }
}

$("#diagnostics-refresh").addEventListener("click", loadDiagnostics);

function resetSessions() {
  sessionsSnapshot = null;
  sessionsLoading = false;
  $("#sessions-list").replaceChildren();
  $("#sessions-error").hidden = true;
  $("#sessions-empty").hidden = true;
  $("#sessions-search").value = "";
  $("#sessions-page").textContent = "";
  $("#sessions-summary").textContent = "Loading VPN sessions…";
  $("#sessions-updated").textContent = "Awaiting first update";
}

function syncSessionControls() {
  const busy = !csrfToken || sessionsLoading || Boolean(pendingAction || confirmation || logoutPending || userPending || userDialog.open || shareDialog.open || limitsDialog.open);
  $("#sessions-refresh").disabled = busy;
  $("#sessions-refresh").textContent = sessionsLoading ? "Loading…" : "Refresh sessions";
  $("#sessions-search").disabled = busy;
  $("#sessions-search-form button").disabled = busy;
  $("#sessions-previous").disabled = busy || !sessionsSnapshot || sessionsSnapshot.page <= 0;
  $("#sessions-next").disabled = busy || !sessionsSnapshot || sessionsSnapshot.page + 1 >= sessionsSnapshot.pages;
  $("#sessions-list").setAttribute("aria-busy", String(sessionsLoading));
}

function formatBytes(value) {
  if (value === null) return "N/A";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toLocaleString(undefined, { maximumFractionDigits: unit ? 1 : 0 })} ${units[unit]}`;
}

async function loadSessions(page = sessionsSnapshot?.page || 0) {
  if (!csrfToken || sessionsLoading || pendingAction || confirmation || logoutPending || userPending || userDialog.open || shareDialog.open || limitsDialog.open) return;
  const version = sessionVersion;
  sessionsLoading = true;
  syncSessionControls();
  try {
    const query = new URLSearchParams({ search: $("#sessions-search").value, page: String(page) });
    const data = await requestJson(`/api/sessions?${query}`);
    if (version !== sessionVersion) return;
    const counter = (value) => value === null || (Number.isSafeInteger(value) && value >= 0);
    if (data.ok !== true || !Array.isArray(data.sessions) || typeof data.running !== "boolean"
        || !["total", "matched", "page", "pages"].every((key) => Number.isInteger(data[key]) && data[key] >= 0)
        || data.pages < 1 || data.page >= data.pages || data.matched > data.total
        || !data.sessions.every((item) => item && ["id", "username", "duration"].every((key) => typeof item[key] === "string")
          && (item.public_ip === null || typeof item.public_ip === "string") && Array.isArray(item.vpn_addresses)
          && item.vpn_addresses.every((ip) => typeof ip === "string") && counter(item.rx_bytes) && counter(item.tx_bytes))) {
      throw new Error("Unexpected VPN session response.");
    }
    sessionsSnapshot = data;
    $("#sessions-error").hidden = true;
    $("#sessions-summary").textContent = data.running ? `${data.total} active VPN sessions · ${data.matched} matching` : "VPN service is stopped. No active sessions.";
    $("#sessions-page").textContent = `Page ${data.page + 1} of ${data.pages}`;
    $("#sessions-empty").hidden = data.sessions.length > 0;
    $("#sessions-empty").textContent = data.total && !data.matched ? "No matching VPN sessions." : "No active VPN sessions.";
    $("#sessions-list").replaceChildren(...data.sessions.map((item) => {
      const row = document.createElement("tr");
      const values = [item.username, item.vpn_addresses.join(", ") || "N/A", item.public_ip || "N/A", item.duration,
        `${formatBytes(item.rx_bytes)} / ${formatBytes(item.tx_bytes)}`];
      values.forEach((value, index) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        if (index === 0) {
          const id = document.createElement("small");
          id.className = "session-id";
          id.textContent = item.id;
          cell.append(id);
        }
        row.append(cell);
      });
      return row;
    }));
    $("#sessions-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    if (version !== sessionVersion) return;
    if (error.status === 401) { showLogin("Your session has expired. Sign in again."); return; }
    $("#sessions-error").hidden = false;
    $("#sessions-error").textContent = `${error.message} ${sessionsSnapshot ? "The previous snapshot is shown and may be out of date." : "Refresh sessions to try again."}`;
    if (!sessionsSnapshot) $("#sessions-summary").textContent = "VPN session status unavailable";
  } finally {
    if (version === sessionVersion) { sessionsLoading = false; syncSessionControls(); }
  }
}

$("#sessions-refresh").addEventListener("click", () => loadSessions());
$("#sessions-search-form").addEventListener("submit", (event) => { event.preventDefault(); loadSessions(0); });
$("#sessions-previous").addEventListener("click", () => loadSessions(sessionsSnapshot.page - 1));
$("#sessions-next").addEventListener("click", () => loadSessions(sessionsSnapshot.page + 1));

function syncUserControls() {
  const busy = usersLoading || userPending || userDialog.open || shareDialog.open || limitsDialog.open || Boolean(pendingAction || confirmation || logoutPending);
  const writable = Boolean(csrfToken && usersFresh && dashboard?.capabilities.user_management);
  $("#user-add").disabled = !writable || busy;
  $("#users-refresh").disabled = !csrfToken || busy;
  $("#users-refresh").textContent = usersLoading ? "Loading…" : "Refresh users";
  $("#users-list").setAttribute("aria-busy", String(busy));
  $("#users-list").querySelectorAll("button").forEach((button) => {
    const operation = button.dataset.userOperation;
    const missingLimits = ["limits_set", "usage_reset"].includes(operation)
      && (!dashboard?.capabilities.user_limits || !/^[0-9a-f]{32}$/.test(usersSnapshot?.limits_revision || ""));
    button.disabled = !writable || busy || missingLimits || (operation === "export" && !dashboard?.capabilities.profile_export);
  });
  $("#users-previous").disabled = busy || !usersSnapshot || usersSnapshot.page <= 0;
  $("#users-next").disabled = busy || !usersSnapshot || usersSnapshot.page + 1 >= usersSnapshot.pages;
  $("#users-progress").hidden = !userPending;
  $("#users-search").disabled = userPending;
  $("#users-search-form button").disabled = busy;
}

async function loadUsers(page = usersSnapshot?.page || 0) {
  if (!csrfToken || usersLoading || userPending || userDialog.open || shareDialog.open || limitsDialog.open) return;
  const version = sessionVersion;
  const generation = ++usersGeneration;
  usersLoading = true;
  syncUserControls();
  try {
    const query = new URLSearchParams({ search: $("#users-search").value, page: String(page) });
    const data = await requestJson(`/api/users?${query}`);
    if (version !== sessionVersion || generation !== usersGeneration) return;
    if (!Array.isArray(data.users) || typeof data.revision !== "string" || !Number.isInteger(data.total)
        || !data.users.every((user) => typeof user?.username === "string")) throw new Error("Unexpected account list response.");
    if (data.users.some((user) => user.limits !== undefined && !validUserLimits(user.limits))) throw new Error("Unexpected user limits response.");
    usersSnapshot = data;
    usersFresh = true;
    $("#users-error").hidden = true;
    $("#limits-notice").hidden = !dashboard?.capabilities.user_limits || data.limits_healthy === true;
    $("#limits-notice").textContent = "Usage accounting is delayed or enforcement needs attention. Values may be stale; check the VPN limits service and refresh users.";
    $("#users-summary").textContent = `${data.total} VPN accounts · ${data.matched} matching`;
    $("#users-page").textContent = `Page ${data.page + 1} of ${data.pages}`;
    $("#users-empty").hidden = data.users.length > 0;
    $("#users-list").replaceChildren(...data.users.map((user) => {
      const row = document.createElement("tr");
      const name = document.createElement("td");
      name.textContent = user.username;
      const usage = document.createElement("td");
      usage.className = "user-usage";
      if (user.limits) {
        const limits = user.limits;
        const traffic = document.createElement("span");
        traffic.textContent = `Traffic: ${formatBytes(limits.used_bytes)} / ${limits.byte_limit === null ? "Unlimited" : formatBytes(limits.byte_limit)}`;
        const online = document.createElement("span");
        online.textContent = limits.validity_seconds === null ? "Validity: Unlimited"
          : limits.first_connected_at === null ? `Validity: ${formatValidity(limits.validity_seconds)} · starts on first connection`
            : `Expires: ${new Date((limits.first_connected_at + limits.validity_seconds) * 1000).toLocaleString()}`;
        usage.append(traffic, online);
        if (limits.suspended || limits.blocked) {
          const blocked = document.createElement("span");
          blocked.className = "limit-blocked";
          blocked.textContent = limits.blocked ? `Limit reached: ${limits.blocked === "traffic" ? "traffic" : "validity expired"}` : "Suspended; check limits service";
          usage.append(blocked);
        }
      } else usage.textContent = "Usage unavailable";
      const actions = document.createElement("td");
      actions.className = "user-row-actions";
      [["export", "Link & QR"], ["limits_set", "Limits"], ["usage_reset", "Reset usage"], ["reset", "Change password"], ["delete", "Delete"]].forEach(([operation, label]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "secondary-button";
        button.textContent = label;
        button.dataset.user = user.username;
        button.dataset.userOperation = operation;
        button.setAttribute("aria-label", `${label}: ${user.username}`);
        actions.append(button);
      });
      row.append(name, usage, actions);
      return row;
    }));
    $("#users-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    if (version !== sessionVersion || generation !== usersGeneration) return;
    if (error.status === 401) { showLogin("Your session has expired. Sign in again."); return; }
    usersFresh = false;
    $("#users-error").hidden = false;
    $("#users-error").textContent = `${error.message} ${usersSnapshot ? "The previous list is shown. Refresh before editing accounts." : "Refresh users to try again."}`;
    if (!usersSnapshot) $("#users-summary").textContent = "Account list unavailable";
  } finally {
    if (version === sessionVersion && generation === usersGeneration) {
      usersLoading = false;
      syncUserControls();
    }
  }
}

function closeUserDialog() {
  $("#user-form").reset();
  $("#user-form").querySelectorAll("input, button").forEach((element) => { element.disabled = false; });
  ["#vpn-password", "#vpn-password-confirm"].forEach((selector) => { $(selector).type = "password"; });
  $("#user-show-password").textContent = "Show password";
  $("#user-show-password").setAttribute("aria-pressed", "false");
  $("#user-save").disabled = false;
  $("#user-cancel").disabled = false;
  if (userDialog.open) userDialog.close();
  userOperation = null;
  userRevision = null;
}

function openUserDialog(operation, username = "") {
  if (!usersFresh || usersLoading || userPending || pendingAction || confirmation || logoutPending || !dashboard?.capabilities.user_management) return;
  closeUserDialog();
  userOperation = operation;
  userRevision = usersSnapshot.revision;
  $("#user-dialog-title").textContent = operation === "create" ? "Add VPN user" : operation === "reset" ? "Change VPN password" : "Delete VPN user";
  $("#user-dialog-copy").textContent = operation === "create"
    ? "Create credentials for a new VPN account. Save the password before closing this dialog."
    : operation === "reset" ? "The new password is required on the next authentication. Existing VPN connections are not disconnected."
      : "This blocks future authentication for this account. Existing VPN connections may remain active.";
  $("#vpn-username").value = username;
  $("#vpn-username").readOnly = operation !== "create";
  $("#user-password-fields").hidden = operation === "delete";
  ["#vpn-password", "#vpn-password-confirm"].forEach((selector) => { $(selector).required = operation !== "delete"; });
  $("#user-delete-confirm-label").hidden = operation !== "delete";
  $("#user-delete-confirm").required = operation === "delete";
  const last = operation === "delete" && usersSnapshot.total === 1;
  $("#user-last-confirm-label").hidden = !last;
  $("#user-last-confirm").required = last;
  $("#user-form-error").hidden = true;
  $("#user-save").textContent = operation === "create" ? "Create user" : operation === "reset" ? "Change password" : "Delete user";
  $("#user-save").classList.toggle("destructive", operation === "delete");
  userDialog.showModal();
  syncControls();
}

$("#user-add").addEventListener("click", () => openUserDialog("create"));
$("#users-list").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-user-operation]");
  if (button && !button.disabled) {
    if (button.dataset.userOperation === "export") openShareDialog(button.dataset.user);
    else if (["limits_set", "usage_reset"].includes(button.dataset.userOperation)) openLimitsDialog(button.dataset.userOperation, button.dataset.user);
    else openUserDialog(button.dataset.userOperation, button.dataset.user);
  }
});
$("#user-cancel").addEventListener("click", () => { closeUserDialog(); syncControls(); });
userDialog.addEventListener("cancel", (event) => { event.preventDefault(); if (!userPending) { closeUserDialog(); syncControls(); } });
$("#users-refresh").addEventListener("click", () => loadUsers());
$("#users-search-form").addEventListener("submit", (event) => { event.preventDefault(); loadUsers(0); });
$("#users-previous").addEventListener("click", () => loadUsers(usersSnapshot.page - 1));
$("#users-next").addEventListener("click", () => loadUsers(usersSnapshot.page + 1));
$("#user-show-password").addEventListener("click", () => {
  const show = $("#vpn-password").type === "password";
  ["#vpn-password", "#vpn-password-confirm"].forEach((selector) => { $(selector).type = show ? "text" : "password"; });
  $("#user-show-password").textContent = show ? "Hide password" : "Show password";
  $("#user-show-password").setAttribute("aria-pressed", String(show));
});
$("#user-generate").addEventListener("click", () => {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const password = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  $("#vpn-password").value = password;
  $("#vpn-password-confirm").value = password;
  if ($("#vpn-password").type === "password") $("#user-show-password").click();
});
$("#user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (userPending || !usersFresh || !userOperation) return;
  const operation = userOperation;
  const payload = { username: $("#vpn-username").value, revision: userRevision };
  if (operation !== "delete") {
    const password = $("#vpn-password").value;
    if (!/^[\x20-\x7e]{12,128}$/.test(password) || password.includes('"') || password !== $("#vpn-password-confirm").value) {
      $("#user-form-error").textContent = "Passwords must match: 12–128 printable ASCII characters, excluding double quotes.";
      $("#user-form-error").hidden = false;
      return;
    }
    payload.password = password;
  } else {
    if (!$("#user-delete-confirm").checked || (usersSnapshot.total === 1 && !$("#user-last-confirm").checked)) return;
    payload.allow_last = $("#user-last-confirm").checked;
  }
  const version = sessionVersion;
  userPending = true;
  $("#user-form").querySelectorAll("input, button").forEach((element) => { element.disabled = true; });
  $("#user-save").disabled = true;
  $("#user-cancel").disabled = true;
  syncControls();
  try {
    const result = await requestJson(`/api/users/${operation}`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken }, body: JSON.stringify(payload),
    }, 60000);
    if (version !== sessionVersion) return;
    if (result.ok !== true) throw new Error("The server did not confirm this change.");
    showToast(result.applies_on_start ? "Account saved. It will load when the VPN service starts." : "VPN account updated.");
  } catch (error) {
    if (version !== sessionVersion) return;
    if (error.status === 401) { showLogin("Your session has expired. Sign in again."); return; }
    showToast(`Account change was not confirmed. ${error.message} Refresh before trying again.`, true);
  } finally {
    delete payload.password;
    if (version === sessionVersion) {
      userPending = false;
      closeUserDialog();
      usersFresh = false;
      syncControls();
      await loadUsers();
    }
  }
});
function validUserLimits(limits) {
  return limits && typeof limits === "object" && ["byte_limit", "validity_seconds"].every((key) =>
    limits[key] === null || (Number.isSafeInteger(limits[key]) && limits[key] > 0))
    && Number.isSafeInteger(limits.used_bytes) && limits.used_bytes >= 0
    && (limits.first_connected_at === null || (Number.isFinite(limits.first_connected_at) && limits.first_connected_at >= 0))
    && typeof limits.suspended === "boolean" && [null, "traffic", "expired"].includes(limits.blocked);
}

function formatValidity(seconds) {
  return `${(seconds / 86400).toLocaleString(undefined, { maximumFractionDigits: 3 })} days`;
}

function syncLimitInputs() {
  $("#limit-traffic").disabled = $("#limit-traffic-unlimited").checked;
  $("#limit-time").disabled = $("#limit-time-unlimited").checked;
}

function closeLimitsDialog() {
  if (limitsDialog.open) limitsDialog.close();
  $("#limits-form").reset();
  $("#limits-form").querySelectorAll("input, button").forEach((element) => { element.disabled = false; });
  $("#limits-reset-confirm").required = false;
  syncLimitInputs();
  limitOperation = limitUsername = limitAccountRevision = limitRevision = null;
}

function openLimitsDialog(operation, username) {
  if (!csrfToken || !usersFresh || usersLoading || userPending || userDialog.open || shareDialog.open || limitsDialog.open
      || pendingAction || confirmation || logoutPending || !dashboard?.capabilities.user_limits) return;
  const limits = usersSnapshot?.users.find((user) => user.username === username)?.limits;
  if (!validUserLimits(limits) || !/^[0-9a-f]{32}$/.test(usersSnapshot?.limits_revision || "")) return;
  closeLimitsDialog();
  limitOperation = operation;
  limitUsername = username;
  limitAccountRevision = usersSnapshot.revision;
  limitRevision = usersSnapshot.limits_revision;
  const resetting = operation === "usage_reset";
  $("#limits-title").textContent = `${resetting ? "Reset usage" : "Limits"}: ${username}`;
  $("#limits-copy").textContent = resetting
    ? "Reset traffic and start a new validity period with the current limits. If online, the new period starts now; otherwise, it starts on the next connection. A suspended account can reconnect after resetting."
    : "Choose a traffic allowance and validity after first connection, or leave each unlimited. Validity runs while offline too. Reaching either limit disconnects the account and blocks new connections. Reducing a limit may disconnect it immediately.";
  $("#limits-current").textContent = `Traffic used: ${formatBytes(limits.used_bytes)}\nValidity: ${limits.validity_seconds === null ? "Unlimited" : limits.first_connected_at === null ? "Awaiting first connection" : `Started ${new Date(limits.first_connected_at * 1000).toLocaleString()}`}`;
  $("#limits-fields").hidden = resetting;
  $("#limits-reset-label").hidden = !resetting;
  $("#limits-reset-confirm").required = resetting;
  $("#limit-traffic-unlimited").checked = limits.byte_limit === null;
  $("#limit-time-unlimited").checked = limits.validity_seconds === null;
  $("#limit-traffic").value = limits.byte_limit === null ? "" : limits.byte_limit / 1024 ** 3;
  $("#limit-time").value = limits.validity_seconds === null ? "" : limits.validity_seconds / 86400;
  $("#limits-save").textContent = resetting ? "Reset usage" : "Save limits";
  $("#limits-form-error").hidden = true;
  syncLimitInputs();
  limitsDialog.showModal();
  syncControls();
}

["#limit-traffic-unlimited", "#limit-time-unlimited"].forEach((selector) => $(selector).addEventListener("change", syncLimitInputs));
$("#limits-cancel").addEventListener("click", () => { closeLimitsDialog(); syncControls(); });
limitsDialog.addEventListener("cancel", (event) => { event.preventDefault(); if (!userPending) { closeLimitsDialog(); syncControls(); } });
$("#limits-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!csrfToken || userPending || !usersFresh || !limitOperation || !limitsDialog.open) return;
  const operation = limitOperation;
  const payload = { username: limitUsername, revision: limitAccountRevision, limits_revision: limitRevision };
  if (operation === "limits_set") {
    payload.byte_limit = $("#limit-traffic-unlimited").checked ? null : Math.round(Number($("#limit-traffic").value) * 1024 ** 3);
    payload.validity_seconds = $("#limit-time-unlimited").checked ? null : Math.round(Number($("#limit-time").value) * 86400);
    if ([payload.byte_limit, payload.validity_seconds].some((value) => value !== null && (!Number.isSafeInteger(value) || value <= 0))
        || payload.validity_seconds > 36600 * 86400) {
      $("#limits-form-error").textContent = "Enter positive allowances, or select Unlimited. Validity may be at most 36,600 days; the minimum is one second.";
      $("#limits-form-error").hidden = false;
      return;
    }
  } else if (!$("#limits-reset-confirm").checked) return;
  const version = sessionVersion;
  userPending = true;
  $("#limits-form").querySelectorAll("input, button").forEach((element) => { element.disabled = true; });
  syncControls();
  try {
    const result = await requestJson(`/api/users/${operation}`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken }, body: JSON.stringify(payload),
    }, 60000);
    if (version !== sessionVersion) return;
    if (result.ok !== true) throw new Error("The server did not confirm this change.");
    showToast(result.enforcement_pending ? "Limits saved; enforcement needs another attempt. Refresh users to check the service."
      : operation === "usage_reset" ? "Traffic usage and validity reset. Limits preserved." : "VPN user limits saved.", result.enforcement_pending === true);
  } catch (error) {
    if (version !== sessionVersion) return;
    if (error.status === 401) { showLogin("Your session has expired. Sign in again."); return; }
    showToast(`Limits change was not confirmed. ${error.message} Refresh before trying again.`, true);
  } finally {
    if (version === sessionVersion) {
      userPending = false;
      closeLimitsDialog();
      usersFresh = false;
      syncControls();
      await loadUsers();
    }
  }
});

function clearShareResult() {
  shareGeneration += 1;
  window.clearTimeout(shareTimer);
  $("#share-result").hidden = true;
  $("#share-link").value = "";
  $("#share-status").textContent = "";
  $("#share-error").hidden = true;
  const canvas = $("#share-qr");
  canvas.width = canvas.height = 0;
  $("#share-generate").disabled = false;
  $("#share-generate").textContent = "Generate link & QR";
  $("#share-password").disabled = false;
}

function closeShareDialog() {
  clearShareResult();
  if (shareDialog.open) shareDialog.close();
  $("#share-password").checked = false;
  $("#share-user").textContent = "";
  shareUsername = shareRevision = null;
}

function openShareDialog(username) {
  if (!csrfToken || !usersFresh || usersLoading || userPending || userDialog.open || shareDialog.open || limitsDialog.open
      || pendingAction || confirmation || logoutPending || !dashboard?.capabilities.profile_export) return;
  closeShareDialog();
  shareUsername = username;
  shareRevision = usersSnapshot.revision;
  $("#share-user").textContent = `VPN user: ${username}`;
  shareDialog.showModal();
  syncControls();
}

$("#share-generate").addEventListener("click", async () => {
  if (!csrfToken || !shareDialog.open || !shareUsername || $("#share-generate").disabled) return;
  clearShareResult();
  const generation = shareGeneration;
  const version = sessionVersion;
  const includePassword = $("#share-password").checked;
  $("#share-generate").disabled = true;
  $("#share-password").disabled = true;
  $("#share-generate").textContent = "Generating…";
  try {
    const data = await requestJson("/api/users/export", {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ username: shareUsername, revision: shareRevision, include_password: includePassword }),
    });
    if (version !== sessionVersion || generation !== shareGeneration || !shareDialog.open) return;
    if (data.ok !== true || typeof data.uri !== "string" || !/^ikev:\/\/import\/v1\/[A-Za-z0-9_-]+$/.test(data.uri)
        || data.uri.length > 2953 || data.includes_password !== includePassword) throw new Error("Unexpected configuration response.");
    const qr = qrcodegen.QrCode.encodeText(data.uri, qrcodegen.QrCode.Ecc.LOW);
    const canvas = $("#share-qr");
    const scale = 5;
    canvas.width = canvas.height = (qr.size + 8) * scale;
    const context = canvas.getContext("2d");
    context.fillStyle = "#fff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = "#000";
    for (let y = 0; y < qr.size; y++) for (let x = 0; x < qr.size; x++) {
      if (qr.getModule(x, y)) context.fillRect((x + 4) * scale, (y + 4) * scale, scale, scale);
    }
    $("#share-link").value = data.uri;
    $("#share-status").textContent = includePassword
      ? "Password included. To revoke account access, change the password or delete the user."
      : "Password omitted. The app must ask the user for their VPN password.";
    $("#share-result").hidden = false;
    shareTimer = window.setTimeout(() => { closeShareDialog(); syncControls(); }, 5 * 60 * 1000);
  } catch (error) {
    if (version !== sessionVersion || generation !== shareGeneration) return;
    if (error.status === 401) { showLogin("Your session has expired. Sign in again."); return; }
    $("#share-error").textContent = error.message;
    $("#share-error").hidden = false;
  } finally {
    if (version === sessionVersion && generation === shareGeneration) {
      $("#share-generate").disabled = false;
      $("#share-password").disabled = false;
      $("#share-generate").textContent = "Generate link & QR";
    }
  }
});
$("#share-password").addEventListener("change", clearShareResult);
$("#share-close").addEventListener("click", () => { closeShareDialog(); syncControls(); });
shareDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeShareDialog(); syncControls(); });
window.addEventListener("pagehide", closeShareDialog);
$("#share-copy-link").addEventListener("click", async () => {
  const link = $("#share-link");
  const generation = shareGeneration;
  if (!link.value) return;
  try {
    await navigator.clipboard.writeText(link.value);
    if (generation === shareGeneration) showToast("Configuration link copied.");
  } catch (_error) {
    if (generation !== shareGeneration) return;
    link.focus();
    link.select();
    showToast("Copy the selected configuration link manually.");
  }
});
$("#share-download").addEventListener("click", () => {
  if (!$("#share-link").value) return;
  const anchor = document.createElement("a");
  anchor.download = `${shareUsername}-ikev-qr.png`;
  anchor.href = $("#share-qr").toDataURL("image/png");
  anchor.click();
});
function clearSecuritySetup() {
  window.clearTimeout(securityTimer);
  securityTimer = null;
  $("#security-setup").hidden = true;
  $("#security-secret").value = "";
  $("#security-confirm-code").value = "";
  $("#security-password").value = "";
  $("#security-disable-code").value = "";
  $("#security-qr").width = $("#security-qr").height = 0;
}

function resetSecurity() {
  securityGeneration += 1;
  securityBusy = false;
  securityEnabled = null;
  clearSecuritySetup();
  $("#security-status").textContent = "Checking two-factor settings…";
  $("#security-error").hidden = true;
  $("#security-retry").hidden = true;
  $("#security-password-form").hidden = true;
}

function syncSecurityControls() {
  const busy = securityBusy || !csrfToken;
  $("#security-password-form").querySelectorAll("input, button").forEach((item) => { item.disabled = busy; });
  $("#security-confirm-form").querySelectorAll("input, button").forEach((item) => { item.disabled = busy; });
  $("#security-disable-code").disabled = busy || securityEnabled !== true;
  $("#security-retry").disabled = busy;
}

function renderSecurity(enabled) {
  if (typeof enabled !== "boolean") throw new Error("Two-factor status could not be confirmed. Refresh Security.");
  securityEnabled = enabled;
  $("#security-status").textContent = enabled ? "Enabled — sign-in requires your password and an Authenticator code." : "Disabled — sign-in currently uses your password.";
  $("#security-password-form").hidden = false;
  $("#security-disable-code").hidden = $("#security-disable-code-label").hidden = !enabled;
  $("#security-disable-code").required = enabled;
  $("#security-start").textContent = enabled ? "Disable two-factor authentication" : "Set up two-factor authentication";
  syncSecurityControls();
}

function securityError(error) {
  if (error.status === 401) { showLogin("Your session expired. Sign in again."); return; }
  $("#security-error").textContent = error.message;
  $("#security-error").hidden = false;
}

async function loadSecurity() {
  if (!csrfToken || securityBusy) return;
  const generation = securityGeneration;
  securityBusy = true;
  syncControls();
  try {
    const data = await requestJson("/api/auth/2fa");
    if (generation !== securityGeneration) return;
    renderSecurity(data.enabled);
  } catch (error) {
    if (generation !== securityGeneration) return;
    securityError(error);
    $("#security-status").textContent = "Two-factor status unavailable.";
    $("#security-retry").hidden = false;
  } finally {
    if (generation === securityGeneration) { securityBusy = false; syncControls(); }
  }
}

async function securityRequest(operation, payload) {
  return requestJson(`/api/auth/2fa/${operation}`, {
    method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken }, body: JSON.stringify(payload),
  });
}

$("#security-retry").addEventListener("click", () => { resetSecurity(); loadSecurity(); });
$("#security-password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (securityBusy || securityEnabled === null || !csrfToken) return;
  const generation = securityGeneration;
  const disabling = securityEnabled;
  const payload = { password: $("#security-password").value };
  if (disabling) payload.code = $("#security-disable-code").value;
  $("#security-password").value = "";
  $("#security-disable-code").value = "";
  securityBusy = true;
  $("#security-error").hidden = true;
  syncControls();
  try {
    const data = await securityRequest(disabling ? "disable" : "setup", payload);
    if (generation !== securityGeneration) return;
    if (disabling) {
      if (data.enabled !== false) throw new Error("Disabling two-factor authentication was not confirmed. Refresh Security.");
      clearSecuritySetup();
      renderSecurity(false);
      showToast("Two-factor authentication disabled. Other panel sessions were signed out.");
    } else {
      if (!/^[A-Z2-7]{32}$/.test(data.secret) || typeof data.uri !== "string" || !data.uri.startsWith("otpauth://totp/")) throw new Error("Setup could not be confirmed. Try again.");
      const qr = qrcodegen.QrCode.encodeText(data.uri, qrcodegen.QrCode.Ecc.MEDIUM);
      const canvas = $("#security-qr");
      const scale = 6;
      canvas.width = canvas.height = (qr.size + 8) * scale;
      const context = canvas.getContext("2d");
      context.fillStyle = "#fff";
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.fillStyle = "#000";
      for (let y = 0; y < qr.size; y++) for (let x = 0; x < qr.size; x++) {
        if (qr.getModule(x, y)) context.fillRect((x + 4) * scale, (y + 4) * scale, scale, scale);
      }
      $("#security-secret").value = data.secret;
      $("#security-password-form").hidden = true;
      $("#security-setup").hidden = false;
      securityTimer = window.setTimeout(() => {
        if (generation !== securityGeneration) return;
        resetSecurity(); loadSecurity();
        securityError(new Error("Setup expired. Start again to get a new QR code."));
      }, 600000);
    }
  } catch (error) {
    if (generation === securityGeneration) securityError(error);
  } finally {
    payload.password = "";
    if (generation === securityGeneration) {
      securityBusy = false; syncControls();
      if (!$("#security-setup").hidden) $("#security-confirm-code").focus();
    }
  }
});

$("#security-confirm-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (securityBusy || !csrfToken) return;
  const generation = securityGeneration;
  const code = $("#security-confirm-code").value;
  $("#security-confirm-code").value = "";
  securityBusy = true;
  $("#security-error").hidden = true;
  syncControls();
  try {
    const data = await securityRequest("enable", { code });
    if (generation !== securityGeneration) return;
    if (data.enabled !== true) throw new Error("Enabling two-factor authentication was not confirmed. Refresh Security.");
    clearSecuritySetup(); renderSecurity(true);
    showToast("Two-factor authentication enabled. Other panel sessions were signed out. Use a new code at your next sign-in.");
  } catch (error) {
    if (generation === securityGeneration) securityError(error);
  } finally {
    if (generation === securityGeneration) { securityBusy = false; syncControls(); }
  }
});

$("#security-cancel").addEventListener("click", async () => {
  if (securityBusy) return;
  const generation = securityGeneration;
  clearSecuritySetup();
  securityBusy = true; syncControls();
  try { await securityRequest("cancel", {}); }
  catch (error) { if (generation === securityGeneration) securityError(error); }
  finally {
    if (generation === securityGeneration) {
      resetSecurity();
      await loadSecurity();
    }
  }
});

window.addEventListener("hashchange", selectView);

initialize();
