// Optional browser checks: use a local static server and mocked API only.
const { test, before, after } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");
const { firefox } = require("playwright");

let browser;
let server;
let base;
const staticRoot = path.resolve(__dirname, "../webui/static");
const snapshot = () => ({
  installed: true,
  capabilities: { service_actions: true, user_management: true },
  services: [
    { id: "ikev2", label: "IKEv2 / StrongSwan", unit: "strongswan-starter.service", state: "active" },
    { id: "firewall", label: "Firewall & NAT", unit: "ikev2-vpn-firewall.service", state: "active" },
    { id: "proxy", label: "SOCKS5 Proxy", unit: "ikev2-vpn-proxy.service", state: "inactive" },
  ],
});

before(async () => {
  server = http.createServer(async (request, response) => {
    const assets = { "/": ["index.html", "text/html"], "/assets/app.js": ["app.js", "text/javascript"], "/assets/app.css": ["app.css", "text/css"] };
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
  const calls = { dashboard: 0, commands: 0, logout: 0, sessions: 0, diagnostics: 0, userChanges: [] };
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
      return json({ ok: true, users: names.slice(page * 20, page * 20 + 20).map((username) => ({ username })),
        total: state.userNames.length, matched: names.length, page, pages: Math.max(1, Math.ceil(names.length / 20)), revision: "1".repeat(64) });
    }
    assert.equal(request.headers()["x-csrf-token"], "browser-test-csrf");
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
