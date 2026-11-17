// Jobs view — standalone IIFE (no frameworks). Talks to /jobs* endpoints.
// Every data value goes through textContent (never raw HTML assignment).
(function () {
  "use strict";
  var body = document.getElementById("jobs-body");
  if (!body) return;

  var TABS = [["projects", "Projects"], ["features", "Features"],
              ["jobs", "Jobs"], ["digest", "Digest"]];
  var STATUSES = ["all", "stuck", "failed", "overdue", "running", "ok", "unknown"];

  var state = {
    tab: "projects",
    group: null,        // {level, id, title} when a rollup row filters Jobs
    status: "all",
    text: "",
    selected: null,     // selected job_id (Jobs tab detail)
  };
  var resolveCache = {}; // ticket_id -> {feature:{id}|null, project:{id}|null}
  var tableArea = null;  // Jobs tab table wrapper (kept while toolbar stays put)
  var lastJobs = null;   // last fetched jobs, so filtering need not refetch

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }

  function api(path) {
    return fetch(path).then(function (r) {
      if (!r.ok) throw new Error(path + " -> " + r.status);
      return r.json();
    });
  }
  function apiText(path) {
    return fetch(path).then(function (r) {
      if (!r.ok) throw new Error(path + " -> " + r.status);
      return r.text();
    });
  }

  function badge(status) {
    var s = status || "unknown";
    return el("span", "job-badge job-" + s, s);
  }

  function fmtTime(iso) { return iso ? iso.replace("T", " ").replace("Z", "") : "—"; }
  function fmtDur(sec) {
    if (sec == null) return "—";
    if (sec < 60) return Math.round(sec) + "s";
    if (sec < 3600) return (sec / 60).toFixed(1) + "m";
    return (sec / 3600).toFixed(1) + "h";
  }

  // Resolve a ticket to its nearest feature/project (self first, then up).
  function resolveTicket(tid) {
    if (!tid) return Promise.resolve({ feature: null, project: null });
    if (resolveCache[tid]) return Promise.resolve(resolveCache[tid]);
    return Promise.all([
      fetch("/tickets/" + encodeURIComponent(tid)).then(function (r) {
        return r.ok ? r.json() : null;
      }),
      fetch("/tickets/" + encodeURIComponent(tid) + "/lineage").then(function (r) {
        return r.ok ? r.json() : { ancestry: [] };
      }),
    ]).then(function (res) {
      var self = res[0], anc = (res[1] && res[1].ancestry) || [];
      // self first, then nearest ancestor (lineage.ancestry is root-first).
      var chain = [self].concat(anc.slice().reverse()).filter(Boolean);
      var out = { feature: null, project: null };
      ["feature", "project"].forEach(function (level) {
        for (var i = 0; i < chain.length; i++) {
          var tags = chain[i].tags || [];
          if (tags.indexOf(level) >= 0) {
            out[level] = { id: chain[i].id, title: chain[i].title };
            break;
          }
        }
      });
      resolveCache[tid] = out;
      return out;
    });
  }

  function tabsBar() {
    var bar = el("div", "job-tabs");
    TABS.forEach(function (t) {
      var b = el("button", "job-tab" + (state.tab === t[0] ? " active" : ""), t[1]);
      b.addEventListener("click", function () {
        state.tab = t[0];
        if (t[0] !== "jobs") state.group = null;
        render();
      });
      bar.appendChild(b);
    });
    return bar;
  }

  // ---- Projects / Features (rollup tables) --------------------------------

  function rollupTable(rows, level) {
    var table = el("table", "job-table");
    var head = el("tr");
    ["Title", "Status", "Jobs", "Runs", "Failed", "Overdue", "Success", "Last failure"]
      .forEach(function (h) { head.appendChild(el("th", null, h)); });
    table.appendChild(head);
    (rows || []).forEach(function (g) {
      var tr = el("tr", "job-row");
      tr.appendChild(el("td", "job-title", g.title));
      var st = el("td"); st.appendChild(badge(g.status)); tr.appendChild(st);
      tr.appendChild(el("td", null, g.jobs));
      tr.appendChild(el("td", null, g.runs));
      tr.appendChild(el("td", null, g.failed));
      tr.appendChild(el("td", null, g.overdue));
      tr.appendChild(el("td", null,
        g.success_rate == null ? "—" : Math.round(g.success_rate * 100) + "%"));
      tr.appendChild(el("td", "job-fail", g.last_failure
        ? g.last_failure.summary + " (" + fmtTime(g.last_failure.ended_at) + ")" : "—"));
      tr.addEventListener("click", function () {
        state.group = { level: level, id: g.id, title: g.title };
        state.tab = "jobs";
        state.selected = null;
        render();
      });
      table.appendChild(tr);
    });
    if (!(rows || []).length) return el("p", "job-muted", "No groups in the window.");
    return table;
  }

  // ---- Jobs table ----------------------------------------------------------

  function jobMatches(j) {
    if (state.status !== "all" && j.status !== state.status) return false;
    if (state.text) {
      var q = state.text.toLowerCase();
      if ((j.name + " " + j.job_id + " " + j.node).toLowerCase().indexOf(q) < 0)
        return false;
    }
    return true;
  }

  function problemsFirst(a, b) {
    var rank = { stuck: 5, failed: 4, overdue: 3, running: 2, ok: 1, unknown: 0 };
    return (rank[b.status] || 0) - (rank[a.status] || 0);
  }

  // Rebuild only the Jobs table area (toolbar + filter input stay in the DOM,
  // so the input keeps focus while typing).
  function refreshJobsTable() {
    if (!tableArea || !tableArea.isConnected || !lastJobs) return;
    tableArea.textContent = "";
    tableArea.appendChild(jobsTable(lastJobs));
  }

  function jobsToolbar() {
    var bar = el("div", "job-toolbar");
    var input = el("input", "job-filter");
    input.type = "search";
    input.placeholder = "Filter jobs…";
    input.value = state.text;
    input.addEventListener("input", function () {
      state.text = input.value;
      refreshJobsTable();
    });
    bar.appendChild(input);
    var chips = el("div", "job-chips");
    STATUSES.forEach(function (s) {
      var c = el("button", "job-chip" + (state.status === s ? " on" : ""), s);
      c.addEventListener("click", function () { state.status = s; render(); });
      chips.appendChild(c);
    });
    bar.appendChild(chips);
    if (state.group) {
      var g = el("button", "job-chip on", "group: " + state.group.title + " ✕");
      g.title = "Clear group filter";
      g.addEventListener("click", function () { state.group = null; render(); });
      bar.appendChild(g);
    }
    return bar;
  }

  function jobDetail(job) {
    var wrap = el("div", "job-detail");
    wrap.appendChild(el("h3", null, job.name));
    wrap.appendChild(el("div", "job-muted", job.job_id + " · " + job.node + " · " + job.kind));
    if (!job.runs || !job.runs.length) {
      wrap.appendChild(el("p", "job-muted", "No runs yet."));
      return wrap;
    }
    job.runs.forEach(function (r) {
      var card = el("div", "job-run");
      var top = el("div", "job-run-top");
      top.appendChild(badge(r.status));
      top.appendChild(el("span", null, fmtTime(r.started_at) + " · " + fmtDur(r.duration_s)));
      if (r.summary) top.appendChild(el("span", "job-run-sum", r.summary));
      card.appendChild(top);
      if (r.log_tail) {
        var pre = el("pre", "job-log");
        pre.textContent = r.log_tail;   // textContent only
        var toggle = el("button", "job-log-toggle", "show log");
        pre.classList.add("hidden");
        toggle.addEventListener("click", function () {
          var show = pre.classList.toggle("hidden");
          toggle.textContent = show ? "show log" : "hide log";
        });
        card.appendChild(toggle);
        card.appendChild(pre);
      }
      wrap.appendChild(card);
    });
    return wrap;
  }

  function jobsTable(jobs) {
    var list = jobs.filter(jobMatches).slice().sort(problemsFirst);
    var table = el("table", "job-table");
    var head = el("tr");
    ["Name", "Node", "Kind", "Schedule", "Status", "Last run", "Dur",
     "Next due", "24h", "Fails"].forEach(function (h) { head.appendChild(el("th", null, h)); });
    table.appendChild(head);
    list.forEach(function (j) {
      var last = j.last_run || {};
      var tr = el("tr", "job-row" + (state.selected === j.job_id ? " active" : ""));
      tr.appendChild(el("td", "job-title", j.name));
      tr.appendChild(el("td", null, j.node));
      tr.appendChild(el("td", null, j.kind));
      tr.appendChild(el("td", null, j.schedule || "—"));
      var st = el("td"); st.appendChild(badge(j.status)); tr.appendChild(st);
      tr.appendChild(el("td", null, fmtTime(last.started_at)));
      tr.appendChild(el("td", null, fmtDur(last.duration_s)));
      tr.appendChild(el("td", null, fmtTime(j.next_due)));
      tr.appendChild(el("td", null, j.runs_24h));
      tr.appendChild(el("td", null, j.fails_24h));
      tr.addEventListener("click", function () {
        state.selected = state.selected === j.job_id ? null : j.job_id;
        refreshJobsTable();
      });
      table.appendChild(tr);
    });
    var out = el("div");
    out.appendChild(table);
    if (!list.length) out.appendChild(el("p", "job-muted", "No jobs match."));
    if (state.selected) {
      var sel = jobs.filter(function (j) { return j.job_id === state.selected; })[0];
      if (sel) out.appendChild(jobDetail(sel));
    }
    return out;
  }

  // ---- Digest --------------------------------------------------------------

  function digestView() {
    return apiText("/jobs/digest?format=md").then(function (md) {
      var pre = el("pre", "job-digest");
      pre.textContent = md;   // textContent only
      return pre;
    }).catch(function (e) { return el("p", "job-muted", "Digest failed: " + e.message); });
  }

  // ---- render --------------------------------------------------------------

  function render() {
    body.textContent = "";
    body.appendChild(tabsBar());
    var content = el("div", "job-content");
    body.appendChild(content);

    if (state.tab === "digest") {
      digestView().then(function (node) { content.appendChild(node); });
      return;
    }
    if (state.tab === "projects" || state.tab === "features") {
      var level = state.tab === "projects" ? "project" : "feature";
      api("/jobs/rollup?level=" + level).then(function (rows) {
        content.appendChild(rollupTable(rows, level));
      }).catch(function (e) { content.appendChild(el("p", "job-muted", e.message)); });
      return;
    }
    // Jobs tab (maybe filtered to a group).
    api("/jobs").then(function (jobs) {
      if (state.group) {
        return Promise.all(jobs.map(function (j) {
          return resolveTicket(j.ticket_id).then(function (r) {
            var node = r && r[state.group.level];
            j._gid = node ? node.id : null;
            return j;
          });
        })).then(function (resolved) {
          return resolved.filter(function (j) { return j._gid === state.group.id; });
        });
      }
      return jobs;
    }).then(function (jobs) {
      content.appendChild(jobsToolbar());
      lastJobs = jobs || [];
      tableArea = el("div", "job-table-area");
      tableArea.appendChild(jobsTable(lastJobs));
      content.appendChild(tableArea);
    }).catch(function (e) { content.appendChild(el("p", "job-muted", e.message)); });
  }

  function isVisible() {
    var v = document.getElementById("view-jobs");
    return v && !v.classList.contains("hidden");
  }

  // Re-render on Jobs nav click and every 30s while visible (never at load
  // while the view is hidden — SPEC: fetch only when visible).
  var navBtn = document.querySelector('[data-view="jobs"]');
  if (navBtn) navBtn.addEventListener("click", function () { setTimeout(render, 0); });
  setInterval(function () { if (!document.hidden && isVisible()) render(); }, 30000);
  if (isVisible()) render();
})();
