// Memento desktop — vanilla JS, no framework.
// Uses Tauri 2.x's window.__TAURI__ globals to call Rust commands.
// `withGlobalTauri: true` in tauri.conf.json enables this; without it
// every line below would throw at module load and silently break every
// button on the page (no event listeners ever get registered).

if (!window.__TAURI__) {
  document.body.innerHTML =
    '<div style="padding:40px;font:14px/1.5 system-ui">' +
    '<h2>Tauri runtime not detected.</h2>' +
    '<p>Open this app via the Memento installer, not by opening dist/index.html ' +
    'directly. If you built from source, ensure tauri.conf.json has ' +
    '<code>app.withGlobalTauri = true</code>.</p></div>';
  throw new Error("window.__TAURI__ undefined — was the page opened outside Tauri?");
}

import { t, apply as applyI18n } from "./i18n.js";
applyI18n();  // walk DOM, replace data-i18n attrs with the active locale's strings

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;
const { open: openDialog } = window.__TAURI__.dialog;
const tauriShell = window.__TAURI__.shell;  // tauri-plugin-shell .open(url)
const tauriApp = window.__TAURI__.app;      // .getVersion()
const tauriUpdater = window.__TAURI__.updater; // .check() — Tauri auto-update
const tauriProcess = window.__TAURI__.process; // .relaunch()

// How often a running instance re-checks for a new release. The app sits in
// the tray for weeks, so this — not the check at startup — is what actually
// delivers updates to anyone who doesn't reboot often.
const UPDATE_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;  // 6 hours

// Same set the Python collector knows about. Keep names in sync with
// collector/collector/tools/*.py — these are the values used to index
// the disabled_tools list in config.
const TOOLS = [
  { id: "claude_code",  name: "Claude Code",   desc: "~/.claude.json + ~/.claude/projects/*.jsonl" },
  { id: "codex",        name: "Codex",         desc: "~/.codex/sessions/*.jsonl" },
  { id: "cursor",       name: "Cursor",        desc: "~/Library/Application Support/Cursor (or AppData)" },
  { id: "openclaw",     name: "OpenClaw",      desc: "~/.openclaw/workspace/conversations" },
  { id: "antigravity",  name: "Antigravity",   desc: "~/Library/Application Support/antigravity" },
  { id: "obsidian",     name: "Obsidian",      desc: "vault path set on Server tab" },
  { id: "hermes",       name: "Hermes Agent",  desc: "~/.hermes" },
];

const $ = (sel) => document.querySelector(sel);

let state = {
  config: null,
  status: { running: false },
  // Which server the dashboard iframe is currently loaded for, and whether a
  // JWT mint is in flight for it. Both gate iframe.src assignment — see
  // openDashboard().
  dashboardLoadedFor: null,
  dashboardMinting: false,
  // A github_login (system-browser OAuth + 127.0.0.1 loopback) is running.
  // The login page's button can be clicked again before the Rust side
  // resolves; a second run would open a second browser window and bind a
  // second listener. One at a time.
  githubLoginInFlight: false,
};

// ─── Tabs ──────────────────────────────────────────────────────────
function activateTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  const tab = document.querySelector(`.tab[data-tab="${name}"]`);
  const panel = document.querySelector(`.panel[data-panel="${name}"]`);
  if (tab) tab.classList.add("active");
  if (panel) panel.classList.add("active");
  // Dashboard runs in fullscreen mode (hides topbar + tabs) ONLY when a
  // server is actually configured and the iframe is shown. In the empty
  // state we must keep the tabs visible — otherwise the back button (which
  // lives inside the hidden iframe toolbar) is gone too and the user is
  // trapped with no way back to the Server tab. openDashboard() owns the
  // fullscreen decision; every other tab exits fullscreen.
  if (name === "dashboard") {
    // Fire-and-forget: openDashboard() is async (it mints a web JWT). Nothing
    // here depends on its result, but swallow rejections so a failed mint can
    // never surface as an unhandled promise rejection.
    openDashboard().catch(() => {});
  } else {
    document.body.classList.remove("dashboard-fullscreen");
  }
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => activateTab(tab.dataset.tab));
});

document.getElementById("dashboardBackToSettings")?.addEventListener("click", () => {
  activateTab("server");
});
document.getElementById("dashboardEmptyToServer")?.addEventListener("click", () => {
  activateTab("server");
});

// ─── Dashboard tab (embedded webview) ─────────────────────────────
// The user only configures ONE URL on the Server tab — the API base, since
// that's what gets paired with the collector token. The Dashboard tab needs
// the WEB UI URL though, which may or may not be the same depending on the
// deployment topology. Heuristics, in order:
//
//   1. API on :8001  → web on :3001         (docker-compose default ports)
//   2. API ends /api → web at the parent    (nginx subpath proxy)
//   3. otherwise     → same base            (nginx unified domain, e.g.
//                                            mem.ihasy.com serves both /app
//                                            and /api/* from one origin)
//
// Two-URL config would let the user override this, but for the common cases
// the derivation gets it right and keeps the Server tab to just one field.
function deriveWebUrl(apiUrl) {
  const base = apiUrl.trim().replace(/\/$/, "");
  if (/:8001(\/|$)/.test(base)) return base.replace(/:8001/, ":3001");
  if (/\/api(\/|$)/.test(base)) return base.replace(/\/api\/?$/, "");
  return base;
}

// Pick the URL to load the dashboard iframe at.
//
// The collector token in state.config survives app updates; the WebView's
// localStorage (where the web app keeps its JWT under "dr_token") does NOT —
// an update/reinstall wipes it. So we mint a fresh web JWT from the durable
// collector token on every dashboard open and hand it to the web app via
// /auth/handoff, which stores it and navigates on to `next` itself. Minting
// every time also means an expired JWT self-heals; there is nothing to cache.
//
// The JWT goes in the URL FRAGMENT, never the query string: fragments are not
// sent to the server, so the token stays out of access logs and Referer.
//
// CONSTRAINT: the caller must assign iframe.src exactly ONCE from this result
// and must never force a follow-up reload. /auth/handoff performs its own
// window.location.replace(); a second navigation races it and logs the user
// straight back out (that regression looked like "logged out on every other
// refresh").
async function dashboardUrlFor(apiUrl, token, webBase) {
  // Never logged in on this device — no collector token to exchange. Go to the
  // login page directly rather than /app, which would flash "Failed to load
  // dashboard" while AuthProvider redirects.
  if (!token) return `${webBase}/auth/login?embed=memento&next=/app`;
  try {
    const jwt = await invoke("mint_web_token", {
      serverUrl: apiUrl,
      collectorToken: token,
    });
    const p = new URLSearchParams({ token: jwt, next: "/app?embed=memento" });
    return `${webBase}/auth/handoff#${p.toString()}`;
  } catch (e) {
    // Offline, server down, or the token was revoked. Fall back to /app so a
    // user whose iframe storage is still intact gets in anyway; if it isn't,
    // the web app redirects to its own login. Not worth alarming anyone over.
    console.warn("mint_web_token failed, falling back to iframe-local auth:", e);
    return `${webBase}/app?embed=memento`;
  }
}

async function openDashboard() {
  const apiUrl = (state.config?.server_url || "").trim();
  const token = (state.config?.server_token || "").trim();
  const empty = document.getElementById("dashboardEmpty");
  const frame = document.getElementById("dashboardFrame");
  const iframe = document.getElementById("dashboardIframe");
  const urlEl = document.getElementById("dashboardUrl");
  if (!apiUrl) {
    empty.style.display = "block";
    frame.classList.add("hidden");
    // Not configured yet — stay windowed so the tabs remain reachable.
    document.body.classList.remove("dashboard-fullscreen");
    return;
  }
  empty.style.display = "none";
  frame.classList.remove("hidden");
  document.body.classList.add("dashboard-fullscreen");
  const webBase = deriveWebUrl(apiUrl);
  // Show the clean /app URL (never the token-bearing handoff URL).
  urlEl.textContent = `${webBase}/app`;

  // Already loaded for this server — don't re-mint + reload every time
  // the user flips back to the Dashboard tab.
  if (state.dashboardLoadedFor === apiUrl && iframe.src && iframe.src !== "about:blank") {
    return;
  }

  // dashboardLoadedFor alone can't gate this: it's only set AFTER the mint,
  // and the mint is a network round-trip (up to 20s). A second entry during
  // that window — impatient double-click on the tab, or the post-Save
  // setTimeout landing on top of the startup call — would sail past the guard
  // above and assign iframe.src a second time, racing the first hand-off's
  // location.replace(). That is exactly the two-navigations bug that used to
  // log people out on every other refresh. One mint in flight at a time.
  if (state.dashboardMinting) return;
  state.dashboardMinting = true;
  try {
    // ?embed=memento makes the web post the collector token back to us on the
    // next in-iframe login event; older web builds just ignore it.
    iframe.src = await dashboardUrlFor(apiUrl, token, webBase);
    state.dashboardLoadedFor = apiUrl;
  } finally {
    state.dashboardMinting = false;
  }
}

// Force the dashboard iframe to reload with a freshly minted session.
//
// Never contentWindow.location.reload(): the iframe is cross-origin with the
// Tauri parent (allow-same-origin makes it same-origin with the *server*, not
// with us), so touching it throws SecurityError. Reassigning src is the only
// way to reload it.
//
// Going through dashboardUrlFor() re-mints the web JWT, so an expired session
// heals itself. This is only ever called for an explicit user action (the
// reload button, or a completed out-of-frame GitHub sign-in), so it
// deliberately bypasses the dashboardLoadedFor guard — but it still honors
// dashboardMinting, so hammering it can't put two navigations in flight at
// once, and it assigns iframe.src exactly ONCE (see dashboardUrlFor's
// constraint: a second navigation races /auth/handoff's location.replace()
// and logs the user straight back out).
async function reloadDashboardFrame() {
  const iframe = document.getElementById("dashboardIframe");
  const apiUrl = (state.config?.server_url || "").trim();
  const token = (state.config?.server_token || "").trim();
  if (!apiUrl || state.dashboardMinting) return;
  const webBase = deriveWebUrl(apiUrl);
  state.dashboardMinting = true;
  try {
    iframe.src = await dashboardUrlFor(apiUrl, token, webBase);
    state.dashboardLoadedFor = apiUrl;
  } finally {
    state.dashboardMinting = false;
  }
}

document.getElementById("dashboardReload")?.addEventListener("click", () => {
  reloadDashboardFrame().catch((e) => console.warn("dashboard reload:", e));
});

document.getElementById("dashboardOpenExternal")?.addEventListener("click", async () => {
  const url = document.getElementById("dashboardUrl").textContent;
  if (!url) return;
  try {
    // tauri-plugin-shell's `open` opens with the OS default browser.
    if (tauriShell?.open) {
      await tauriShell.open(url);
    } else {
      window.open(url, "_blank");
    }
  } catch (e) {
    console.warn("openExternal failed", e);
  }
});

// ─── Initial load ─────────────────────────────────────────────────
async function boot() {
  try {
    state.config = await invoke("load_config");
  } catch (e) {
    flash("err", "Failed to load config: " + e.message);
    state.config = {};
  }
  fillForm(state.config);
  renderToolList();

  try {
    const legacy = await invoke("detect_legacy_install");
    if (legacy) {
      $("#legacyWarning").classList.remove("hidden");
    }
  } catch { /* non-fatal */ }

  try {
    const ok = await invoke("sidecar_available");
    if (!ok) {
      $("#sidecarMissing").classList.remove("hidden");
    }
  } catch { /* non-fatal */ }

  try {
    state.status = await invoke("sidecar_status");
    renderStatus();
  } catch { /* sidecar may not have started yet */ }

  // Hydrate log view with whatever the Rust side has buffered.
  try {
    const lines = await invoke("sidecar_log_snapshot");
    if (lines.length) $("#logView").textContent = lines.join("\n") + "\n";
  } catch { /* fine */ }

  // Live updates from the sidecar process.
  await listen("sidecar:status", (e) => {
    state.status = e.payload;
    renderStatus();
  });
  await listen("sidecar:log", (e) => {
    appendLog(e.payload);
  });

  // If already configured, drop the user straight on the dashboard.
  // First-time setup keeps them on Server tab so they fill in fields.
  if (state.config?.server_url && state.config?.server_token) {
    activateTab("dashboard");
  }

  // Fire-and-forget update check. Failures (no network, rate-limited
  // GitHub API) are silent — banner just won't appear.
  checkForUpdate().catch(() => {});

  // ...and keep checking. Memento lives in the tray for weeks at a time, and
  // the webview is never reloaded while it does, so a one-shot check at
  // startup means a long-running instance never learns about anything
  // published after it launched. (Observed: an instance up for 7 days sat
  // through four releases without a peep.)
  setInterval(() => { checkForUpdate().catch(() => {}); }, UPDATE_CHECK_INTERVAL_MS);

  // Tray menu "Check for updates" → Rust emits this. Clear the session
  // dismissal and force a fresh check; if no update, show a brief notice
  // so the user gets feedback instead of silence.
  await listen("menu:check-update", async () => {
    sessionStorage.removeItem("update_dismissed");
    try {
      const update = await tauriUpdater.check();
      if (update?.available) {
        await checkForUpdate();
      } else {
        const v = await tauriApp.getVersion();
        flash("ok", `Memento v${v} · ${t("update.upToDate")}`);
      }
    } catch (e) {
      flash("err", String(e?.message || e));
    }
  });

  // Tray menu "Reconfigure MCP" → user-driven rewrite of every AI tool's
  // memento-memory entry. Same code path as the auto-refresh on app
  // upgrade, but flashes a visible result in the Server tab.
  await listen("menu:reconfig-mcp", async () => {
    activateTab("server");
    const apiUrl = (state.config?.server_url || "").trim();
    const token = (state.config?.server_token || "").trim();
    if (!apiUrl || !token) {
      flash("err", t("mcp.needConfig"));
      return;
    }
    try {
      const report = await invoke("configure_mcp", {
        serverUrl: apiUrl,
        serverToken: token,
      });
      const which = report.configured?.length
        ? ` · ${t("save.mcpConfigured")} ${report.configured.join(", ")}`
        : "";
      flash("ok", `${t("mcp.reconfigured")}${which}`);
    } catch (e) {
      flash("err", e?.message || String(e));
    }
  });
}

// ─── Update check (Tauri auto-updater) ────────────────────────────
// Uses tauri-plugin-updater under the hood:
//   1. Fetches https://github.com/.../releases/latest/download/latest.json
//   2. Verifies the bundled .sig with our embedded ed25519 pubkey
//   3. Downloads the signed NSIS installer to a temp dir
//   4. Runs the installer; on Windows it self-terminates the running app,
//      replaces files, then we call relaunch()
async function checkForUpdate() {
  let update;
  try {
    update = await tauriUpdater.check();
  } catch (e) {
    console.warn("Update check failed:", e);
    return;  // network down / signature invalid / endpoint 404 — silent
  }
  if (!update?.available) return;

  // Dismissal is remembered per *version*, and deliberately checked after the
  // fetch rather than before it: brushing off v1 must not also silence v2.
  // (It used to store a bare "1", so one "Later" click muted every future
  // release for the life of the process — which, for a tray app, is forever.)
  if (sessionStorage.getItem("update_dismissed") === update.version) return;

  // Mount the banner first so the user can dismiss without confirming.
  showUpdateBanner(update.version, async () => {
    if (!confirm(`Install Memento v${update.version} now? The app will restart automatically.`)) return;
    const banner = document.getElementById("updateBanner");
    banner.classList.add("hidden");
    const status = document.createElement("div");
    status.className = "update-banner";
    status.textContent = `Downloading v${update.version}…`;
    document.body.insertBefore(status, document.body.firstChild);
    try {
      // Stop the sidecar BEFORE the installer runs — otherwise the old
      // Python sidecar process keeps the install dir open on Windows,
      // the new installer can't fully replace files, and you end up with
      // two memento-app.exe processes (one zombie, one new) leaving two
      // tray icons in the systray.
      try { await invoke("sidecar_stop"); } catch { /* sidecar wasn't running */ }
      await update.downloadAndInstall((event) => {
        // Tauri 2.x emits {event: 'Started'|'Progress'|'Finished', ...}
        if (event.event === "Progress") {
          status.textContent =
            `Downloading v${update.version}: ${(event.data.chunkLength / 1024 / 1024).toFixed(1)} MB`;
        } else if (event.event === "Finished") {
          status.textContent = `Installed v${update.version} — restarting…`;
        }
      });
      await tauriProcess.relaunch();
    } catch (e) {
      status.textContent = `Update failed: ${e?.message || e}`;
    }
  });
}

function showUpdateBanner(version, onInstall) {
  const banner = document.getElementById("updateBanner");
  document.getElementById("updateBannerVersion").textContent = `v${version}`;
  const link = document.getElementById("updateBannerDownload");
  link.textContent = "Install";
  link.href = "#";
  link.onclick = (e) => { e.preventDefault(); onInstall(); };
  document.getElementById("updateBannerDismiss").onclick = () => {
    banner.classList.add("hidden");
    sessionStorage.setItem("update_dismissed", version);
  };
  banner.classList.remove("hidden");
}

// Default to the public hosted instance so the common path is:
// register on mem.ihasy.com → copy token → open app (URL already
// filled) → paste token → Save. Self-hosters just overwrite it.
const DEFAULT_SERVER_URL = "https://mem.ihasy.com";

function fillForm(cfg) {
  $("#serverUrl").value = cfg.server_url || DEFAULT_SERVER_URL;
  $("#autoStartDaemon").checked = cfg.auto_start_daemon ?? true;
  $("#autostart").checked = cfg.autostart ?? true;
}

// User-friendly: they probably paste the Memento URL they use in the
// browser (port 3001), but the Python collector needs the API base
// (port 8001). Normalize on save so the daemon always gets the API URL.
// Reverses deriveWebUrl()'s direction.
function normalizeApiUrl(input) {
  const base = (input || "").trim().replace(/\/$/, "");
  if (/:3001(\/|$)/.test(base)) return base.replace(/:3001/, ":8001");
  return base;
}

function readForm() {
  const normalized = normalizeApiUrl($("#serverUrl").value);
  // Reflect the normalized URL back into the input so users see what's
  // actually stored. Avoids confusion next time they open Settings.
  if (normalized && normalized !== $("#serverUrl").value.trim()) {
    $("#serverUrl").value = normalized;
  }
  return {
    server_url: normalized,
    // Token is never shown/edited in the UI anymore — it's assigned
    // invisibly on register/login and only lives in the saved config.
    server_token: state.config?.server_token || "",
    // obsidian_vault_path: removed from UI — collector now auto-discovers
    // the user's vault from obsidian.json. Keep the value if it was set
    // previously (advanced override) so existing configs aren't clobbered.
    obsidian_vault_path: state.config?.obsidian_vault_path || "",
    auto_start_daemon: $("#autoStartDaemon").checked,
    autostart: $("#autostart").checked,
    disabled_tools: state.config?.disabled_tools || [],
  };
}

// ─── Server tab — only action is Save ─────────────────────────────
// The minimal flow: user fills the server URL (+ optional toggles) and
// hits Save. Save persists the config + jumps to the Dashboard tab,
// where the embedded web login/register happens. Once the user logs in
// inside that iframe the web posts the collector token back to the
// desktop via window.postMessage (see the listener below), and only
// then is the collector daemon configured + started.
$("#saveBtn").addEventListener("click", async () => {
  const cfg = readForm();
  try {
    await invoke("save_config", { cfg });
    state.config = cfg;
    flash("ok", t("save.ok"));
    // Always go to the dashboard — first-time users will see the web
    // login page in the iframe; returning users with a saved token will
    // be SSO'd straight in. Either way, this tab is "done".
    setTimeout(() => activateTab("dashboard"), 300);
  } catch (e) {
    flash("err", e.message);
  }
});

// ─── Adopting a collector token ───────────────────────────────────
// The collector token is this app's whole identity: it's what the daemon
// syncs with, what configure_mcp writes into every AI tool's MCP entry,
// and what mint_web_token exchanges for a web session. Whichever way we
// got hold of one (in-iframe login, or the out-of-frame GitHub loopback),
// the post-auth work is the same — so it lives here, once.
//
// `refreshDashboard` is the ONE difference between the two callers; see the
// comments at the bottom. Getting it wrong reintroduces the double-navigation
// bug that used to log people out on every other refresh.
async function adoptCollectorToken(token, { refreshDashboard = false } = {}) {
  state.config = { ...(state.config || {}), server_token: token };
  const cfg = readForm();
  await invoke("save_config", { cfg });
  state.config = cfg;
  try {
    await invoke("configure_mcp", {
      serverUrl: cfg.server_url,
      serverToken: cfg.server_token,
    });
  } catch (e) { console.warn("configure_mcp:", e); }
  // Replace any running collector with one that uses the new token.
  try { await invoke("sidecar_stop"); } catch (e) { console.warn("sidecar_stop:", e); }
  try { await invoke("sidecar_start"); } catch (e) { console.warn("sidecar_start:", e); }

  if (refreshDashboard) {
    // GitHub loopback path: the iframe never navigated (the OAuth dance
    // happened in the system browser), so it's still parked on the login
    // page with no navigation of its own in flight — there is nothing to
    // race. Reload it exactly once, through the dashboardMinting-guarded
    // helper, so it comes back up already signed in via /auth/handoff.
    await reloadDashboardFrame();
  } else {
    // In-iframe login path: do NOT touch the iframe here — the user already
    // authenticated inside it and is mid-navigation to /app. Force-reloading
    // via the SSO handoff would race that navigation and look like a logout
    // flash. The handoff path is reserved for the next time the user opens
    // the Dashboard tab fresh (state.dashboardLoadedFor !== url).
    // Remember the current load so the tab-flip skip still works.
    state.dashboardLoadedFor = (state.config?.server_url || "").trim();
  }
}

// ─── Messages from the embedded dashboard ─────────────────────────
// Two things the framed web app asks of us, both origin-guarded against the
// configured web origin (deriveWebUrl maps the saved API base to it):
//
//   memento:token         — the user logged in / registered *inside* the
//                           iframe and the web handed us the collector token.
//                           The only place this app learns the token at all.
//   memento:github-login  — the user clicked "Continue with GitHub". That
//                           can't run in-frame (github.com/login/oauth/authorize
//                           sends `x-frame-options: deny`), so the page hands
//                           the click to us and we run the standard desktop
//                           OAuth flow: system browser + a one-shot 127.0.0.1
//                           loopback listener that delivers the collector
//                           token back (see src-tauri/src/auth.rs).
window.addEventListener("message", async (evt) => {
  const data = evt.data;
  if (!data || (data.type !== "memento:token" && data.type !== "memento:github-login")) return;
  const apiUrl = (state.config?.server_url || "").trim();
  if (!apiUrl) return;
  // Origin guard: only accept messages from the configured web origin. This
  // matters just as much for github-login as for the token — without it any
  // page we happened to frame could kick off a sign-in flow.
  let expectedOrigin = "";
  try { expectedOrigin = new URL(deriveWebUrl(apiUrl)).origin; } catch { /* invalid url */ }
  if (!expectedOrigin || evt.origin !== expectedOrigin) {
    console.warn(`${data.type} from unexpected origin`, evt.origin);
    return;
  }

  if (data.type === "memento:token") {
    if (!data.collector_token) return;
    try {
      await adoptCollectorToken(data.collector_token);
    } catch (e) {
      console.warn("memento:token handler:", e);
    }
    return;
  }

  // memento:github-login — the button is disabled in the page once it posts,
  // but a stale/duplicated message must not spawn a second browser window and
  // a second loopback listener.
  if (state.githubLoginInFlight) return;
  state.githubLoginInFlight = true;
  try {
    // Blocks until the user finishes in their browser and the loopback
    // listener gets the token (or the Rust side times out after 5 minutes /
    // rejects a bad nonce).
    const res = await invoke("github_login", { serverUrl: apiUrl });
    if (!res?.collector_token) throw new Error(t("auth.githubFailed"));
    await adoptCollectorToken(res.collector_token, { refreshDashboard: true });
    flash("ok", t("auth.okLoggedIn"));
  } catch (e) {
    console.warn("github_login:", e);
    // CmdError serializes as { message }; a thrown JS Error has .message too.
    flash("err", e?.message || String(e) || t("auth.githubFailed"));
    // Abandoned / timed-out / failed: the login page left its GitHub button
    // disabled ("continue in your browser…"), so leaving the frame as-is would
    // wedge the user with no way to retry. Reload it back to a fresh login
    // page. Guarded + single-assignment like every other src write.
    try {
      await reloadDashboardFrame();
    } catch (e2) {
      console.warn("dashboard reload after failed github_login:", e2);
    }
  } finally {
    state.githubLoginInFlight = false;
  }
});

// (Obsidian path picker removed in v0.1.9 — the collector reads
// obsidian.json on startup and auto-discovers the user's most recent
// vault. Set MEMENTO_OBSIDIAN_VAULT_PATH env to override.)

// ─── Tools tab ────────────────────────────────────────────────────
function renderToolList() {
  const ul = $("#toolList");
  ul.innerHTML = "";
  const disabled = new Set(state.config?.disabled_tools || []);
  for (const t of TOOLS) {
    const li = document.createElement("li");
    li.innerHTML = `
      <label class="toggle inline">
        <input type="checkbox" data-tool="${t.id}" ${disabled.has(t.id) ? "" : "checked"} />
        <span></span>
      </label>
      <div>
        <div class="tool-name">${t.name}</div>
        <div class="tool-desc">${t.desc}</div>
      </div>
    `;
    ul.appendChild(li);
  }
}

$("#saveToolsBtn").addEventListener("click", async () => {
  const disabled = [...document.querySelectorAll('.tool-list input[type="checkbox"]')]
    .filter((cb) => !cb.checked)
    .map((cb) => cb.dataset.tool);
  state.config = { ...(state.config || {}), ...readForm(), disabled_tools: disabled };
  try {
    await invoke("save_config", { cfg: state.config });
    flash("ok", "Tools updated. Restart collector to apply.");
  } catch (e) {
    flash("err", e.message);
  }
});

// ─── Status pill ──────────────────────────────────────────────────
function renderStatus() {
  const pill = $("#statusPill");
  const text = $("#statusText");
  pill.classList.remove("running", "error", "idle");
  if (state.status?.running) {
    pill.classList.add("running");
    text.textContent = t("status.running");
  } else if (state.status?.last_error) {
    pill.classList.add("error");
    text.textContent = t("status.error");
  } else {
    pill.classList.add("idle");
    text.textContent = t("status.idle");
  }
  $("#daemonInfo").textContent = state.status?.running
    ? `${t("status.running")} · PID ${state.status.pid}`
    : t("status.idle");
}

// ─── Logs tab ─────────────────────────────────────────────────────
function appendLog(line) {
  const view = $("#logView");
  view.textContent += line + "\n";
  // Cap to ~2000 lines to keep DOM cheap.
  const lines = view.textContent.split("\n");
  if (lines.length > 2000) {
    view.textContent = lines.slice(-2000).join("\n");
  }
  if ($("#autoscroll").checked) view.scrollTop = view.scrollHeight;
}

$("#clearLogBtn").addEventListener("click", () => {
  $("#logView").textContent = "";
});

// ─── Notice helper ────────────────────────────────────────────────
function flash(tone, msg) {
  const n = $("#serverNotice");
  n.classList.remove("ok", "err");
  n.classList.add(tone);
  n.textContent = msg;
  setTimeout(() => {
    n.textContent = "";
    n.classList.remove("ok", "err");
  }, 3000);
}

boot();
