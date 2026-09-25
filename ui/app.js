/* SYZYGY Mission Control V0.1 — browser renderer only.
 * Reads /api/snapshot and /api/events. Never probes MCP/hosts/Cloudflare.
 */
(function () {
  "use strict";

  const REFRESH_MS = 7000;
  const ACTIVITY_LIMIT = 20;
  const TZ = "America/Los_Angeles";

  const els = {
    clock: document.getElementById("clock"),
    refreshBadge: document.getElementById("refresh-badge"),
    fetchError: document.getElementById("fetch-error"),
    gStatus: document.getElementById("g-status"),
    gHeartbeat: document.getElementById("g-heartbeat"),
    gAge: document.getElementById("g-age"),
    gRun: document.getElementById("g-run"),
    gTrace: document.getElementById("g-trace"),
    sysStatus: document.getElementById("sys-status"),
    sysReason: document.getElementById("sys-reason"),
    nodes: document.getElementById("nodes"),
    nodesCount: document.getElementById("nodes-count"),
    services: document.getElementById("services"),
    servicesCount: document.getElementById("services-count"),
    paths: document.getElementById("paths"),
    pathsCount: document.getElementById("paths-count"),
    auth: document.getElementById("auth"),
    authSummary: document.getElementById("auth-summary"),
    activityBody: document.getElementById("activity-body"),
    activityCount: document.getElementById("activity-count"),
    servedMeta: document.getElementById("served-meta"),
  };

  function normStatus(raw) {
    if (raw == null || raw === "") return "UNKNOWN";
    const s = String(raw).trim().toLowerCase();
    if (s === "green" || s === "pass" || s === "ok") return "GREEN";
    if (s === "yellow" || s === "warn" || s === "warning") return "YELLOW";
    if (s === "red" || s === "fail" || s === "failed" || s === "error") return "RED";
    return "UNKNOWN";
  }

  function setChip(el, status) {
    const label = normStatus(status);
    el.textContent = label;
    el.className = "status-chip " + label;
  }

  function fmtPT(iso) {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return String(iso);
      return new Intl.DateTimeFormat("en-US", {
        timeZone: TZ,
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }).format(d) + " PT";
    } catch (_) {
      return String(iso);
    }
  }

  function fmtAge(seconds) {
    if (seconds == null || Number.isNaN(Number(seconds))) return "—";
    const s = Math.max(0, Math.round(Number(seconds)));
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60);
    const r = s % 60;
    if (m < 60) return m + "m " + r + "s";
    const h = Math.floor(m / 60);
    return h + "h " + (m % 60) + "m";
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function asList(value, idKey) {
    if (Array.isArray(value)) return value;
    if (value && typeof value === "object") {
      return Object.keys(value).map(function (k) {
        const item = Object.assign({}, value[k]);
        if (item.id == null) item.id = k;
        return item;
      });
    }
    return [];
  }

  function layerStatus(layer) {
    if (layer == null) return "UNKNOWN";
    if (typeof layer === "string") return normStatus(layer);
    if (typeof layer === "object") return normStatus(layer.status);
    return "UNKNOWN";
  }

  function renderLayers(layers) {
    if (!layers || typeof layers !== "object") return "";
    const keys = Object.keys(layers);
    if (!keys.length) return "";
    return (
      '<div class="layers">' +
      keys
        .map(function (name) {
          const st = layerStatus(layers[name]);
          return '<span class="layer-chip ' + st + '">' + esc(name) + ":" + st + "</span>";
        })
        .join("") +
      "</div>"
    );
  }

  function fmtBytes(n) {
    if (n == null || n === "") return "—";
    const v = Number(n);
    if (Number.isNaN(v)) return esc(n);
    const units = ["B", "KB", "MB", "GB", "TB"];
    let x = Math.max(0, v);
    let i = 0;
    while (x >= 1024 && i < units.length - 1) {
      x /= 1024;
      i += 1;
    }
    return (i === 0 ? String(Math.round(x)) : x.toFixed(1)) + " " + units[i];
  }

  function renderMetrics(metrics) {
    if (!metrics || typeof metrics !== "object") return "";
    const rows = [];
    if (metrics.cpu_load_1m != null || metrics.cpu_load != null) {
      rows.push(["CPU load", metrics.cpu_load_1m != null ? metrics.cpu_load_1m : metrics.cpu_load]);
    }
    if (metrics.ram_available_bytes != null) rows.push(["RAM free", fmtBytes(metrics.ram_available_bytes)]);
    if (metrics.mem_used_pct != null) rows.push(["RAM used %", metrics.mem_used_pct]);
    if (metrics.disk_free_bytes != null) rows.push(["Disk free", fmtBytes(metrics.disk_free_bytes)]);
    if (metrics.disk_used_pct != null) rows.push(["Disk used %", metrics.disk_used_pct]);
    if (metrics.temperature_c != null) rows.push(["Temp °C", metrics.temperature_c]);
    if (metrics.temp_c != null) rows.push(["Temp °C", metrics.temp_c]);
    if (metrics.temps_c && typeof metrics.temps_c === "object") {
      Object.keys(metrics.temps_c).forEach(function (k) {
        rows.push(["Temp " + k, metrics.temps_c[k]]);
      });
    }
    if (metrics.gpu_utilization_percent != null) {
      rows.push(["GPU %", metrics.gpu_utilization_percent === null ? "null" : metrics.gpu_utilization_percent]);
    } else if ("gpu_utilization_percent" in metrics) {
      rows.push(["GPU %", "null"]);
    } else if (metrics.gpu_load != null) {
      rows.push(["GPU load", metrics.gpu_load]);
    }
    if (!rows.length) return '<div class="entity-meta">No metrics in snapshot</div>';
    return (
      '<div class="metrics">' +
      rows
        .map(function (r) {
          return "<div>" + esc(r[0]) + ": <strong>" + esc(r[1]) + "</strong></div>";
        })
        .join("") +
      "</div>"
    );
  }

  function renderNodes(nodes) {
    const list = asList(nodes);
    els.nodesCount.textContent = String(list.length);
    if (!list.length) {
      els.nodes.innerHTML = '<div class="entity-meta">No nodes in snapshot</div>';
      return;
    }
    els.nodes.innerHTML = list
      .map(function (n) {
        const id = n.id || n.host || "node";
        const st = normStatus(n.status);
        const req = n.required ? "required" : "optional";
        const cls = n.class ? " · " + n.class : "";
        const age = n.observation_age_s != null ? " · age " + fmtAge(n.observation_age_s) : "";
        return (
          '<article class="entity">' +
          '<div class="entity-head"><div class="entity-id">' +
          esc(id) +
          '</div><div class="status-chip ' +
          st +
          '">' +
          st +
          "</div></div>" +
          '<div class="entity-meta">' +
          esc(req) +
          esc(cls) +
          esc(age) +
          "</div>" +
          renderMetrics(n.metrics || n) +
          "</article>"
        );
      })
      .join("");
  }

  function renderServices(services) {
    const list = asList(services);
    els.servicesCount.textContent = String(list.length);
    if (!list.length) {
      els.services.innerHTML = '<div class="entity-meta">No services in snapshot</div>';
      return;
    }
    els.services.innerHTML = list
      .map(function (s) {
        const id = s.id || "service";
        const st = normStatus(s.status);
        const req = s.required ? "required" : "optional";
        const host = s.host ? " · host " + s.host : "";
        const cls = s.class ? " · " + s.class : "";
        let cam = "";
        if (s.cameras && typeof s.cameras === "object") {
          const keys = Object.keys(s.cameras);
          if (keys.length) {
            cam =
              '<div class="layers">' +
              keys
                .map(function (c) {
                  const cst = layerStatus(s.cameras[c]);
                  return '<span class="layer-chip ' + cst + '">cam/' + esc(c) + ":" + cst + "</span>";
                })
                .join("") +
              "</div>";
          }
        }
        // Prefer non-camera layers in main chips; cameras shown separately.
        const layers = Object.assign({}, s.layers || {});
        Object.keys(layers).forEach(function (k) {
          if (k.indexOf("camera/") === 0) delete layers[k];
        });
        return (
          '<article class="entity">' +
          '<div class="entity-head"><div class="entity-id">' +
          esc(id) +
          '</div><div class="status-chip ' +
          st +
          '">' +
          st +
          "</div></div>" +
          '<div class="entity-meta">' +
          esc(req) +
          esc(host) +
          esc(cls) +
          "</div>" +
          renderLayers(layers) +
          cam +
          "</article>"
        );
      })
      .join("");
  }

  function renderPaths(paths) {
    const list = asList(paths);
    els.pathsCount.textContent = String(list.length);
    if (!list.length) {
      els.paths.innerHTML = '<div class="entity-meta">No paths in snapshot</div>';
      return;
    }
    els.paths.innerHTML = list
      .map(function (p) {
        const id = p.id || "path";
        const st = normStatus(p.status);
        const req = p.required ? "required" : "optional";
        const cls = p.class ? " · " + p.class : "";
        return (
          '<article class="entity">' +
          '<div class="entity-head"><div class="entity-id">' +
          esc(id) +
          '</div><div class="status-chip ' +
          st +
          '">' +
          st +
          "</div></div>" +
          '<div class="entity-meta">' +
          esc(req) +
          esc(cls) +
          "</div>" +
          renderLayers(p.layers) +
          "</article>"
        );
      })
      .join("");
  }

  const MCP_NAMES = {
    "dhras-mcp": "DHRAS", "fitbit-mcp": "Fitbit", "tv-mcp": "TV",
    "pi-git-mcp": "Pi Git Audit", "polar-h10-mcp": "Polar H10",
    "jetson-git-mcp": "Jetson Git Audit",
  };
  const REASONS = {
    AUTH_DENIED: "The access gateway rejected Guardian’s identity.",
    OAUTH_EXPIRED: "The authentication credential has expired.",
    AUTH_PROBE_OFF: "Authenticated access has not been checked.",
    TOOL_TIMEOUT: "The check did not finish within its time limit.",
    TLS_FAIL: "A secure connection could not be established.",
    DNS_FAIL: "The server address could not be found.",
    PUBLIC_ENDPOINT_FAIL: "The public endpoint rejected the request.",
    MCP_HANDSHAKE_FAIL: "The server could not complete the connection handshake.",
    MCP_MALFORMED: "The server returned an unexpected response.",
    MCP_CATALOG_STALE: "The available tools differ from the expected list.",
    HOST_AGENT_DOWN: "The host is not reporting fresh health information.",
    SERVICE_INACTIVE: "The service is not running.",
    SVC_INACTIVE: "The service is not running.",
    SVC_STATE_UNKNOWN: "The service’s running state could not be checked.",
    SVC_ALIVE_UNHEALTHY: "The service is running but its health check failed.",
    PORT_CLOSED: "The service is not accepting connections.",
    DEPENDENCY_OUTAGE: "A service this depends on is unavailable.",
    PROBE_CRASH: "The health check encountered an internal error.",
    EVIDENCE_MISSING: "No health information is available yet.",
    SNAPSHOT_STALE: "Guardian’s information is out of date.",
    OBSERVATION_STALE: "The last observation is out of date.",
    EVIDENCE_STALE: "The last observation is out of date.",
  };

  function accessReason(item, auth) {
    const status = normStatus(item.status);
    if (status === "GREEN") return auth ? "Guardian completed the authenticated connection check." : "The service is healthy.";
    if (item.reason === "IDENTITY_MISSING") return "Guardian’s authentication credentials are not configured.";
    return REASONS[item.class] || (status === "UNKNOWN" ? "No current result is available." :
      status === "YELLOW" ? "The check needs attention. Open details for the reported reason." :
      "The check failed. Open details for the reported reason.");
  }

  function renderAuth(auth, services, guardian, ui) {
    const authById = new Map(asList(auth).map(a => [a.id, a]));
    const serviceById = new Map(asList(services).map(s => [s.id, s]));
    const heartbeatAge = (Date.now() - Date.parse((guardian || {}).heartbeat_at)) / 1000;
    const stale = normStatus((guardian || {}).status) !== "GREEN" || !Number.isFinite(heartbeatAge) ||
      heartbeatAge < -5 || heartbeatAge > ((ui || {}).hard_stale_s || 120);
    let healthy = 0;
    els.auth.innerHTML = Object.keys(MCP_NAMES).map(function (id) {
      const service = serviceById.get(id) || {};
      const authItem = authById.get(id) || (service.layers || {}).client_invoke || {};
      const health = stale ? {status: "unknown", class: "SNAPSHOT_STALE"} : service;
      const access = stale ? {status: "unknown", class: "SNAPSHOT_STALE"} : authItem;
      const hs = normStatus(health.status), as = normStatus(access.status);
      if (hs === "GREEN" && as === "GREEN") healthy += 1;
      const healthLabels = {GREEN: "Healthy", YELLOW: "Attention", RED: "Unavailable", UNKNOWN: "Unknown"};
      const authLabels = {GREEN: "Verified", YELLOW: "Attention", RED: "Failed", UNKNOWN: "Unverified"};
      const timing = !stale && Number.isFinite(authItem.duration_ms) ?
        " · " + (authItem.duration_ms / 1000).toFixed(2) + "s" : "";
      const policy = authItem.required === true ? "Required for system health" :
        authItem.required === false ? "Does not block system health" : "Requirement not reported";
      return '<article class="entity access-card"><h3>' + esc(MCP_NAMES[id]) + '</h3>' +
        '<div class="access-row"><span>Service health</span><span class="status-chip ' + hs + '">' + healthLabels[hs] + '</span></div>' +
        '<p class="access-reason">' + esc(accessReason(health, false)) + '</p>' +
        '<div class="access-row"><span>Authenticated access</span><span class="status-chip ' + as + '">' + authLabels[as] + '</span></div>' +
        '<p class="access-reason">' + esc(accessReason(access, true)) + '</p>' +
        '<div class="entity-meta">' + esc(policy + timing) + '</div>' +
        '<details><summary>Check details</summary><dl class="access-details">' +
        '<dt>Service</dt><dd>' + esc(id) + '</dd><dt>Last auth check</dt><dd>' + esc(fmtPT(authItem.observed_at)) + '</dd>' +
        '<dt>Responding server</dt><dd>' + esc((authItem.server_info || {}).name || "Not reported") + '</dd>' +
        '<dt>Service reason</dt><dd>' + esc(health.class || "None reported") + '</dd>' +
        '<dt>Auth reason</dt><dd>' + esc(access.class || "None reported") + '</dd></dl></details></article>';
    }).join("");
    els.authSummary.textContent = stale ? "Waiting for current Guardian data" : healthy + " of 6 healthy and verified";
  }

  function collectActivity(snapshot, eventsPayload) {
    const fromApi = (eventsPayload && eventsPayload.events) || [];
    let fromSnap = [];
    if (snapshot) {
      if (Array.isArray(snapshot.activity)) fromSnap = snapshot.activity;
      else if (snapshot.activity && Array.isArray(snapshot.activity.recent_events)) {
        fromSnap = snapshot.activity.recent_events;
      }
    }
    // Prefer events.jsonl (append-only truth); fall back to snapshot activity.
    const merged = fromApi.length ? fromApi : fromSnap;
    return merged.slice(-ACTIVITY_LIMIT).reverse();
  }

  function renderActivity(rows) {
    els.activityCount.textContent = "last " + Math.min(ACTIVITY_LIMIT, rows.length);
    if (!rows.length) {
      els.activityBody.innerHTML = '<tr><td colspan="6" class="empty">No events yet</td></tr>';
      return;
    }
    els.activityBody.innerHTML = rows
      .map(function (e) {
        const sev = normStatus(e.severity === "error" ? "red" : e.severity === "warning" ? "yellow" : e.result || e.severity);
        return (
          "<tr>" +
          '<td class="mono">' +
          esc(fmtPT(e.ts)) +
          "</td>" +
          "<td><span class=\"pill " +
          sev +
          '">' +
          esc((e.severity || "info").toUpperCase()) +
          "</span></td>" +
          "<td>" +
          esc(e.component || e.host || "—") +
          "</td>" +
          "<td><span class=\"status-chip " +
          normStatus(e.result) +
          '">' +
          normStatus(e.result) +
          "</span></td>" +
          '<td class="mono">' +
          esc(e.class || "—") +
          "</td>" +
          "<td>" +
          esc(e.message || "") +
          "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  function renderSnapshot(snap, eventsPayload) {
    const g = snap.guardian || {};
    const sys = snap.system || {};
    setChip(els.gStatus, g.status);
    els.gHeartbeat.textContent = fmtPT(g.heartbeat_at);
    els.gAge.textContent = fmtAge(g.observation_age_s);
    els.gRun.textContent = g.last_run_result || "—";
    els.gTrace.textContent = snap.trace_id || g.trace_id || "—";
    els.gTrace.title = els.gTrace.textContent;

    setChip(els.sysStatus, sys.status);
    els.sysReason.textContent = sys.reason || "(no reason)";

    renderNodes(snap.nodes);
    renderServices(snap.services);
    renderPaths(snap.paths);
    renderAuth(snap.auth, snap.services, snap.guardian, snap._ui);
    renderActivity(collectActivity(snap, eventsPayload));

    const ui = snap._ui || {};
    els.servedMeta.textContent =
      "served " +
      (ui.served_at ? fmtPT(ui.served_at) : "—") +
      " · stale>" +
      (ui.hard_stale_s != null ? ui.hard_stale_s + "s" : "120s") +
      (ui.error ? " · " + ui.error : "");
  }

  async function tick() {
    els.clock.textContent = new Intl.DateTimeFormat("en-US", {
      timeZone: TZ,
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date()) + " PT";

    try {
      const [snapRes, evRes] = await Promise.all([
        fetch("/api/snapshot", { cache: "no-store" }),
        fetch("/api/events?limit=" + ACTIVITY_LIMIT, { cache: "no-store" }),
      ]);
      if (!snapRes.ok) throw new Error("snapshot HTTP " + snapRes.status);
      const snap = await snapRes.json();
      let eventsPayload = { events: [] };
      if (evRes.ok) eventsPayload = await evRes.json();
      renderSnapshot(snap, eventsPayload);
      els.fetchError.classList.add("hidden");
      els.refreshBadge.textContent = "refresh " + Math.round(REFRESH_MS / 1000) + "s";
      els.refreshBadge.className = "pill GREEN";
    } catch (err) {
      renderAuth([], [], {}, {});
      els.authSummary.textContent = "Connection lost — current status unavailable";
      els.fetchError.textContent = "UI fetch error: " + (err && err.message ? err.message : err);
      els.fetchError.classList.remove("hidden");
      els.refreshBadge.textContent = "refresh failed";
      els.refreshBadge.className = "pill RED";
    }
  }

  tick();
  setInterval(tick, REFRESH_MS);
})();
