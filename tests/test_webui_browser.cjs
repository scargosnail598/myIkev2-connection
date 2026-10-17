// Optional browser checks: use a local static server and mocked API only.
const { test, before, after } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");
const { deflateSync, inflateSync } = require("node:zlib");
const { randomBytes } = require("node:crypto");
const jsQR = require("jsqr");
const { firefox } = require("playwright");

let browser;
let server;
let base;
const staticRoot = path.resolve(__dirname, "../webui/static");
const snapshot = () => ({
  installed: true,
  capabilities: { service_actions: true, user_management: true, profile_export: true, user_limits: true },
  services: [
    { id: "ikev2", label: "IKEv2 / StrongSwan", unit: "strongswan-starter.service", state: "active" },
    { id: "firewall", label: "Firewall & NAT", unit: "ikev2-vpn-firewall.service", state: "active" },
    { id: "proxy", label: "SOCKS5 Proxy", unit: "ikev2-vpn-proxy.service", state: "inactive" },
  ],
});

const resourceSnapshot = () => ({
  ok: true, sampled_at: 1792238400, interval_seconds: 5, window_seconds: 300,
  cpu: { percent: 42.5, cores: 2 }, memory: { percent: 62.5, used_bytes: 5368709120, total_bytes: 8589934592 },
  network: { interface: "ens3", source: "configured", rx_bytes: 12884901888, tx_bytes: 6442450944, rx_bytes_per_second: 1048576, tx_bytes_per_second: 524288 },
  history: Array.from({ length: 61 }, (_, index) => ({ age_seconds: (60 - index) * 5, cpu_percent: 25 + 17 * Math.sin(index / 4) ** 2,
    memory_percent: 58 + index / 15, interface: "ens3", rx_bytes_per_second: 500000 + 500000 * Math.sin(index / 5) ** 2,
    tx_bytes_per_second: 200000 + 300000 * Math.cos(index / 4) ** 2 })),
});

before(async () => {
  server = http.createServer(async (request, response) => {
    const assets = { "/": ["index.html", "text/html"], "/assets/app.js": ["app.js", "text/javascript"], "/assets/qrcodegen.js": ["qrcodegen.js", "text/javascript"], "/assets/app.css": ["app.css", "text/css"] };
    const asset = assets[request.url];
    if (!asset) { response.writeHead(404).end(); return; }
    response.setHeader("Content-Type", asset[1]);
    response.setHeader("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'none'");
    response.end(await fs.readFile(path.join(staticRoot, asset[0])));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${server.address().port}`;
  browser = await firefox.launch();
});

after(async () => {
  if (browser) await browser.close();
  if (server) await new Promise((resolve) => server.close(resolve));
});

async function waitText(page, selector, text) {
  await page.waitForFunction(({ selector, text }) => document.querySelector(selector)?.textContent.includes(text), { selector, text });
}

async function fixture(t, options = {}) {
  const context = await browser.newContext({ viewport: options.viewport || { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  const state = { authenticated: true, data: snapshot(), dashboardFailure: false, logoutFailure: false, sessionFailure: false, loginLimited: false, commandFailure: false, ...options };
  state.userNames = options.userNames || ["alice", "bob", "carol"];
  state.userLimits = options.userLimits || {};
  const limitsFor = (username) => state.userLimits[username] || { byte_limit: null, validity_seconds: null,
    used_bytes: 0, first_connected_at: null, reset_at: 1792238400, blocked: null, suspended: false };
  const calls = { dashboard: 0, commands: 0, logout: 0, sessions: 0, diagnostics: 0, metrics: 0, userChanges: [], exports: [], limitChanges: [] };
  const session = () => ({ authenticated: state.authenticated, username: state.authenticated ? "browser-test-admin" : null, csrf_token: state.authenticated ? "browser-test-csrf" : null });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const endpoint = new URL(request.url()).pathname;
    const json = (body, status = 200, headers = {}) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body), headers });
    if (endpoint === "/api/session") {
      if (state.sessionFailure) return route.fulfill({ status: 503, contentType: "text/html", body: "<h1>Unavailable</h1>" });
      return json(session());
    }
    if (endpoint === "/api/dashboard") {
      calls.dashboard += 1;
      if (state.dashboardHold) {
        const hold = state.dashboardHold;
        state.dashboardHold = null;
        await hold;
        return json(state.delayedData || snapshot());
      }
      if (!state.authenticated) return json({ message: "Sign in to continue." }, 401);
      if (state.dashboardFailure) return json({ message: "Status is temporarily unavailable." }, 503);
      return json(state.data);
    }
    if (endpoint === "/api/auth/login") {
      if (state.loginLimited) return json({ message: "Too many failed attempts." }, 429, { "Retry-After": "120" });
      state.authenticated = true;
      return json(session());
    }
    if (endpoint === "/api/metrics") {
      calls.metrics += 1;
      if (state.metricsHold) await state.metricsHold;
      if (!state.authenticated) return json({ message: "Sign in to continue." }, 401);
      if (state.metricsFailure) return json({ message: "Server readings unavailable." }, 503);
      return json(state.resourceData || resourceSnapshot());
    }
    if (endpoint === "/api/diagnostics") {
      calls.diagnostics += 1;
      if (!state.authenticated) return json({ message: "Sign in to continue." }, 401);
      if (state.diagnosticsHold) await state.diagnosticsHold;
      if (state.diagnosticsFailure) return json({ message: "Broker is unavailable." }, 503);
      if (state.diagnosticsMalformed) return json({ ok: true, checks: [] });
      return json({ ok: true, checked_at: "2026-10-17T12:00:00+00:00", checks: state.diagnosticChecks || [
        { id: "vpn", label: "VPN service", status: "pass", detail: "Service state: active.", guidance: "" },
        { id: "cert", label: "Server certificate dates", status: "warn", detail: "Certificate expires in 13 days.", guidance: "Review certificate renewal before it expires." },
        { id: "udp", label: "IKEv2 UDP ports", status: "fail", detail: "No local binding for UDP 4500.", guidance: "Check the VPN service." },
        { id: "forwarding", label: "IPv4 forwarding", status: "unknown", detail: "The forwarding setting could not be read.", guidance: "" },
      ] });
    }
    if (endpoint === "/api/sessions") {
      calls.sessions += 1;
      if (!state.authenticated) return json({ message: "Sign in to continue." }, 401);
      if (state.sessionsFailure) return json({ message: "VPN session status unavailable." }, 503);
      if (state.sessionsHold) await state.sessionsHold;
      const all = state.vpnSessions || [{ id: "ikev2-eap[7]", username: "alice", public_ip: "198.51.100.2", duration: "2 minutes ago",
        vpn_addresses: ["10.10.10.1", "fd00::1"], rx_bytes: 1024, tx_bytes: null }];
      const query = new URL(request.url()).searchParams;
      const matching = all.filter((item) => item.username.includes(query.get("search") || ""));
      const pages = Math.max(1, Math.ceil(matching.length / 20));
      const page = Math.min(Number(query.get("page") || 0), pages - 1);
      return json({ ok: true, sessions: matching.slice(page * 20, page * 20 + 20), total: all.length, matched: matching.length, page, pages, running: state.vpnRunning !== false });
    }
    if (endpoint === "/api/users") {
      if (state.userFailure) return json({ message: "Account list unavailable." }, 503);
      const query = new URL(request.url()).searchParams;
      const names = state.userNames.filter((name) => name.includes(query.get("search") || ""));
      const page = Math.min(Number(query.get("page") || 0), Math.max(0, Math.ceil(names.length / 20) - 1));
      return json({ ok: true, users: names.slice(page * 20, page * 20 + 20).map((username) => ({ username, limits: limitsFor(username) })),
        total: state.userNames.length, matched: names.length, page, pages: Math.max(1, Math.ceil(names.length / 20)), revision: "1".repeat(64),
        limits_revision: "2".repeat(32), limits_healthy: state.limitsHealthy !== false, limits_sampled_at: 1792238400 });
    }
    assert.equal(request.headers()["x-csrf-token"], "browser-test-csrf");
    if (["/api/users/limits_set", "/api/users/usage_reset"].includes(endpoint)) {
      const payload = request.postDataJSON();
      calls.limitChanges.push({ operation: endpoint.split("/").at(-1), ...payload });
      if (state.limitChangeHold) await state.limitChangeHold;
      if (state.limitChangeFailure) return json({ message: "Limits changed. Refresh users before retrying." }, state.limitChangeFailure);
      const limits = { ...limitsFor(payload.username) };
      if (endpoint.endsWith("limits_set")) Object.assign(limits, { byte_limit: payload.byte_limit, validity_seconds: payload.validity_seconds });
      else Object.assign(limits, { used_bytes: 0, first_connected_at: null, blocked: null, suspended: false });
      state.userLimits[payload.username] = limits;
      return json({ ok: true, enforcement_pending: state.enforcementPending === true });
    }
    if (endpoint === "/api/users/export") {
      const payload = request.postDataJSON();
      calls.exports.push(payload);
      if (state.exportHold) await state.exportHold;
      if (state.exportFailure) return json({ message: "Accounts changed. Refresh users before exporting." }, state.exportFailure);
      if (state.exportMalformed) return json({ ok: true, uri: "https://unexpected.example", includes_password: false });
      const profile = { format: "ikev-profile", version: 1, name: "alice@vpn.example.test", server: "vpn.example.test",
        remote_id: "vpn.example.test", username: payload.username, authentication: "eap-mschapv2", certificate_trust: "private-ca",
        ca_certificate: { encoding: "der-base64", data: state.fixtureCertificate || "RkFLRQ==", sha256: "00:11:22" },
        connection: { mode: "full-tunnel", ipv6: true }, server_profile: "secure", proxy: { enabled: false } };
      const envelope = { format: "ikev-share", version: 1, profile };
      if (payload.include_password) envelope.password = "browser-fixture-password-only";
      state.exportUri = `ikev://import/v1/${deflateSync(JSON.stringify(envelope), { level: 9 }).toString("base64url")}`;
      return json({ ok: true, uri: state.exportUri, includes_password: payload.include_password });
    }
    if (endpoint.startsWith("/api/users/")) {
      const payload = request.postDataJSON();
      calls.userChanges.push({ operation: endpoint.split("/").at(-1), ...payload });
      if (state.userChangeHold) await state.userChangeHold;
      if (state.userChangeFailure) return json({ message: "Accounts changed. Refresh the list." }, 409);
      if (endpoint.endsWith("/create")) state.userNames.push(payload.username);
      if (endpoint.endsWith("/delete")) state.userNames = state.userNames.filter((name) => name !== payload.username);
      return json({ ok: true });
    }
    if (endpoint === "/api/auth/logout") {
      calls.logout += 1;
      if (state.logoutFailure) return json({ message: "Please try again." }, 503);
      state.authenticated = false;
      return json({ authenticated: false });
    }
    if (endpoint.startsWith("/api/services/")) {
      calls.commands += 1;
      if (state.commandHold) await state.commandHold;
      return state.commandFailure ? json({ message: "Service action failed." }, 502) : json({ ok: true });
    }
    throw new Error(`Unexpected API endpoint: ${endpoint}`);
  });
  t.after(async () => { await context.close(); assert.deepEqual(errors, []); });
  await page.clock.install();
  await page.goto(base);
  if (state.sessionFailure) await page.locator("#connection-retry").waitFor({ state: "visible" });
  else if (state.authenticated) await waitText(page, "#running-services", "2 / 3");
  else await page.locator("#login-view").waitFor({ state: "visible" });
  return { page, state, calls };
}

test("dashboard uses service data and refreshes automatically", async (t) => {
  const { page, calls } = await fixture(t);
  assert.equal(await page.locator("#attention-services").textContent(), "0");
  assert.equal(await page.locator(".service-card").count(), 3);
  assert.equal(await page.locator("#broker-notice").isVisible(), false);
  await page.clock.fastForward(30000);
  await page.waitForFunction(() => !document.querySelector("#refresh").disabled);
  assert.equal(calls.dashboard, 2);
});

test("failed refresh preserves data, pauses controls, and recovers", async (t) => {
  const { page, state } = await fixture(t);
  state.dashboardFailure = true;
  await page.locator("#refresh").click();
  await waitText(page, "#mode-badge", "OUT OF DATE");
  assert.equal(await page.locator(".service-card").count(), 3);
  assert.equal(await page.locator("#running-services").textContent(), "2 / 3");
  assert.equal(await page.getByRole("button", { name: "Stop IKEv2 / StrongSwan", exact: true }).isDisabled(), true);
  state.dashboardFailure = false;
  await page.locator("#refresh").click();
  await waitText(page, "#mode-badge", "CONTROLS AVAILABLE");
  assert.equal(await page.locator("#status-notice").isVisible(), false);
  assert.equal(await page.getByRole("button", { name: "Stop IKEv2 / StrongSwan", exact: true }).isDisabled(), false);
});

test("confirmation cancels by keyboard and only sends one command while busy", async (t) => {
  const { page, state, calls } = await fixture(t);
  const button = page.getByRole("button", { name: "Restart IKEv2 / StrongSwan", exact: true });
  await button.click();
  assert.match(await page.locator("#action-copy").textContent(), /VPN connections may be disconnected/);
  await page.keyboard.press("Escape");
  assert.equal(calls.commands, 0);
  assert.equal(await button.evaluate((element) => element === document.activeElement), true);
  let release;
  state.commandHold = new Promise((resolve) => { release = resolve; });
  t.after(() => release());
  await button.click();
  await page.locator("#action-confirm").evaluate((element) => { element.click(); element.click(); });
  await page.locator("#action-progress").waitFor({ state: "visible" });
  await page.waitForFunction(() => document.querySelector("#refresh").disabled);
  assert.equal(await button.isDisabled(), true);
  const reads = calls.dashboard;
  await page.clock.fastForward(30000);
  assert.equal(calls.dashboard, reads);
  await page.waitForTimeout(1);
  assert.equal(calls.commands, 1);
  release();
  await waitText(page, "#toast", "restart completed");
  await page.locator("#action-progress").waitFor({ state: "hidden" });
  assert.equal(calls.commands, 1);
});

test("failed command reports an uncertain outcome and is never retried automatically", async (t) => {
  const { page, calls } = await fixture(t, { commandFailure: true });
  await page.getByRole("button", { name: "Start SOCKS5 Proxy", exact: true }).click();
  assert.match(await page.locator("#action-copy").textContent(), /start SOCKS5 Proxy/);
  await page.locator("#action-confirm").click();
  await waitText(page, "#toast", "command was not confirmed");
  await page.locator("#action-progress").waitFor({ state: "hidden" });
  await page.clock.fastForward(30000);
  assert.equal(calls.commands, 1);
});

test("failed logout keeps the session visible until logout is confirmed", async (t) => {
  const { page, state, calls } = await fixture(t, { logoutFailure: true });
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await waitText(page, "#toast", "Sign out was not confirmed");
  assert.equal(await page.locator("#app-shell").isVisible(), true);
  assert.equal(await page.locator("#login-view").isVisible(), false);
  state.logoutFailure = false;
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.locator("#login-view").waitFor({ state: "visible" });
  assert.equal(calls.logout, 2);
});

test("expired session returns to login", async (t) => {
  const { page, state } = await fixture(t);
  state.authenticated = false;
  await page.locator("#refresh").click();
  await page.locator("#login-view").waitFor({ state: "visible" });
  assert.match(await page.locator("#login-error").textContent(), /session has expired/);
  assert.equal(await page.locator("#app-shell").isVisible(), false);
});

test("an old status response cannot overwrite a newly signed-in session", async (t) => {
  const { page, state, calls } = await fixture(t);
  let release;
  state.dashboardHold = new Promise((resolve) => { release = resolve; });
  t.after(() => release());
  await page.locator("#refresh").click();
  await page.waitForFunction(() => document.querySelector("#refresh").disabled);
  // Wait until the mock has captured the previous session's status request.
  while (calls.dashboard < 2) await new Promise((resolve) => setTimeout(resolve, 5));
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.locator("#login-view").waitFor({ state: "visible" });
  state.data.installed = false;
  await page.locator("#username").fill("browser-test-admin");
  await page.locator("#password").fill("browser-test-password");
  await page.getByRole("button", { name: /Sign in/ }).click();
  await waitText(page, "#installation-state", "Not detected");
  release();
  await page.waitForTimeout(100);
  assert.equal(await page.locator("#installation-state").textContent(), "Not detected");
});

test("boot failure offers retry and login throttling explains the delay", async (t) => {
  const { page, state } = await fixture(t, { sessionFailure: true, authenticated: false });
  assert.equal(await page.locator("#login-view").isVisible(), false);
  state.sessionFailure = false;
  state.loginLimited = true;
  await page.locator("#connection-retry").click();
  await page.locator("#login-view").waitFor({ state: "visible" });
  await page.locator("#username").fill("browser-test-admin");
  await page.locator("#password").fill("browser-test-password");
  await page.getByRole("button", { name: /Sign in/ }).click();
  await waitText(page, "#login-error", "120 seconds");
  assert.equal(await page.locator("#password").inputValue(), "");
});

test("read-only and transitioning services keep controls disabled", async (t) => {
  const { page, state } = await fixture(t);
  state.data.capabilities.service_actions = false;
  await page.locator("#refresh").click();
  await waitText(page, "#mode-badge", "VIEW ONLY");
  assert.equal(await page.locator("#services button:enabled").count(), 0);
  state.data.capabilities.service_actions = true;
  state.data.services[0].state = "activating";
  await page.locator("#refresh").click();
  await waitText(page, ".status.activating", "Starting");
  assert.equal(await page.locator('#services button[data-target="ikev2"]:enabled').count(), 0);
  assert.equal(await page.locator('#services button[data-target="proxy"]:enabled').count(), 3);
  state.data.services[0].state = "failed";
  await page.locator("#refresh").click();
  await waitText(page, "#attention-services", "1");
  assert.equal(await page.locator("#attention-card.needs-attention").count(), 1);
});

test("desktop and mobile layouts fit and dialogs remain accessible", async (t) => {
  const { page } = await fixture(t);
  const output = process.env.UI_SCREENSHOT_DIR;
  if (output) {
    await fs.mkdir(output, { recursive: true });
    await page.screenshot({ path: path.join(output, "desktop.png"), fullPage: true });
  }
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.getByRole("button", { name: "Stop SOCKS5 Proxy", exact: true }).click();
    assert.match(await page.locator("#action-copy").textContent(), /proxy connections/);
    const dialog = await page.getByRole("dialog").boundingBox();
    assert.ok(dialog.x >= 0 && dialog.x + dialog.width <= width);
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
  }
  if (output) await page.screenshot({ path: path.join(output, "mobile.png"), fullPage: true });
});

test("users navigation searches and paginates the real API shape", async (t) => {
  const { page } = await fixture(t, { userNames: Array.from({ length: 25 }, (_, i) => `user${String(i).padStart(2, "0")}`) });
  await page.locator('[data-view="users"]').click();
  await waitText(page, "#users-summary", "25 VPN accounts");
  assert.equal(await page.locator("#users-list tr").count(), 20);
  await page.locator("#users-next").click();
  await waitText(page, "#users-page", "Page 2 of 2");
  assert.equal(await page.locator("#users-list tr").count(), 5);
  await page.locator("#users-search").fill("user24");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await waitText(page, "#users-summary", "1 matching");
  assert.equal(await page.locator("#users-list tr").count(), 1);
  await page.locator("#users-search").fill("absent");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await page.locator("#users-empty").waitFor({ state: "visible" });
});

test("create validates passwords, sends once and clears credentials after saving", async (t) => {
  const { page, state, calls } = await fixture(t);
  await page.locator('[data-view="users"]').click();
  await waitText(page, "#users-summary", "3 VPN accounts");
  await page.locator("#user-add").click();
  await page.locator("#vpn-username").fill("new-user");
  await page.locator("#vpn-password").fill("fixture password one");
  await page.locator("#vpn-password-confirm").fill("fixture password two");
  await page.locator("#user-save").click();
  await page.locator("#user-form-error").waitFor({ state: "visible" });
  assert.equal(calls.userChanges.length, 0);
  await page.locator("#user-generate").click();
  assert.match(await page.locator("#vpn-password").inputValue(), /^[a-f0-9]{32}$/);
  let release;
  state.userChangeHold = new Promise((resolve) => { release = resolve; });
  t.after(() => release());
  await page.locator("#user-save").click();
  await page.waitForFunction(() => document.querySelector("#user-save").disabled);
  assert.equal(await page.locator("#vpn-password").isDisabled(), true);
  while (calls.userChanges.length < 1) await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(calls.userChanges[0].username, "new-user");
  release();
  await page.locator("#user-dialog").waitFor({ state: "hidden" });
  await waitText(page, "#users-summary", "4 VPN accounts");
  assert.equal(await page.locator("#vpn-password").inputValue(), "");
  assert.equal(calls.userChanges.length, 1);
});

test("password reset explains active sessions and cancel clears the form", async (t) => {
  const { page, calls } = await fixture(t);
  await page.locator('[data-view="users"]').click();
  await waitText(page, "#users-summary", "3 VPN accounts");
  await page.getByRole("button", { name: "Change password: alice", exact: true }).click();
  assert.match(await page.locator("#user-dialog-copy").textContent(), /not disconnected/);
  assert.equal(await page.locator("#vpn-username").getAttribute("readonly"), "");
  await page.locator("#user-generate").click();
  await page.keyboard.press("Escape");
  assert.equal(await page.locator("#vpn-password").inputValue(), "");
  assert.equal(calls.userChanges.length, 0);
  await page.getByRole("button", { name: "Change password: alice", exact: true }).click();
  await page.locator("#user-generate").click();
  await page.locator("#user-save").click();
  await page.locator("#user-dialog").waitFor({ state: "hidden" });
  assert.equal(calls.userChanges[0].operation, "reset");
});

test("deleting the final account requires both explicit confirmations", async (t) => {
  const { page, calls } = await fixture(t, { userNames: ["alice"] });
  await page.locator('[data-view="users"]').click();
  await waitText(page, "#users-summary", "1 VPN accounts");
  await page.getByRole("button", { name: "Delete: alice", exact: true }).click();
  await page.locator("#user-delete-confirm").check();
  await page.locator("#user-save").click();
  assert.equal(calls.userChanges.length, 0);
  await page.locator("#user-last-confirm").check();
  await page.locator("#user-save").click();
  await page.locator("#user-dialog").waitFor({ state: "hidden" });
  await waitText(page, "#users-summary", "0 VPN accounts");
  assert.equal(calls.userChanges[0].allow_last, true);
  assert.equal(calls.userChanges[0].operation, "delete");
});

test("account failures keep data stale and conflicts are never retried", async (t) => {
  const { page, state, calls } = await fixture(t);
  await page.locator('[data-view="users"]').click();
  await waitText(page, "#users-summary", "3 VPN accounts");
  state.userFailure = true;
  await page.locator("#users-refresh").click();
  await page.locator("#users-error").waitFor({ state: "visible" });
  assert.equal(await page.locator("#user-add").isDisabled(), true);
  assert.equal(await page.locator("#users-list tr").count(), 3);
  state.userFailure = false;
  await page.locator("#users-refresh").click();
  await page.locator("#users-error").waitFor({ state: "hidden" });
  state.userChangeFailure = true;
  await page.getByRole("button", { name: "Delete: alice", exact: true }).click();
  await page.locator("#user-delete-confirm").check();
  await page.locator("#user-save").click();
  await waitText(page, "#toast", "not confirmed");
  await page.locator("#user-dialog").waitFor({ state: "hidden" });
  assert.equal(calls.userChanges.length, 1);
});

test("users list and password dialog fit mobile with long usernames", async (t) => {
  const { page } = await fixture(t, { userNames: ["a".repeat(64)] });
  await page.setViewportSize({ width: 320, height: 844 });
  await page.locator('[data-view="users"]').click();
  await waitText(page, "#users-summary", "1 VPN accounts");
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  if (process.env.UI_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.UI_SCREENSHOT_DIR, "users-mobile.png"), fullPage: true });
  await page.getByRole("button", { name: `Change password: ${"a".repeat(64)}`, exact: true }).click();
  const bounds = await page.getByRole("dialog").boundingBox();
  assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 320);
});


test("sessions show dual-stack addresses and unknown counters, refresh, and keep stale data", async (t) => {
  const { page, state, calls } = await fixture(t);
  await page.locator('[data-view="sessions"]').click();
  await waitText(page, "#sessions-summary", "1 active VPN sessions");
  await waitText(page, "#sessions-list", "1 KiB / N/A");
  assert.match(await page.locator("#sessions-list").textContent(), /10\.10\.10\.1, fd00::1/);
  assert.equal(await page.locator('[data-view="sessions"]').getAttribute("aria-current"), "page");
  await page.clock.fastForward(30000);
  await page.waitForFunction(() => !document.querySelector("#sessions-refresh").disabled);
  assert.equal(calls.sessions, 2);
  state.sessionsFailure = true;
  await page.locator("#sessions-refresh").click();
  await waitText(page, "#sessions-error", "previous snapshot");
  assert.equal(await page.locator("#sessions-list tr").count(), 1);
  state.sessionsFailure = false;
  state.vpnSessions = [];
  await page.locator("#sessions-refresh").click();
  await waitText(page, "#sessions-summary", "0 active VPN sessions");
  assert.equal(await page.locator("#sessions-empty").isVisible(), true);
  assert.equal(await page.locator("#sessions-error").isVisible(), false);
  assert.equal(calls.commands, 0);
});

test("sessions support pagination and search with safe text on mobile", async (t) => {
  const all = Array.from({ length: 21 }, (_, index) => ({ id: `ikev2-eap[${index}]`, username: index === 20 ? '<img src=x onerror="alert(1)">' : `user${index}`,
    public_ip: "198.51.100.2", duration: "1 minute ago", vpn_addresses: ["10.10.10.1"], rx_bytes: 0, tx_bytes: 0 }));
  const { page } = await fixture(t, { viewport: { width: 320, height: 844 }, vpnSessions: all });
  await page.locator('[data-view="sessions"]').click();
  await waitText(page, "#sessions-page", "Page 1 of 2");
  assert.equal(await page.locator("#sessions-list tr").count(), 20);
  await page.locator("#sessions-next").click();
  await waitText(page, "#sessions-page", "Page 2 of 2");
  assert.equal(await page.locator("#sessions-list tr").count(), 1);
  assert.equal(await page.locator("#sessions-list img").count(), 0);
  assert.match(await page.locator("#sessions-list").textContent(), /onerror/);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  await page.locator("#sessions-search").fill("missing");
  await page.locator("#sessions-search-form button").click();
  await waitText(page, "#sessions-empty", "No matching");
  await waitText(page, "#sessions-page", "Page 1 of 1");
});

test("sessions distinguish unavailable status from stopped service and expire cleanly", async (t) => {
  const { page, state } = await fixture(t, { sessionsFailure: true });
  await page.locator('[data-view="sessions"]').click();
  await waitText(page, "#sessions-summary", "unavailable");
  assert.equal(await page.locator("#sessions-empty").isVisible(), false);
  state.sessionsFailure = false;
  state.vpnRunning = false;
  state.vpnSessions = [];
  await page.locator("#sessions-refresh").click();
  await waitText(page, "#sessions-summary", "VPN service is stopped");
  state.authenticated = false;
  await page.locator("#sessions-refresh").click();
  await page.locator("#login-view").waitFor({ state: "visible" });
  assert.equal(await page.locator("#sessions-list tr").count(), 0);
});

test("sessions ignore a response arriving after sign out", async (t) => {
  const { page, state } = await fixture(t);
  let release;
  state.sessionsHold = new Promise((resolve) => { release = resolve; });
  t.after(() => release());
  await page.locator('[data-view="sessions"]').click();
  await page.waitForFunction(() => document.querySelector("#sessions-refresh").disabled);
  await page.locator("#logout").click();
  await page.locator("#login-view").waitFor({ state: "visible" });
  release();
  await page.waitForTimeout(100);
  assert.equal(await page.locator("#sessions-list tr").count(), 0);
});


test("diagnostics renders all result types and guidance without sending commands or polling", async (t) => {
  const { page, calls } = await fixture(t);
  await page.locator('[data-view="diagnostics"]').click();
  await waitText(page, "#diagnostics-summary", "4 checks completed · 3 to review");
  assert.equal(await page.locator(".diagnostic-check").count(), 4);
  for (const status of ["pass", "warn", "fail", "unknown"]) assert.equal(await page.locator(`#diagnostics-${status}`).textContent(), "1");
  await waitText(page, "#diagnostics-list", "Review certificate renewal");
  assert.equal(await page.locator("#workspace-title").textContent(), "Diagnostics");
  await page.clock.fastForward(30000);
  assert.equal(calls.diagnostics, 1);
  assert.equal(calls.commands, 0);
  assert.equal(calls.userChanges.length, 0);
});

test("diagnostics preserves stale results, rejects malformed responses, and recovers", async (t) => {
  const { page, state, calls } = await fixture(t);
  await page.locator('[data-view="diagnostics"]').click();
  await waitText(page, "#diagnostics-summary", "4 checks completed");
  state.diagnosticsFailure = true;
  await page.locator("#diagnostics-refresh").click();
  await waitText(page, "#diagnostics-error", "Previous results");
  assert.equal(await page.locator(".diagnostic-check").count(), 4);
  state.diagnosticsFailure = false;
  state.diagnosticsMalformed = true;
  await page.locator("#diagnostics-refresh").click();
  await waitText(page, "#diagnostics-error", "unexpected diagnostics");
  assert.equal(await page.locator(".diagnostic-check").count(), 4);
  state.diagnosticsMalformed = false;
  await page.locator("#diagnostics-refresh").click();
  await page.locator("#diagnostics-error").waitFor({ state: "hidden" });
  assert.equal(calls.diagnostics, 4);
});

test("diagnostics fits narrow screens and treats server fields as text", async (t) => {
  const { page } = await fixture(t, { viewport: { width: 320, height: 844 }, diagnosticChecks: [
    { id: "fixture", label: '<img src=x onerror="alert(1)">', status: "unknown", detail: "Unrecognized status. ".repeat(12), guidance: "Run the installer diagnostics to investigate." },
  ] });
  await page.locator('[data-view="diagnostics"]').click();
  await waitText(page, "#diagnostics-summary", "1 checks completed");
  assert.equal(await page.locator("#diagnostics-list img").count(), 0);
  assert.match(await page.locator("#diagnostics-list").textContent(), /onerror/);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  if (process.env.UI_SCREENSHOT_DIR) {
    await fs.mkdir(process.env.UI_SCREENSHOT_DIR, { recursive: true });
    await page.screenshot({ path: path.join(process.env.UI_SCREENSHOT_DIR, "diagnostics-mobile.png"), fullPage: true });
  }
});

test("diagnostics blocks duplicate requests and ignores results after sign out", async (t) => {
  const { page, state, calls } = await fixture(t);
  let release;
  state.diagnosticsHold = new Promise((resolve) => { release = resolve; });
  t.after(() => release());
  await page.locator('[data-view="diagnostics"]').click();
  await page.locator("#diagnostics-progress").waitFor({ state: "visible" });
  assert.equal(await page.locator("#diagnostics-refresh").isDisabled(), true);
  await page.locator("#diagnostics-refresh").evaluate((element) => element.click());
  await page.clock.fastForward(30000);
  assert.equal(calls.diagnostics, 1);
  await page.locator("#logout").click();
  await page.locator("#login-view").waitFor({ state: "visible" });
  release();
  await page.waitForTimeout(100);
  assert.equal(await page.locator(".diagnostic-check").count(), 0);
});

test("diagnostics handles unavailable status and session expiry", async (t) => {
  const { page, state } = await fixture(t, { diagnosticsFailure: true });
  await page.locator('[data-view="diagnostics"]').click();
  await waitText(page, "#diagnostics-summary", "unavailable");
  assert.equal(await page.locator("#diagnostics-counts").isVisible(), false);
  state.authenticated = false;
  await page.locator("#diagnostics-refresh").click();
  await page.locator("#login-view").waitFor({ state: "visible" });
});


test("Overview graphs show real API readings and poll only while Overview is visible", async (t) => {
  const { page, calls } = await fixture(t);
  await waitText(page, "#cpu-value", "42.5%");
  await waitText(page, "#memory-value", "62.5%");
  await waitText(page, "#memory-copy", "5 GiB used of 8 GiB");
  await waitText(page, "#network-rx", "1 MiB/s");
  await waitText(page, "#network-tx", "512 KiB/s");
  assert.equal(await page.locator("#network-interface").textContent(), "ens3");
  assert.equal(await page.locator("#cpu-chart .chart-line").count(), 1);
  assert.equal(await page.locator("#network-chart .chart-line").count(), 2);
  assert.match(await page.locator("#cpu-chart").getAttribute("aria-label"), /42.5%/);
  const before = calls.metrics;
  await page.clock.fastForward(5000);
  await page.waitForFunction(() => !document.querySelector("#metrics-refresh").disabled);
  assert.equal(calls.metrics, before + 1);
  if (process.env.UI_SCREENSHOT_DIR) {
    await fs.mkdir(process.env.UI_SCREENSHOT_DIR, { recursive: true });
    await page.screenshot({ path: path.join(process.env.UI_SCREENSHOT_DIR, "resources-desktop.png"), fullPage: true });
  }
  await page.locator('[data-view="users"]').click();
  await waitText(page, "#users-summary", "3 VPN accounts");
  const hiddenCount = calls.metrics;
  await page.clock.fastForward(30000);
  assert.equal(calls.metrics, hiddenCount);
});

test("metrics keep stale readings on errors and recover without enabling service commands", async (t) => {
  const { page, state, calls } = await fixture(t);
  await waitText(page, "#cpu-value", "42.5%");
  state.metricsFailure = true;
  await page.locator("#metrics-refresh").click();
  await waitText(page, "#metrics-error", "Last readings");
  assert.equal(await page.locator("#cpu-value").textContent(), "42.5%");
  assert.equal(await page.locator("#cpu-chart .chart-line").count(), 1);
  state.metricsFailure = false;
  await page.locator("#metrics-refresh").click();
  await page.locator("#metrics-error").waitFor({ state: "hidden" });
  assert.equal(calls.commands, 0);
});

test("metrics distinguish warming up, zero readings, and missing readings on mobile", async (t) => {
  const data = resourceSnapshot();
  data.cpu.percent = null;
  data.network.rx_bytes_per_second = null;
  data.network.tx_bytes_per_second = null;
  data.history = [{ age_seconds: 0, cpu_percent: null, memory_percent: 62.5, interface: "ens3", rx_bytes_per_second: null, tx_bytes_per_second: null }];
  const { page, state } = await fixture(t, { resourceData: data, viewport: { width: 320, height: 844 } });
  await waitText(page, "#cpu-copy", "Collecting CPU");
  assert.equal(await page.locator("#cpu-value").textContent(), "—");
  assert.equal(await page.locator("#cpu-chart-empty").isVisible(), true);
  assert.equal(await page.locator("#metrics-error").isVisible(), false);
  state.resourceData = resourceSnapshot();
  state.resourceData.cpu.percent = 0;
  state.resourceData.network.rx_bytes_per_second = 0;
  await page.locator("#metrics-refresh").click();
  await waitText(page, "#cpu-value", "0%");
  await waitText(page, "#network-rx", "0 B/s");
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  if (process.env.UI_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.UI_SCREENSHOT_DIR, "resources-mobile.png"), fullPage: true });
  state.resourceData.cpu.cores = null;
  state.resourceData.cpu.percent = null;
  state.resourceData.network.rx_bytes = null;
  await page.locator("#metrics-refresh").click();
  await waitText(page, "#metrics-error", "Some server readings");
  await waitText(page, "#cpu-copy", "unavailable");
});

test("metric charts preserve missing-data gaps and never mix interface histories", async (t) => {
  const data = resourceSnapshot();
  data.history = [
    { age_seconds: 30, cpu_percent: 10, memory_percent: 20, interface: "old-interface", rx_bytes_per_second: 999999999, tx_bytes_per_second: 999999999 },
    { age_seconds: 25, cpu_percent: null, memory_percent: 20, interface: "ens3", rx_bytes_per_second: null, tx_bytes_per_second: null },
    { age_seconds: 0, cpu_percent: 30, memory_percent: 25, interface: "ens3", rx_bytes_per_second: 1024, tx_bytes_per_second: 2048 },
  ];
  const { page } = await fixture(t, { resourceData: data });
  await waitText(page, "#cpu-value", "42.5%");
  const path = await page.locator("#cpu-chart .chart-line").getAttribute("d");
  assert.equal((path.match(/M/g) || []).length, 2);
  assert.equal(await page.locator("#network-scale").textContent(), "2 KiB/s");
});

test("metrics reject malformed responses and ignore results from a previous login", async (t) => {
  const { page, state } = await fixture(t);
  await waitText(page, "#cpu-value", "42.5%");
  state.resourceData = { ok: true };
  await page.locator("#metrics-refresh").click();
  await waitText(page, "#metrics-error", "unexpected metrics");
  let release;
  state.metricsHold = new Promise((resolve) => { release = resolve; });
  t.after(() => release());
  await page.locator("#metrics-refresh").click();
  await page.waitForFunction(() => document.querySelector("#metrics-refresh").disabled);
  await page.locator("#logout").click();
  await page.locator("#login-view").waitFor({ state: "visible" });
  release();
  await page.waitForTimeout(100);
  assert.equal(await page.locator("#cpu-chart .chart-line").count(), 0);
  assert.equal(await page.locator("#cpu-value").textContent(), "—");
});


test("configuration QR scans back to the same link and optional password; PNG downloads", async (t) => {
  const { page, state, calls } = await fixture(t, { fixtureCertificate: randomBytes(1450).toString("base64") });
  await page.goto(`${base}/#users`);
  await page.getByRole("button", { name: "Link & QR: alice", exact: true }).click();
  assert.equal(await page.locator("#share-password").isChecked(), false);
  assert.equal(calls.exports.length, 0);
  await page.locator("#share-generate").click();
  await page.locator("#share-result").waitFor({ state: "visible" });
  const uri = await page.locator("#share-link").inputValue();
  assert.equal(uri, state.exportUri);
  const pixels = await page.locator("#share-qr").evaluate((canvas) => ({ width: canvas.width, height: canvas.height,
    data: Array.from(canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data) }));
  const decoded = jsQR(Uint8ClampedArray.from(pixels.data), pixels.width, pixels.height);
  assert.ok(decoded, "The generated QR image must scan successfully");
  assert.equal(decoded.data, uri);
  const envelope = JSON.parse(inflateSync(Buffer.from(uri.split("/").at(-1), "base64url")));
  assert.equal(envelope.profile.username, "alice");
  assert.equal(envelope.password, undefined);
  const downloadPromise = page.waitForEvent("download");
  await page.locator("#share-download").click();
  assert.equal((await downloadPromise).suggestedFilename(), "alice-ikev-qr.png");
  await page.locator("#share-password").check();
  assert.equal(await page.locator("#share-link").inputValue(), "");
  assert.equal(await page.locator("#share-result").isHidden(), true);
  await page.locator("#share-generate").click();
  await page.locator("#share-result").waitFor({ state: "visible" });
  const secretUri = await page.locator("#share-link").inputValue();
  const withPassword = JSON.parse(inflateSync(Buffer.from(secretUri.split("/").at(-1), "base64url")));
  assert.equal(withPassword.password, "browser-fixture-password-only");
  await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
    writeText: async (value) => { window.copiedConfig = value; },
  } }));
  await page.locator("#share-copy-link").click();
  assert.equal(await page.evaluate(() => window.copiedConfig), secretUri);
  await page.locator("#share-close").click();
  assert.equal(await page.locator("#share-link").inputValue(), "");
  assert.equal(await page.locator("#share-qr").getAttribute("width"), "0");
  assert.equal(await page.locator("#share-password").isChecked(), false);
  assert.equal(calls.exports.length, 2);
});

test("closing a pending export discards the delayed sensitive response", async (t) => {
  let release;
  const hold = new Promise((resolve) => { release = resolve; });
  const { page, calls } = await fixture(t, { exportHold: hold });
  await page.goto(`${base}/#users`);
  await page.getByRole("button", { name: "Link & QR: alice", exact: true }).click();
  await page.locator("#share-password").check();
  await page.locator("#share-generate").click();
  await page.waitForFunction(() => document.querySelector("#share-generate").disabled);
  await page.locator("#share-close").click();
  const response = page.waitForResponse((response) => response.url().endsWith("/api/users/export"));
  release();
  await response;
  await page.getByRole("button", { name: "Link & QR: bob", exact: true }).click();
  assert.equal(await page.locator("#share-link").inputValue(), "");
  assert.equal(await page.locator("#share-result").isHidden(), true);
  assert.equal(calls.exports.length, 1);
});

test("export errors and malformed responses leave no shareable output; expired session closes dialog", async (t) => {
  const { page, state } = await fixture(t, { exportFailure: 409 });
  await page.goto(`${base}/#users`);
  await page.getByRole("button", { name: "Link & QR: alice", exact: true }).click();
  await page.locator("#share-generate").click();
  await waitText(page, "#share-error", "Refresh users");
  assert.equal(await page.locator("#share-link").inputValue(), "");
  state.exportFailure = false;
  state.exportMalformed = true;
  await page.locator("#share-generate").click();
  await waitText(page, "#share-error", "Unexpected configuration");
  assert.equal(await page.locator("#share-result").isHidden(), true);
  state.exportMalformed = false;
  state.exportFailure = 401;
  await page.locator("#share-generate").click();
  await page.locator("#login-view").waitFor({ state: "visible" });
  assert.equal(await page.locator("#share-dialog").evaluate((dialog) => dialog.open), false);
  assert.equal(await page.locator("#share-link").inputValue(), "");
});

test("sharing fits mobile screens and clipboard failure selects the link for manual copy", async (t) => {
  const { page } = await fixture(t, { viewport: { width: 375, height: 812 } });
  await page.goto(`${base}/#users`);
  await page.getByRole("button", { name: "Link & QR: alice", exact: true }).click();
  await page.locator("#share-generate").click();
  await page.locator("#share-result").waitFor({ state: "visible" });
  await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined }));
  await page.locator("#share-copy-link").click();
  await waitText(page, "#toast", "manually");
  assert.equal(await page.locator("#share-link").evaluate((input) => input.selectionEnd - input.selectionStart),
    (await page.locator("#share-link").inputValue()).length);
  assert.equal(await page.locator("#share-dialog").evaluate((dialog) => dialog.scrollWidth <= dialog.clientWidth), true);
  await page.keyboard.press("Escape");
  assert.equal(await page.locator("#share-link").inputValue(), "");
});


test("share view clears after five minutes and older brokers disable configuration export", async (t) => {
  const { page, state, calls } = await fixture(t);
  await page.goto(`${base}/#users`);
  await page.getByRole("button", { name: "Link & QR: alice", exact: true }).click();
  await page.locator("#share-generate").click();
  await page.locator("#share-result").waitFor({ state: "visible" });
  await page.clock.fastForward(5 * 60 * 1000 + 1);
  assert.equal(await page.locator("#share-dialog").evaluate((dialog) => dialog.open), false);
  assert.equal(await page.locator("#share-link").inputValue(), "");
  assert.equal(await page.locator("#share-qr").getAttribute("width"), "0");
  delete state.data.capabilities.profile_export;
  await page.goto(base);
  await waitText(page, "#running-services", "2 / 3");
  await page.locator('[data-view="users"]').click();
  assert.equal(await page.getByRole("button", { name: "Link & QR: alice", exact: true }).isDisabled(), true);
  assert.equal(calls.exports.length, 1);
});


test("per-user traffic and first-connection validity allow independent unlimited values", async (t) => {
  const { page, calls } = await fixture(t);
  await page.goto(`${base}/#users`);
  await waitText(page, "#users-list", "Validity: Unlimited");
  await page.getByRole("button", { name: "Limits: alice", exact: true }).click();
  assert.equal(await page.locator("#limit-traffic-unlimited").isChecked(), true);
  assert.equal(await page.locator("#limit-time-unlimited").isChecked(), true);
  await page.locator("#limit-traffic-unlimited").uncheck();
  await page.locator("#limit-time-unlimited").uncheck();
  await page.locator("#limit-traffic").fill("0");
  await page.locator("#limit-time").fill("30");
  await page.locator("#limits-save").click();
  await waitText(page, "#limits-form-error", "positive");
  assert.equal(calls.limitChanges.length, 0);
  await page.locator("#limit-traffic").fill("50");
  await page.locator("#limits-save").click();
  await waitText(page, "#users-list", "50 GiB");
  await waitText(page, "#users-list", "30 days · starts on first connection");
  assert.equal(calls.limitChanges[0].byte_limit, 50 * 1024 ** 3);
  assert.equal(calls.limitChanges[0].validity_seconds, 30 * 86400);
  assert.equal(calls.limitChanges[0].limits_revision, "2".repeat(32));
  assert.equal("password" in calls.limitChanges[0], false);
  await page.getByRole("button", { name: "Limits: alice", exact: true }).click();
  await page.locator("#limit-traffic-unlimited").check();
  await page.locator("#limits-save").click();
  await page.waitForFunction(() => !document.querySelector("#limits-dialog").open);
  assert.equal(calls.limitChanges[1].byte_limit, null);
  assert.equal(calls.limitChanges[1].validity_seconds, 30 * 86400);
});

test("reset traffic and validity requires confirmation and preserves configured limits", async (t) => {
  const { page, state, calls } = await fixture(t, { userLimits: { alice: {
    byte_limit: 10 * 1024 ** 3, validity_seconds: 30 * 86400, used_bytes: 2 * 1024 ** 3,
    first_connected_at: 1792238400, reset_at: 1792238400, blocked: "expired", suspended: true,
  } } });
  await page.goto(`${base}/#users`);
  await waitText(page, "#users-list", "validity expired");
  await page.getByRole("button", { name: "Reset usage: alice", exact: true }).click();
  await waitText(page, "#limits-copy", "next connection");
  await page.locator("#limits-save").click();
  assert.equal(calls.limitChanges.length, 0);
  await page.locator("#limits-reset-confirm").check();
  await page.locator("#limits-save").click();
  await waitText(page, "#toast", "validity reset");
  await waitText(page, "#users-list", "30 days · starts on first connection");
  assert.equal(state.userLimits.alice.byte_limit, 10 * 1024 ** 3);
  assert.equal(state.userLimits.alice.validity_seconds, 30 * 86400);
  assert.equal(state.userLimits.alice.used_bytes, 0);
  assert.equal(state.userLimits.alice.first_connected_at, null);
  assert.deepEqual(Object.keys(calls.limitChanges[0]).sort(), ["limits_revision", "operation", "revision", "username"]);
});

test("limit writes send once, pause polling, and report conflicts without automatic retry", async (t) => {
  let release;
  const hold = new Promise((resolve) => { release = resolve; });
  const { page, calls, state } = await fixture(t, { limitChangeHold: hold });
  await page.goto(`${base}/#users`);
  await page.getByRole("button", { name: "Limits: alice", exact: true }).click();
  await page.locator("#limits-save").click();
  await page.waitForFunction(() => document.querySelector("#limits-save").disabled);
  await page.locator("#limits-form").evaluate((form) => { form.requestSubmit(); form.requestSubmit(); });
  await page.clock.fastForward(31000);
  assert.equal(calls.limitChanges.length, 1);
  release();
  await page.waitForFunction(() => !document.querySelector("#limits-dialog").open);
  state.limitChangeHold = null;
  state.limitChangeFailure = 409;
  await page.getByRole("button", { name: "Limits: alice", exact: true }).click();
  await page.locator("#limits-save").click();
  await waitText(page, "#toast", "not confirmed");
  assert.equal(calls.limitChanges.length, 2);
});

test("limits fit mobile and stale accounting and expired sessions are visible", async (t) => {
  const { page, state } = await fixture(t, { viewport: { width: 375, height: 812 }, limitsHealthy: false });
  await page.goto(`${base}/#users`);
  await page.locator("#limits-notice").waitFor({ state: "visible" });
  await page.getByRole("button", { name: "Limits: alice", exact: true }).click();
  assert.equal(await page.locator("#limits-dialog").evaluate((dialog) => dialog.scrollWidth <= dialog.clientWidth), true);
  await page.keyboard.press("Escape");
  state.limitChangeFailure = 401;
  await page.getByRole("button", { name: "Limits: alice", exact: true }).click();
  await page.locator("#limits-save").click();
  await page.locator("#login-view").waitFor({ state: "visible" });
  assert.equal(await page.locator("#limits-dialog").evaluate((dialog) => dialog.open), false);
});

test("older brokers disable limits and reset controls", async (t) => {
  const data = snapshot();
  delete data.capabilities.user_limits;
  const { page, calls } = await fixture(t, { data });
  await page.goto(`${base}/#users`);
  const button = page.getByRole("button", { name: "Limits: alice", exact: true });
  await button.waitFor();
  assert.equal(await button.isDisabled(), true);
  assert.equal(await page.getByRole("button", { name: "Reset usage: alice", exact: true }).isDisabled(), true);
  assert.equal(calls.limitChanges.length, 0);
});
