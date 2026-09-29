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
    operational: document.getElementById("operational"),
    controlResult: document.getElementById("control-result"),
    roarm: document.getElementById("roarm"),
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

  function renderRoarm(roarm, guardian, ui) {
    if (!roarm || typeof roarm !== "object") {
      els.roarm.innerHTML =
        '<article class="entity"><div class="entity-head"><div class="entity-id">RoArm-M3</div>' +
        '<div class="status-chip UNKNOWN">UNKNOWN</div></div>' +
        '<div class="entity-meta">No RoArm observation</div></article>';
      return;
    }

    const heartbeatAge = (Date.now() - Date.parse((guardian || {}).heartbeat_at)) / 1000;
    const snapshotStale = normStatus((guardian || {}).status) !== "GREEN" ||
      !Number.isFinite(heartbeatAge) || heartbeatAge < -5 ||
      heartbeatAge > ((ui || {}).hard_stale_s || 120);
    const status = snapshotStale ? "UNKNOWN" : normStatus(roarm.status);
    const observedMs = Date.parse(roarm.observed_at);
    const computedAge = Number.isFinite(observedMs) ? (Date.now() - observedMs) / 1000 : null;
    const observationAge = roarm.observation_age_s != null ? roarm.observation_age_s : computedAge;
    const freshness = snapshotStale ? "Stale" :
      roarm.fresh === true ? "Fresh" : roarm.fresh === false ? "Stale" : "Unknown";
    const reason = snapshotStale
      ? "SNAPSHOT_STALE"
      : (roarm.failure_reason || roarm.class || "No fault");
    const value = function (item) {
      return item == null || item === "" ? "—" : item;
    };
    const pose = roarm.pose && typeof roarm.pose === "object" ? roarm.pose : {};
    const joints = roarm.joints && typeof roarm.joints === "object" ? roarm.joints : {};
    const route = roarm.route && typeof roarm.route === "object" ? roarm.route : {};
    const row = function (label, item) {
      return "<div>" + esc(label) + ": <strong>" + esc(value(item)) + "</strong></div>";
    };
    const statusUpdatedMs = Date.parse(roarm.transport_status_updated_at);
    const statusAge = Number.isFinite(statusUpdatedMs)
      ? (Date.now() - statusUpdatedMs) / 1000
      : roarm.transport_status_age_s;
    const recordFresh = roarm.transport_status_fresh === true;
    const transportLabel = function (state) {
      if (!recordFresh || state === "serial" || state === "usb") return "unavailable";
      if (state === "udp") return "UDP trajectory active";
      if (state === "http") return "HTTP";
      if (state === "idle") return "idle";
      return "unavailable";
    };
    const statusRecord = roarm.transport_status_updated_at
      ? (recordFresh ? "fresh" : "stale") + " · age " + fmtAge(statusAge)
      : "unavailable";
    const routeLabel = function () {
      if (route.status === "ok") return "wlan0 192.168.4.2 -> 192.168.4.1";
      if (route.status === "wrong_interface") {
        return "wrong interface " + value(route.device) + "; need wlan0";
      }
      if (route.status === "wrong_source") {
        return "wrong source " + value(route.source) + "; need 192.168.4.2";
      }
      return "unavailable";
    };
    const eventLabel = function (item) {
      if (!item || typeof item !== "object") return "unavailable";
      const parts = [];
      if (item.at) parts.push(fmtPT(item.at));
      if (item.sequence != null) parts.push("seq " + item.sequence);
      if (item.detail) parts.push(item.detail);
      return parts.length ? parts.join(" · ") : "recorded";
    };
    const completionLabel = function (item) {
      if (!item || typeof item !== "object") return "unavailable";
      const parts = [];
      if (item.pattern) parts.push(item.pattern);
      if (item.at) parts.push(fmtPT(item.at));
      if (item.stream_id != null) parts.push("stream " + item.stream_id);
      if (item.sequence != null) parts.push("seq " + item.sequence);
      return parts.length ? parts.join(" · ") : "recorded";
    };
    const streamLabel = roarm.stream_id == null
      ? "unavailable"
      : String(roarm.stream_id) + (
        recordFresh && roarm.stream_role === "current" ? " (current)" : " (last)"
      );

    els.roarm.innerHTML =
      '<article class="entity roarm-card"><div class="entity-head"><div class="entity-id">RoArm-M3</div>' +
      '<div class="status-chip ' + status + '">' + status + "</div></div>" +
      '<div class="entity-meta">' +
      esc(reason + " · T105 " + freshness.toLowerCase() + " · age " + fmtAge(observationAge)) +
      "</div><div class=\"roarm-summary\">" +
      row("Reachability", roarm.reachability || (roarm.connected === true ? "reachable" : "unreachable")) +
      row("Route", routeLabel()) +
      row("T105", freshness + " · age " + fmtAge(observationAge)) +
      row("Transport state", transportLabel(roarm.transport_state)) +
      row("Status record", statusRecord) +
      row("UDP target", roarm.udp_target || "192.168.4.1:4210") +
      row("Stream ID", streamLabel) +
      row("Last sequence", roarm.last_sequence) +
      row("Last completion", completionLabel(roarm.last_completion)) +
      row("UDP late send", eventLabel(roarm.last_late)) +
      row("UDP failed send", eventLabel(roarm.last_failed)) +
      row("Watchdog release", eventLabel(roarm.last_watchdog)) +
      row("Failure", reason) +
      row("Endpoint", roarm.endpoint) +
      row("Observed", fmtPT(roarm.observed_at)) +
      "</div><h3>XYZ</h3><div class=\"metrics\">" +
      row("X", pose.x) + row("Y", pose.y) + row("Z", pose.z) + row("Tilt", pose.tilt) +
      "</div><h3>Joints</h3><div class=\"metrics\">" +
      row("Base", joints.base) + row("Shoulder", joints.shoulder) +
      row("Elbow", joints.elbow) + row("Wrist", joints.wrist) +
      row("Roll", joints.roll) + row("Gripper", joints.gripper) +
      "</div></article>";
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
    ROARM_UNREACHABLE: "The RoArm did not answer the state check.",
    ARM_ROUTE_WRONG_INTERFACE: "The RoArm route is not on wlan0.",
    ARM_ROUTE_WRONG_SOURCE: "The RoArm route is not from the Pi address 192.168.4.2.",
    ARM_ROUTE_UNAVAILABLE: "The route to the RoArm could not be read.",
    ROARM_SERIAL_REJECTED: "USB/serial is not a RoArm runtime path.",
    PATTERN_UDP_LATE: "The UDP trajectory sender fell behind and stopped.",
    PATTERN_UDP_FAILED: "A UDP trajectory send failed.",
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

  function controlValue(id) {
    const node = document.getElementById(id);
    return node ? node.value.trim() : "";
  }

  function optionalNumber(id) {
    const text = controlValue(id);
    if (!text) return undefined;
    const value = Number(text);
    return Number.isFinite(value) ? value : text;
  }

  async function postSkill(body) {
    const response = await fetch("/api/roarm/skills", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    if (els.controlResult) els.controlResult.textContent = JSON.stringify(payload, null, 2);
    tick();
  }

  async function postEngineering(packet) {
    const response = await fetch("/api/roarm/engineering", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        packet: packet,
        authority: controlValue("ctrl-authority") || "manual",
        operator: controlValue("ctrl-operator") || "manual",
        mission: controlValue("ctrl-mission") || null,
      }),
    });
    const payload = await response.json();
    if (els.controlResult) els.controlResult.textContent = JSON.stringify(payload, null, 2);
    tick();
  }

  function skillBody(skill, params) {
    return {
      skill: skill,
      authority: controlValue("ctrl-authority") || "manual",
      operator: controlValue("ctrl-operator") || "manual",
      mission: controlValue("ctrl-mission") || null,
      params: params || {},
    };
  }

  const moveButton = document.getElementById("ctrl-move");
  if (moveButton && moveButton.addEventListener) {
    moveButton.addEventListener("click", function () {
      const params = {
        x: optionalNumber("ctrl-x"),
        y: optionalNumber("ctrl-y"),
        z: optionalNumber("ctrl-z"),
      };
      const tilt = optionalNumber("ctrl-t");
      const roll = optionalNumber("ctrl-r");
      const speed = optionalNumber("ctrl-spd");
      if (tilt !== undefined) params.t = tilt;
      if (roll !== undefined) params.r = roll;
      if (speed !== undefined) params.spd = speed;
      postSkill(skillBody("move_to_pose", params));
    });
  }
  const homeButton = document.getElementById("ctrl-home");
  if (homeButton && homeButton.addEventListener) homeButton.addEventListener("click", function () { postSkill(skillBody("return_home", {})); });
  const patternButton = document.getElementById("ctrl-pattern-run");
  if (patternButton && patternButton.addEventListener) {
    patternButton.addEventListener("click", function () {
      postSkill(skillBody("run_pattern", { pattern: controlValue("ctrl-pattern") }));
    });
  }
  const stopButton = document.getElementById("ctrl-stop");
  if (stopButton && stopButton.addEventListener) stopButton.addEventListener("click", function () { postSkill(skillBody("stop", {})); });
  const clearButton = document.getElementById("ctrl-clear");
  if (clearButton && clearButton.addEventListener) clearButton.addEventListener("click", function () { postSkill(skillBody("clear", {})); });
  const jsonButton = document.getElementById("ctrl-json-send");
  if (jsonButton && jsonButton.addEventListener) {
    jsonButton.addEventListener("click", function () {
      let packet = null;
      try {
        packet = JSON.parse(controlValue("ctrl-json") || document.getElementById("ctrl-json").value);
      } catch (err) {
        if (els.controlResult) els.controlResult.textContent = "Malformed engineering JSON";
        return;
      }
      postEngineering(packet);
    });
  }

  function renderOperational(snap) {
    const op = snap && snap.state_engine && snap.state_engine.operational;
    const known = function (value) {
      return value == null || value === "" ? "unknown" : String(value);
    };
    const permitted = !op
      ? "unknown"
      : op.motion_permitted === true
        ? "true"
        : op.motion_permitted === false
          ? "false"
          : "unknown";
    const fault = !op
      ? "unknown"
      : op.fault_class || op.fault_reason
        ? [op.fault_class, op.fault_reason].filter(Boolean).join(" · ")
        : "unknown";
    const transition = !op
      ? "unknown"
      : [
          (op.previous_state ? op.previous_state + " → " : "") + known(op.state),
          op.transition_reason || "unknown",
          op.transition_at ? fmtPT(op.transition_at) : "unknown",
        ].join(" · ");
    const row = function (label, item) {
      return "<div>" + esc(label) + ": <strong>" + esc(item) + "</strong></div>";
    };
    els.operational.innerHTML =
      '<div class="roarm-summary">' +
      row("Operational state", known(op && op.state)) +
      row("Active operator", known(op && op.operator)) +
      row("Active skill/action", known(op && op.skill)) +
      row("Active mission", known(op && op.mission)) +
      row("Motion permitted", permitted) +
      row("Motion authority", known(op && op.motion_authority)) +
      row("Last transition", transition) +
      row("Fault reason", fault) +
      row("Last completed action", known(op && op.last_completed_action)) +
      "</div>";
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

    renderOperational(snap);
    renderNodes(snap.nodes);
    renderServices(snap.services);
    renderPaths(snap.paths);
    renderRoarm(snap.roarm, snap.guardian, snap._ui);
    renderAuth(snap.auth, snap.services, snap.guardian, snap._ui);
    renderActivity(collectActivity(snap, eventsPayload));
    renderOverview(snap, eventsPayload);

    const ui = snap._ui || {};
    els.servedMeta.textContent =
      "served " +
      (ui.served_at ? fmtPT(ui.served_at) : "—") +
      " · stale>" +
      (ui.hard_stale_s != null ? ui.hard_stale_s + "s" : "120s") +
      (ui.error ? " · " + ui.error : "");
  }

  function setText(id, value) {
    const node = document.getElementById(id);
    if (node) node.textContent = value == null || value === "" ? "unknown" : String(value);
  }

  function renderOverview(snap, eventsPayload) {
    const sys = (snap && snap.system) || {};
    const op = snap && snap.state_engine && snap.state_engine.operational;
    const roarm = (snap && snap.roarm) || {};
    const events = collectActivity(snap, eventsPayload);
    const latest = events.length ? events[0].message || events[0].class || "activity" : "No events yet";
    setText("overview-system", (sys.status || "unknown").toUpperCase());
    setText("overview-alerts", op && (op.fault_class || op.fault_reason) ? (op.fault_class || op.fault_reason) : "None");
    setText("overview-operational", op && op.state ? op.state : "unknown");
    setText("overview-robots", roarm.status ? String(roarm.status).toUpperCase() : "unknown");
    setText("overview-activity", latest);
  }

  function badgeList(items) {
    return (items || []).map(function (item) {
      return '<span class="pill">' + esc(item) + "</span>";
    }).join(" ");
  }

  let ptzTimer = null;
  let ptzCamera = null;
  let ptzDown = false;

  function sendPtz(camera, dir) {
    const request = fetch("/api/perception/ptz", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ camera: camera, dir: dir }),
    });
    if (dir === "stop") {
      request.catch(function () {});
      return;
    }
    request.then(function (response) { return response.json(); }).then(function (payload) {
      setText("perception-status", (payload && payload.reason) || "requested");
    }).catch(function () {
      setText("perception-status", "Perception UNKNOWN / OFFLINE");
    });
  }

  function releasePtz() {
    ptzDown = false;
    if (ptzTimer) {
      clearInterval(ptzTimer);
      ptzTimer = null;
    }
    if (!ptzCamera) return;
    const camera = ptzCamera;
    ptzCamera = null;
    sendPtz(camera, "stop");
  }

  function holdPtz(camera, dir) {
    if (ptzTimer) {
      clearInterval(ptzTimer);
      ptzTimer = null;
    }
    ptzCamera = camera;
    sendPtz(camera, dir);
    ptzTimer = setInterval(function () { sendPtz(camera, dir); }, 300);
  }

  function cameraSignature(camera) {
    return [
      camera.id,
      camera.name,
      camera.parent_id || "",
      camera.type || "",
      (camera.capabilities || []).join("|"),
      camera.stream_url || "",
      camera.hub_url || "",
    ].join("~");
  }

  function cameraCard(camera) {
    const caps = camera.capabilities || [];
    const controls = [];
    if (caps.indexOf("ptz") >= 0) {
      ["left", "right", "up", "down", "stop"].forEach(function (dir) {
        controls.push('<button type="button" data-ptz="' + esc(dir) + '" data-camera="' + esc(camera.id) + '">' + esc(dir) + "</button>");
      });
    }
    if (caps.indexOf("track") >= 0) {
      controls.push('<button type="button" data-track="on" data-camera="' + esc(camera.id) + '">Track on</button>');
      controls.push('<button type="button" data-track="off" data-camera="' + esc(camera.id) + '">Track off</button>');
    }
    if (caps.indexOf("snapshot") >= 0) {
      controls.push('<button type="button" data-snap="' + esc(camera.id) + '">Snapshot</button>');
    }
    const stream = caps.indexOf("stream") >= 0 && camera.stream_url
      ? '<img class="cam-stream" alt="' + esc(camera.name) + '" src="' + esc(camera.stream_url) + '" data-src="' + esc(camera.stream_url) + '">'
      : "";
    const event = camera.last_event
      ? esc(camera.last_event.class || "detection") + " · " + esc(camera.last_event.timestamp || "")
      : "No recent detection";
    return '<article class="card cam-card" data-camera-card="' + esc(camera.id) + '">' +
      "<h2>" + esc(camera.name) + "</h2>" +
      '<p class="cam-health">' + esc(camera.health || "unknown") + (camera.mode ? " · " + esc(camera.mode) : "") + "</p>" +
      "<p>Parent: " + esc(camera.parent_id || "none") + "</p>" +
      '<p class="cam-event">' + event + "</p>" +
      "<p>" + badgeList(caps) + "</p>" +
      stream +
      (camera.hub_url ? '<p><a href="' + esc(camera.hub_url) + '" target="_blank" rel="noopener">Open Vision Hub</a></p>' : "") +
      '<div class="control-actions">' + controls.join("") + "</div></article>";
  }

  function ptzButton(event) {
    const button = event && event.target;
    if (!button || !button.getAttribute || !button.getAttribute("data-ptz")) return null;
    return button;
  }

  function pressPtz(event) {
    const button = ptzButton(event);
    if (!button) return;
    if (event.cancelable && event.preventDefault) event.preventDefault();
    const camera = button.getAttribute("data-camera");
    const dir = button.getAttribute("data-ptz");
    if (dir === "stop") {
      releasePtz();
      sendPtz(camera, "stop");
      return;
    }
    if (ptzDown && (event.type === "mousedown" || event.type === "touchstart")) return;
    ptzDown = true;
    if (button.getAttribute("data-leave") !== "yes") {
      button.setAttribute("data-leave", "yes");
      button.addEventListener("mouseleave", function () { releasePtz(); });
    }
    holdPtz(camera, dir);
  }

  function bindCameraGrid(grid) {
    if (!grid || grid.getAttribute("data-bound") === "yes") return;
    grid.setAttribute("data-bound", "yes");
    grid.addEventListener("pointerdown", pressPtz);
    grid.addEventListener("mousedown", pressPtz);
    grid.addEventListener("touchstart", pressPtz);
    grid.addEventListener("click", function (event) {
      const button = event.target;
      if (!button || !button.getAttribute || button.getAttribute("data-ptz")) return;
      const camera = button.getAttribute("data-camera") || button.getAttribute("data-snap");
      if (button.getAttribute("data-track")) {
        postPerception("/api/perception/track", { camera: camera, enabled: button.getAttribute("data-track") === "on" });
      } else if (button.getAttribute("data-snap")) {
        window.open("/api/perception/snapshot/" + encodeURIComponent(button.getAttribute("data-snap")), "_blank", "noopener");
      }
    });
  }

  if (typeof window !== "undefined" && window.addEventListener) {
    window.addEventListener("pointerup", releasePtz);
    window.addEventListener("pointercancel", releasePtz);
    window.addEventListener("touchend", releasePtz);
    window.addEventListener("mouseup", releasePtz);
    window.addEventListener("blur", releasePtz);
  }

  async function postPerception(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    setText("perception-status", (payload && payload.reason) || "requested");
  }

  function renderCameras(view) {
    const grid = document.getElementById("cam-grid");
    const status = document.getElementById("perception-status");
    const link = document.getElementById("vision-hub-link");
    if (!view || view.available !== true) {
      if (status) status.textContent = "Perception UNKNOWN / OFFLINE";
      setText("overview-perception", "UNKNOWN / OFFLINE");
      if (grid && grid.setAttribute) {
        grid.innerHTML = "";
        grid.setAttribute("data-cams", "");
      }
      return;
    }
    if (status) status.textContent = view.cameras.length + " cameras";
    setText("overview-perception", view.cameras.length + " cameras");
    if (link && view.hub_url) link.href = view.hub_url;
    if (!grid) return;
    const signature = view.cameras.map(cameraSignature).join(",");
    if (grid.getAttribute("data-cams") !== signature) {
      releasePtz();
      grid.innerHTML = view.cameras.map(cameraCard).join("");
      grid.setAttribute("data-cams", signature);
      bindCameraGrid(grid);
    } else {
      view.cameras.forEach(function (camera) {
        const card = grid.querySelector('[data-camera-card="' + camera.id + '"]');
        if (!card) return;
        const health = card.querySelector(".cam-health");
        const recent = card.querySelector(".cam-event");
        if (health) health.textContent = (camera.health || "unknown") + (camera.mode ? " · " + camera.mode : "");
        if (recent) {
          recent.textContent = camera.last_event
            ? (camera.last_event.class || "detection") + " · " + (camera.last_event.timestamp || "")
            : "No recent detection";
        }
      });
    }
  }

  function renderDetections(payload, targetId) {
    const node = document.getElementById(targetId);
    if (!node) return;
    const events = payload && Array.isArray(payload.events) ? payload.events : [];
    if (!payload || payload.available === false) {
      node.innerHTML = "<p>Detections unavailable</p>";
      return;
    }
    node.innerHTML = events.length
      ? events.map(function (event) {
          return "<p>" + esc(event.timestamp || "") + " · " + esc(event.camera || "") + " · " + esc(event.class || "detection") + "</p>";
        }).join("")
      : "<p>No detections</p>";
  }

  function renderSnapshots(payload) {
    const node = document.getElementById("snapshot-grid");
    if (!node) return;
    const images = payload && Array.isArray(payload.snapshots) ? payload.snapshots : [];
    if (!payload || payload.available === false) {
      node.textContent = "Snapshots unavailable";
      return;
    }
    node.innerHTML = images.map(function (image) {
      const src = "/api/perception/media?path=" + encodeURIComponent(image.path);
      return '<article class="card"><h2>' + esc(image.camera || image.name || "snapshot") + "</h2>" +
        '<img class="cam-stream" alt="' + esc(image.name || "snapshot") + '" src="' + src + '"></article>';
    }).join("") || "<p>No snapshots</p>";
  }

  function homeCard(entity) {
    const unit = entity.unit ? " " + entity.unit : "";
    const presence = entity.available ? "online" : "offline";
    return '<article class="card"><h2>' + esc(entity.name) + "</h2>" +
      "<p>" + esc(entity.state) + esc(unit) + " · " + presence + "</p>" +
      "<p>" + esc(entity.entity_id) + "</p></article>";
  }

  function homeControl(entity) {
    const actions = [];
    if (entity.available && entity.writable) {
      if ((entity.capabilities || []).indexOf("turn_on") >= 0) {
        actions.push('<button type="button" data-home-action="turn_on" data-entity="' + esc(entity.entity_id) + '">On</button>');
      }
      if ((entity.capabilities || []).indexOf("turn_off") >= 0) {
        actions.push('<button type="button" data-home-action="turn_off" data-entity="' + esc(entity.entity_id) + '">Off</button>');
      }
      if ((entity.capabilities || []).indexOf("brightness") >= 0) {
        actions.push('<label>Brightness<input type="number" min="0" max="255" data-home-brightness="' + esc(entity.entity_id) + '"></label>');
        actions.push('<button type="button" data-home-action="set_brightness" data-entity="' + esc(entity.entity_id) + '">Set</button>');
      }
    }
    return '<article class="card" data-home-card="' + esc(entity.entity_id) + '"><h2>' + esc(entity.name) + "</h2>" +
      '<p class="home-state">' + esc(entity.state) + " · " + (entity.available ? "online" : "offline") + "</p>" +
      '<div class="home-actions">' + actions.join("") + "</div></article>";
  }

  function bindHome(node) {
    if (!node || typeof node.querySelectorAll !== "function") return;
    node.querySelectorAll("[data-home-action]").forEach(function (button) {
      button.addEventListener("click", function () {
        const entity = button.getAttribute("data-entity");
        const action = button.getAttribute("data-home-action");
        let brightness = null;
        if (action === "set_brightness") {
          const input = node.querySelector('[data-home-brightness="' + entity + '"]');
          const raw = input ? String(input.value) : "";
          if (!/^\d+$/.test(raw)) {
            setText("home-status", "MALFORMED_PARAMETERS");
            return;
          }
          brightness = Number(raw);
        }
        postHome(entity, action, brightness);
      });
    });
  }

  async function postHome(entity, action, brightness) {
    const body = { entity_id: entity, action: action };
    if (brightness != null) body.brightness = brightness;
    try {
      const response = await fetch("/api/home/action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = await response.json();
      setText("home-status", payload && payload.accepted ? "requested" : ((payload && payload.reason) || "rejected"));
    } catch (err) {
      setText("home-status", "Home UNKNOWN / OFFLINE");
    }
  }

  function renderHome(view) {
    const linkIds = ["home-assistant-link", "home-assistant-settings-link"];
    if (!view || view.available !== true) {
      setText("home-status", "Home UNKNOWN / OFFLINE" + (view && view.reason ? " · " + view.reason : ""));
      setText("overview-home", "UNKNOWN / OFFLINE");
      ["home-counts", "home-sensors", "home-binary", "home-controls"].forEach(function (id) {
        const node = document.getElementById(id);
        if (node) node.innerHTML = "";
      });
      return;
    }
    const entities = Array.isArray(view.entities) ? view.entities : [];
    setText("home-status", entities.length + " entities");
    setText("overview-home", entities.length + " entities");
    linkIds.forEach(function (id) {
      const link = document.getElementById(id);
      if (link && view.ui_url) link.href = view.ui_url;
    });
    const counts = document.getElementById("home-counts");
    if (counts) {
      const tally = view.counts || {};
      counts.innerHTML = Object.keys(tally).map(function (domain) {
        return "<p>" + esc(domain) + " · " + esc(tally[domain]) + "</p>";
      }).join("") || "<p>No exposed entities</p>";
    }
    const sensors = document.getElementById("home-sensors");
    const binary = document.getElementById("home-binary");
    if (sensors) sensors.innerHTML = entities.filter(function (entity) { return entity.domain === "sensor"; }).map(homeCard).join("") || "<p>None</p>";
    if (binary) binary.innerHTML = entities.filter(function (entity) { return entity.domain === "binary_sensor"; }).map(homeCard).join("") || "<p>None</p>";
    const controls = document.getElementById("home-controls");
    if (!controls) return;
    const controllable = entities.filter(function (entity) {
      return entity.domain === "switch" || entity.domain === "light";
    });
    const signature = controllable.map(function (entity) {
      return [entity.entity_id, entity.writable, entity.available, (entity.capabilities || []).join("|")].join(":");
    }).join(",");
    if (controls.getAttribute("data-home") !== signature) {
      controls.innerHTML = controllable.map(homeControl).join("") || "<p>None</p>";
      controls.setAttribute("data-home", signature);
      bindHome(controls);
    } else {
      controllable.forEach(function (entity) {
        const card = typeof controls.querySelector === "function"
          ? controls.querySelector('[data-home-card="' + entity.entity_id + '"]')
          : null;
        if (!card || typeof card.querySelector !== "function") return;
        const state = card.querySelector(".home-state");
        if (state) state.textContent = (entity.state || "unknown") + " · " + (entity.available ? "online" : "offline");
      });
    }
  }

  async function refreshHome() {
    try {
      const response = await fetch("/api/home/entities", { cache: "no-store" });
      const view = response.ok ? await response.json() : null;
      renderHome(view);
    } catch (err) {
      setText("home-status", "Home UNKNOWN / OFFLINE");
      setText("overview-home", "UNKNOWN / OFFLINE");
    }
  }

  function renderDevices(payload) {
    const node = document.getElementById("device-list");
    if (!node || !payload || !Array.isArray(payload.devices)) return;
    node.innerHTML = payload.devices.map(function (device) {
      return '<article class="card"><h2>' + esc(device.display_name) + "</h2>" +
        "<p>" + esc(device.kind) + " · " + esc(device.health || "unknown") + "</p>" +
        "<p>" + esc(device.provider) + " / " + esc(device.provider_id) + "</p>" +
        "<p>Parent: " + esc(device.parent_id || "none") + "</p>" +
        "<p>" + badgeList(device.capabilities) + "</p></article>";
    }).join("");
  }

  async function refreshPerception() {
    try {
      const responses = await Promise.all([
        fetch("/api/perception/cameras", { cache: "no-store" }),
        fetch("/api/devices", { cache: "no-store" }),
        fetch("/api/perception/events?limit=30", { cache: "no-store" }),
        fetch("/api/perception/snapshots?limit=12", { cache: "no-store" }),
      ]);
      const payloads = [];
      for (let index = 0; index < responses.length; index += 1) {
        payloads.push(responses[index].ok ? await responses[index].json() : null);
      }
      renderCameras(payloads[0]);
      renderDevices(payloads[1]);
      renderDetections(payloads[2], "detection-list");
      renderDetections(payloads[2], "event-detections");
      renderSnapshots(payloads[3]);
    } catch (err) {
      setText("perception-status", "Perception UNKNOWN / OFFLINE");
      setText("overview-perception", "UNKNOWN / OFFLINE");
    }
  }

  function bindMissionStarts(root) {
    if (!root || typeof root.querySelectorAll !== "function") return;
    root.querySelectorAll("[data-mission-start]").forEach(function (button) {
      if (!button.addEventListener) return;
      button.addEventListener("click", function () {
        postMission("/api/missions/start", { mission: button.getAttribute("data-mission-start") });
      });
    });
  }

  async function postMission(url, body) {
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = response.ok ? await response.json() : {};
      const reason = payload.reason || (payload.accepted ? payload.state : "MISSION_REQUEST_FAILED");
      setText("mission-reason", reason || "none");
      refreshMissions();
    } catch (err) {
      setText("mission-reason", "MISSION_REQUEST_FAILED");
    }
  }

  function renderMissions(status, catalog, history) {
    const available = status && status.available === true;
    const active = status && status.active;
    setText("mission-status", available ? ("Mission Engine " + (status.state || "IDLE")) : "Missions UNKNOWN / OFFLINE");
    setText("overview-missions", available ? (status.state || "IDLE") : "UNKNOWN / OFFLINE");
    setText("mission-active", active ? (active.name || active.mission_id || "mission") : "none");
    setText("mission-step", active ? ((active.state || "unknown") + " · " + (active.step || "none")) : "none");
    const reason = (active && active.reason) || (status && status.reason) || "none";
    setText("mission-reason", reason);
    const list = document.getElementById("mission-list");
    if (list) {
      const missions = catalog && Array.isArray(catalog.missions) ? catalog.missions : [];
      list.innerHTML = missions.map(function (mission) {
        return '<article class="card"><h2>' + esc(mission.name || mission.id) + "</h2>" +
          "<p>" + esc(mission.id) + " · v" + esc(mission.version) + "</p>" +
          "<p>Target " + esc(mission.target) + " · final " + esc(mission.final_state) + "</p>" +
          '<div class="mission-actions"><button type="button" data-mission-start="' + esc(mission.id) + '">Start</button></div></article>';
      }).join("") || "<p>None</p>";
      bindMissionStarts(list);
    }
    const past = document.getElementById("mission-history");
    if (!past) return;
    const runs = history && Array.isArray(history.runs) ? history.runs : [];
    past.innerHTML = runs.map(function (run) {
      return "<p>" + esc(run.mission_id) + " · " + esc(run.state) + " · " + esc(run.reason || "none") +
        " · " + esc(run.started_at || "") + "</p>";
    }).join("") || "<p>None</p>";
  }

  async function refreshMissions() {
    try {
      const responses = await Promise.all([
        fetch("/api/missions/status", { cache: "no-store" }),
        fetch("/api/missions", { cache: "no-store" }),
        fetch("/api/missions/history?limit=20", { cache: "no-store" }),
      ]);
      const payloads = [];
      for (let index = 0; index < responses.length; index += 1) {
        payloads.push(responses[index].ok ? await responses[index].json() : null);
      }
      renderMissions(payloads[0], payloads[1], payloads[2]);
    } catch (err) {
      setText("mission-status", "Missions UNKNOWN / OFFLINE");
      setText("overview-missions", "UNKNOWN / OFFLINE");
    }
  }

  function plainNum(value) {
    return typeof value === "number" && Number.isFinite(value) ? String(value) : "—";
  }

  function zoneText(zones) {
    const names = [
      ["light_minutes", "light"],
      ["moderate_minutes", "moderate"],
      ["vigorous_minutes", "vigorous"],
      ["peak_minutes", "peak"],
    ];
    const parts = [];
    names.forEach(function (pair) {
      const value = zones && zones[pair[0]];
      if (typeof value === "number" && Number.isFinite(value)) parts.push(pair[1] + " " + value);
    });
    return parts.length ? parts.join(" · ") : "—";
  }

  function workoutCard(workout) {
    const zones = workout.heart_rate_zones || {};
    const gps = workout.has_gps === true ? "yes" : workout.has_gps === false ? "no" : "—";
    return '<article class="card"><h2>' + esc(workout.name || workout.exercise_type || "Workout") + "</h2>" +
      "<p>" + esc(workout.exercise_type || "type unavailable") + " · " + esc(workout.source || "fitbit") + "</p>" +
      "<p>Start " + esc(workout.start || "—") + "</p>" +
      "<p>End " + esc(workout.end || "—") + "</p>" +
      "<p>Duration " + esc(plainNum(workout.duration_minutes)) + " min</p>" +
      "<p>Distance " + esc(plainNum(workout.distance_miles)) + " mi</p>" +
      "<p>Calories " + esc(plainNum(workout.calories)) + "</p>" +
      "<p>Steps " + esc(plainNum(workout.steps)) + "</p>" +
      "<p>Average HR " + esc(plainNum(workout.average_heart_rate_bpm)) + " bpm</p>" +
      "<p>Active-zone minutes " + esc(plainNum(workout.active_zone_minutes)) + "</p>" +
      "<p>Zones " + esc(zoneText(zones)) + "</p>" +
      "<p>Pace " + esc(plainNum(workout.average_pace_minutes_per_mile)) + " min/mi</p>" +
      "<p>GPS " + esc(gps) + "</p>" +
      "<p>Device " + esc(workout.device || "—") + "</p>" +
      "<p>Platform " + esc(workout.platform || "—") + "</p>" +
      "<p>Recording " + esc(workout.recording_method || "—") + "</p></article>";
  }

  function summaryCard(summary) {
    const totals = (summary && summary.totals) || {};
    const zones = totals.heart_rate_zones || {};
    const daily = (summary && summary.daily_active_zone_minutes) || {};
    const dailyTotals = daily.totals || {};
    const timeInZone = (summary && summary.daily_time_in_heart_rate_zone) || {};
    const seconds = timeInZone.duration_seconds || {};
    const zoneLines = Object.keys(seconds).map(function (name) {
      return name + " " + plainNum(seconds[name]) + " s";
    }).join(" · ");
    const dailyLine = daily.available === false
      ? "Daily active-zone minutes unavailable"
      : "Daily active-zone minutes " + plainNum(dailyTotals.active_zone_minutes) +
        " · fat burn " + plainNum(dailyTotals.fat_burn_zone_minutes) +
        " · cardio " + plainNum(dailyTotals.cardio_zone_minutes) +
        " · peak " + plainNum(dailyTotals.peak_zone_minutes);
    const zoneLine = timeInZone.available === false
      ? "Daily time in heart-rate zone unavailable"
      : "Daily time in zone " + (zoneLines || "—");
    return '<article class="card"><h2>7-day totals</h2>' +
      "<p>" + esc(summary.workout_count) + " workouts · " + esc(summary.source || "fitbit") + "</p>" +
      "<p>" + esc(summary.start_date || "—") + " to " + esc(summary.end_date || "—") + "</p>" +
      "<p>Duration " + esc(plainNum(totals.duration_minutes)) + " min</p>" +
      "<p>Distance " + esc(plainNum(totals.distance_miles)) + " mi</p>" +
      "<p>Calories " + esc(plainNum(totals.calories)) + "</p>" +
      "<p>Steps " + esc(plainNum(totals.steps)) + "</p>" +
      "<p>Workout active-zone minutes " + esc(plainNum(totals.active_zone_minutes)) + "</p>" +
      "<p>Workout zones " + esc(zoneText(zones)) + "</p>" +
      "<p>" + esc(dailyLine) + "</p>" +
      "<p>" + esc(zoneLine) + "</p></article>";
  }

  function renderTraining(recent, summary) {
    const recentNode = document.getElementById("training-recent");
    const latestNode = document.getElementById("training-latest");
    const summaryNode = document.getElementById("training-summary");
    const online = recent && recent.available === true;
    const rows = online && Array.isArray(recent.workouts) ? recent.workouts : [];
    if (!online) {
      setText("training-status", "Training UNKNOWN / OFFLINE");
      setText("overview-training", "UNKNOWN / OFFLINE");
    } else {
      setText("training-status", "Fitbit read-only · " + rows.length + " recent");
      setText("overview-training", rows.length ? (rows[0].name || rows[0].exercise_type || "Workout") : "No recent workouts");
    }
    if (latestNode) latestNode.innerHTML = rows.length ? workoutCard(rows[0]) : '<p class="hint">No recent workouts.</p>';
    if (recentNode) {
      recentNode.innerHTML = rows.length
        ? rows.map(workoutCard).join("")
        : '<p class="hint">No recent workouts.</p>';
    }
    if (summaryNode) {
      summaryNode.innerHTML = summary && summary.available === true
        ? summaryCard(summary)
        : '<article class="card"><h2>7-day totals</h2><p>Unavailable</p></article>';
    }
  }

  async function refreshWorkouts() {
    try {
      const responses = await Promise.all([
        fetch("/api/workouts/recent?limit=8", { cache: "no-store" }),
        fetch("/api/workouts/summary?days=7", { cache: "no-store" }),
      ]);
      const recent = responses[0].ok ? await responses[0].json() : null;
      const summary = responses[1].ok ? await responses[1].json() : null;
      renderTraining(recent, summary);
    } catch (err) {
      setText("training-status", "Training UNKNOWN / OFFLINE");
      setText("overview-training", "UNKNOWN / OFFLINE");
    }
  }

  function showView(name) {
    if (!document.querySelectorAll) return;
    document.querySelectorAll(".view").forEach(function (view) {
      view.classList.toggle("active", view.id === "view-" + name);
    });
    document.querySelectorAll(".nav-button").forEach(function (button) {
      button.classList.toggle("active", button.getAttribute("data-view") === name);
    });
  }

  if (document.querySelectorAll) {
    document.querySelectorAll(".nav-button").forEach(function (button) {
      button.addEventListener("click", function () {
        showView(button.getAttribute("data-view"));
      });
    });
    document.querySelectorAll(".subtab").forEach(function (button) {
      button.addEventListener("click", function () {
        const tab = button.getAttribute("data-tab");
        if (!tab) return;
        const parent = button.parentElement;
        parent.querySelectorAll(".subtab").forEach(function (item) {
          item.classList.toggle("active", item === button);
        });
        ["cameras", "detections", "snapshots"].forEach(function (name) {
          const panel = document.getElementById("tab-" + name);
          if (panel) panel.classList.toggle("active", name === tab);
        });
      });
    });
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
      refreshPerception();
      refreshHome();
      refreshMissions();
      refreshWorkouts();
      els.fetchError.classList.add("hidden");
      els.refreshBadge.textContent = "refresh " + Math.round(REFRESH_MS / 1000) + "s";
      els.refreshBadge.className = "pill GREEN";
    } catch (err) {
      renderOperational(null);
      renderRoarm(null, {}, {});
      renderAuth([], [], {}, {});
      els.authSummary.textContent = "Connection lost — current status unavailable";
      els.fetchError.textContent = "UI fetch error: " + (err && err.message ? err.message : err);
      els.fetchError.classList.remove("hidden");
      els.refreshBadge.textContent = "refresh failed";
      els.refreshBadge.className = "pill RED";
    }
  }

  const missionStop = document.getElementById("mission-stop");
  if (missionStop && missionStop.addEventListener) {
    missionStop.addEventListener("click", function () {
      postMission("/api/missions/stop", {});
    });
  }

  tick();
  setInterval(tick, REFRESH_MS);
  if (typeof navigator !== "undefined" && navigator.serviceWorker && navigator.serviceWorker.register) {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  }
})();
