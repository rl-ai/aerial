/* ticket-hub SPA — no build step, fetch-based against /api contract. */
(() => {
  "use strict";

  const STATUSES = ["draft", "open", "running", "blocked", "review", "done", "cancelled"];
  const label = (s) => s ? s[0].toUpperCase() + s.slice(1) : s;
  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).error?.message || msg; } catch {}
      throw new Error(msg);
    }
    return res.json();
  }

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.add("hidden"), 2600);
  }

  // ---------------------------------------------------------------- state
  const state = {
    view: "board",
    tickets: [],
    childCounts: {},
    parents: {},            // board: child id -> parent id
    statusOf: {},           // board: id -> status
    collapsed: new Set(),   // graph: ticket ids whose children are hidden
    boardCollapsed: new Set(JSON.parse(localStorage.getItem("hub-board-collapsed") || "[]")),
    typesOn: new Set(),     // empty = all types
    topLevelOnly: localStorage.getItem("hub-board-toponly") === "1",
    drawerId: null,
    query: "",
  };

  // ticket "kind" inferred from template/workflow tags
  const TICKET_TYPES = [
    { key: "product",  label: "Product",  tags: ["feature", "pmf", "design", "accept"] },
    { key: "bug",      label: "Bug",      tags: ["bugfix", "triage-bug"] },
    { key: "infra",    label: "Infra",    tags: ["infra", "monitor", "service"] },
    { key: "research", label: "Research", tags: ["spike", "research", "wiki"] },
    { key: "project",  label: "Project",  tags: ["project", "milestone", "charter"] },
    { key: "system",   label: "System",   tags: ["forge", "lane", "promote", "build", "verify", "integration"] },
    { key: "biz",      label: "Biz",      tags: ["bizdev", "sizing", "decision"] },
  ];
  function ticketType(t) {
    const tags = (t.tags || []);
    for (const ty of TICKET_TYPES)
      if (tags.some((g) => ty.tags.includes(g))) return ty;
    return { key: "other", label: "Other" };
  }

  // ---------------------------------------------------------------- nav
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });

  function switchView(view) {
    state.view = view;
    document.querySelectorAll(".nav-item").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === view));
    document.querySelectorAll(".view").forEach((v) =>
      v.classList.toggle("hidden", v.id !== "view-" + view));
    const titles = {
      board: "Board", graph: "Graph", updates: "Updates",
      wiki: "Wiki", monitoring: "Monitoring", admin: "Admin", roles: "Roles",
      jobs: "Jobs",
    };
    $("#view-title").textContent = titles[view] || label(view);
    refresh();
  }

  $("#search").addEventListener("input", (e) => {
    state.query = e.target.value.trim().toLowerCase();
    if (state.view === "board") renderBoard();
    if (state.view === "updates") renderUpdates();
  });

  // ---------------------------------------------------------------- board
  async function loadBoard() {
    const data = await api("/board");
    state.columns = data.columns || {};
    state.childCounts = data.child_counts || {};
    state.parents = data.parents || {};
    state.statusOf = {};
    for (const list of Object.values(state.columns))
      for (const t of list) state.statusOf[t.id] = t.status;
  }

  function matchesQuery(t) {
    if (!state.query) return true;
    return (t.title + " " + (t.body || "") + " " + t.id + " " + (t.tags || []).join(" "))
      .toLowerCase().includes(state.query);
  }

  // a child stays visible when searching or when its type filter matches
  function passesType(t) {
    return !state.typesOn.size || state.typesOn.has(ticketType(t).key);
  }

  function renderBoard() {
    const root = $("#board-columns");
    root.textContent = "";
    renderFilterBar();
    for (const status of STATUSES) {
      const col = el("div", "column");
      const head = el("div", "column-head");
      head.append(el("span", `column-dot st-${status}`));
      head.append(el("span", null, status));
      const all = (state.columns?.[status] || []).filter(matchesQuery);
      // signal filters: top-level only hides children whose parent is also
      // on the board; search always defeats it (you asked, you get the hit).
      let tickets = all.filter(passesType);
      // collapse: hide a card when any ancestor is collapsed
      if (!state.query)
        tickets = tickets.filter((t) =>
          !ancestorsOf(t.id).some((a) => state.boardCollapsed.has(a)));
      if (state.topLevelOnly && !state.query)
        tickets = tickets.filter((t) => !state.parents[t.id]
                                     || !(state.parents[t.id] in state.statusOf));
      head.append(el("span", "column-count", String(tickets.length)));
      col.append(head);
      const cards = el("div", "column-cards");
      for (const t of tickets) cards.append(ticketCard(t));
      col.append(cards);
      root.append(col);
    }
  }

  function renderFilterBar() {
    const bar = $("#board-filters");
    if (!bar) return;
    bar.textContent = "";
    const counts = {};
    for (const list of Object.values(state.columns))
      for (const t of list) {
        const k = ticketType(t).key;
        counts[k] = (counts[k] || 0) + 1;
      }
    for (const ty of [...TICKET_TYPES, { key: "other", label: "Other" }]) {
      if (!counts[ty.key]) continue;
      const b = el("button", `type-chip type-${ty.key}` +
                   (state.typesOn.has(ty.key) ? " on" : ""),
                   `${ty.label} ${counts[ty.key]}`);
      b.addEventListener("click", () => {
        state.typesOn.has(ty.key) ? state.typesOn.delete(ty.key)
                                  : state.typesOn.add(ty.key);
        renderBoard();
      });
      bar.append(b);
    }
    const top = el("button", "type-chip" + (state.topLevelOnly ? " on" : ""),
                   "Top level only");
    top.addEventListener("click", () => {
      state.topLevelOnly = !state.topLevelOnly;
      localStorage.setItem("hub-board-toponly", state.topLevelOnly ? "1" : "0");
      renderBoard();
    });
    bar.append(top);
    if (state.boardCollapsed.size) {
      const exp = el("button", "type-chip", "Expand all");
      exp.addEventListener("click", () => {
        state.boardCollapsed.clear();
        localStorage.setItem("hub-board-collapsed", "[]");
        renderBoard();
      });
      bar.append(exp);
    }
  }

  function ancestorsOf(id) {
    const out = [];
    let p = state.parents[id];
    const seen = new Set();
    while (p && !seen.has(p)) { out.push(p); seen.add(p); p = state.parents[p]; }
    return out;
  }

  function ticketCard(t) {
    const kids = state.childCounts[t.id] || 0;
    const collapsed = state.boardCollapsed.has(t.id);
    const card = el("div", "card" + (collapsed ? " is-collapsed" : ""));
    const titleRow = el("div", "card-title-row");
    if (kids) {
      const tog = el("button", "collapse-toggle" + (collapsed ? " collapsed" : ""),
                     collapsed ? "▸" : "▾");
      tog.title = collapsed ? "Expand children" : "Collapse children";
      tog.addEventListener("click", (e) => {
        e.stopPropagation();
        collapsed ? state.boardCollapsed.delete(t.id)
                  : state.boardCollapsed.add(t.id);
        localStorage.setItem("hub-board-collapsed",
                             JSON.stringify([...state.boardCollapsed]));
        renderBoard();
      });
      titleRow.append(tog);
    }
    titleRow.append(el("div", "card-title", t.title));
    card.append(titleRow);
    const meta = el("div", "card-meta");
    meta.append(el("span", `status-pill st-${t.status}`, label(t.status)));
    const ty = ticketType(t);
    if (ty.key !== "other")
      meta.append(el("span", `type-chip sm type-${ty.key}`, ty.label));
    const owner = t.owner || {};
    if (owner.family && owner.family !== "unknown")
      meta.append(el("span", "chip chip-family", owner.family));
    if (t.priority && t.priority !== "medium")
      meta.append(el("span", `prio prio-${t.priority}`, t.priority));
    if (kids) {
      const done = (state.columns?.done || []).filter(
        (d) => state.parents[d.id] === t.id).length;
      const cc = el("span", "child-count", `${done}/${kids} done`);
      if (collapsed) cc.classList.add("dim");
      meta.append(cc);
    }
    card.append(meta);
    card.addEventListener("click", () => openDrawer(t.id));
    return card;
  }

  // ---------------------------------------------------------------- graph
  async function loadTickets() {
    state.tickets = await api("/tickets");
  }

  async function childrenOf(id) {
    try { return await api(`/tickets/${id}/lineage`); } catch { return null; }
  }

  async function renderGraph() {
    const svg = $("#graph-svg");
    svg.textContent = "";
    await loadTickets();
    const empty = $("#graph-empty");
    if (!state.tickets.length) { empty.classList.remove("hidden"); return; }
    empty.classList.add("hidden");

    // Build full tree of parent_of relations via each ticket's ancestry.
    const lineages = {};
    for (const t of state.tickets) lineages[t.id] = await childrenOf(t.id);
    const childMap = {};
    for (const t of state.tickets) childMap[t.id] = (lineages[t.id]?.tree?.children) || [];
    const isChild = new Set();
    for (const t of state.tickets)
      for (const c of childMap[t.id]) isChild.add(c.ticket.id);
    const roots = state.tickets.filter((t) => !isChild.has(t.id));

    const NODE_W = 220, NODE_H = 56, GAP_X = 40, GAP_Y = 20;
    const positions = [];
    function layout(node, depth) {
      const id = node.ticket.id;
      const kids = state.collapsed.has(id) ? [] : (childMap[id] || []);
      if (!kids.length) {
        const pos = { node, depth, slot: layout.next++ };
        positions.push(pos);
        return { top: pos.slot, bottom: pos.slot };
      }
      const ranges = kids.map((k) => layout(k, depth + 1));
      const top = ranges[0].top, bottom = ranges[ranges.length - 1].bottom;
      const mid = (top + bottom) / 2;
      positions.push({ node, depth, slot: mid });
      return { top, bottom };
    }
    layout.next = 0;
    const forest = roots.map((t) => ({ ticket: t, children: [] }));
    const ranges = forest.map((r) => layout(r, 0));

    const totalSlots = Math.max(layout.next, 1);
    const maxDepth = Math.max(...positions.map((p) => p.depth), 0);
    const W = (maxDepth + 1) * (NODE_W + GAP_X) + 60;
    const H = totalSlots * (NODE_H + GAP_Y) + 60;
    svg.setAttribute("width", W);
    svg.setAttribute("height", H);
    const xy = (p) => [50 + p.depth * (NODE_W + GAP_X), 40 + p.slot * (NODE_H + GAP_Y)];
    const byId = {};
    positions.forEach((p) => { byId[p.node.ticket.id] = p; });

    const NS = "http://www.w3.org/2000/svg";
    // edges first
    for (const p of positions) {
      const kids = state.collapsed.has(p.node.ticket.id) ? [] : (childMap[p.node.ticket.id] || []);
      for (const k of kids) {
        const q = byId[k.ticket.id];
        if (!q) continue;
        const [x1, y1] = xy(p), [x2, y2] = xy(q);
        const path = document.createElementNS(NS, "path");
        const sx = x1 + NODE_W, sy = y1 + NODE_H / 2, tx = x2, ty = y2 + NODE_H / 2;
        path.setAttribute("d", `M ${sx} ${sy} C ${sx + GAP_X / 2} ${sy}, ${tx - GAP_X / 2} ${ty}, ${tx} ${ty}`);
        path.setAttribute("class", "edge");
        svg.append(path);
      }
    }
    // nodes
    for (const p of positions) {
      const t = p.node.ticket;
      const [x, y] = xy(p);
      const g = document.createElementNS(NS, "g");
      const rect = document.createElementNS(NS, "rect");
      rect.setAttribute("x", x); rect.setAttribute("y", y);
      rect.setAttribute("width", NODE_W); rect.setAttribute("height", NODE_H);
      rect.setAttribute("rx", 7);
      rect.setAttribute("class", "node-box");
      const dot = document.createElementNS(NS, "circle");
      dot.setAttribute("cx", x + 14); dot.setAttribute("cy", y + 18); dot.setAttribute("r", 4);
      dot.setAttribute("fill", `var(--st-${t.status.toLowerCase()})`);
      const title = document.createElementNS(NS, "text");
      title.setAttribute("x", x + 26); title.setAttribute("y", y + 22);
      title.setAttribute("class", "node-title");
      title.textContent = t.title.length > 26 ? t.title.slice(0, 25) + "…" : t.title;
      const sub = document.createElementNS(NS, "text");
      sub.setAttribute("x", x + 14); sub.setAttribute("y", y + 41);
      sub.setAttribute("class", "node-sub");
      sub.textContent = t.id;
      g.append(rect, dot, title, sub);
      rect.addEventListener("click", () => openDrawer(t.id));
      const kids = childMap[t.id] || [];
      if (kids.length) {
        const tg = document.createElementNS(NS, "g");
        tg.setAttribute("class", "node-toggle");
        const c = document.createElementNS(NS, "circle");
        c.setAttribute("cx", x + NODE_W); c.setAttribute("cy", y + NODE_H / 2); c.setAttribute("r", 9);
        const label = document.createElementNS(NS, "text");
        label.setAttribute("x", x + NODE_W); label.setAttribute("y", y + NODE_H / 2 + 4);
        label.setAttribute("text-anchor", "middle");
        label.textContent = state.collapsed.has(t.id) ? `+${kids.length}` : "–";
        tg.append(c, label);
        tg.addEventListener("click", (e) => {
          e.stopPropagation();
          state.collapsed.has(t.id) ? state.collapsed.delete(t.id) : state.collapsed.add(t.id);
          renderGraph();
        });
        g.append(tg);
      }
      svg.append(g);
    }
  }

  // ---------------------------------------------------------------- drawer
  async function openDrawer(id) {
    state.drawerId = id;
    const drawer = $("#drawer");
    drawer.classList.remove("hidden");
    $("#drawer-body").textContent = "";
    $("#drawer-id").textContent = id;
    $("#drawer-title").textContent = "…";
    let ticket, lin, bindings, plan, assignment;
    try {
      [ticket, lin] = await Promise.all([
        api(`/tickets/${id}`),
        api(`/tickets/${id}/lineage`),
      ]);
      try { bindings = await api(`/tickets/${id}/bindings`); } catch { bindings = []; }
      try { plan = await api(`/tickets/${id}/plan`); } catch { plan = null; }
      try { assignment = await api(`/tickets/${id}/assignment`); } catch { assignment = null; }
    } catch (err) {
      $("#drawer-body").append(el("p", "muted", "Failed to load: " + err.message));
      return;
    }
    $("#drawer-title").textContent = ticket.title;
    const body = $("#drawer-body");

    // lineage breadcrumb
    const sec1 = el("div", "drawer-section");
    sec1.append(el("h3", null, "Lineage"));
    const crumbs = el("div", "breadcrumb");
    const chain = [...(lin.ancestry || [])].reverse();
    chain.forEach((a) => {
      const link = el("a", null, a.id);
      link.addEventListener("click", () => openDrawer(a.id));
      crumbs.append(link, el("span", "sep", "›"));
    });
    crumbs.append(el("span", "current", ticket.id));
    sec1.append(crumbs);
    if ((lin.related || []).length) {
      const rel = el("div", "muted");
      rel.textContent = "Related: " + lin.related.map((r) => r.id).join(", ");
      sec1.append(rel);
    }
    body.append(sec1);

    // details
    const sec2 = el("div", "drawer-section");
    sec2.append(el("h3", null, "Details"));
    const kv = el("dl", "kv");
    const add = (k, v) => { kv.append(el("dt", null, k), el("dd", null, v)); };
    add("Status", ticket.status);
    add("Priority", ticket.priority);
    add("Owner", `${ticket.owner?.name || "—"}${ticket.owner?.family ? " · " + ticket.owner.family : ""}`);
    if (assignment && assignment.holder)
      add("Holder", `${assignment.holder.seat} @ ${assignment.holder.role}`);
    add("Tags", (ticket.tags || []).join(", ") || "—");
    add("Updated", (ticket.updated_at || "").replace("T", " ").slice(0, 16));
    sec2.append(kv);
    if ((ticket.artifacts || []).length) {
      const art = el("div", "muted");
      art.textContent = "Artifacts: " + ticket.artifacts.map((a) => a.name || a).join(", ");
      sec2.append(art);
    }
    body.append(sec2);

    // plan: progress, blocked by owner, remaining
    if (plan) {
      const secP = el("div", "drawer-section");
      secP.append(el("h3", null, "Plan"));
      const p = plan.progress || {};
      const bar = el("div", "plan-bar");
      const fill = el("div", "plan-bar-fill");
      fill.style.width = (p.percent || 0) + "%";
      bar.append(fill);
      secP.append(bar);
      secP.append(el("div", "plan-pct",
        `${p.percent || 0}% — ${p.done || 0}/${p.total || 0} work items done`));
      if (plan.summary) {
        const sum = el("p", "plan-summary");
        sum.textContent = plan.summary;
        secP.append(sum);
      }
      if ((plan.blocked_by_owner || []).length) {
        const bl = el("div", "plan-blocked");
        bl.append(el("h4", null, `Blocked by owner (${plan.blocked_by_owner.length})`));
        for (const b of plan.blocked_by_owner.slice(0, 10)) {
          const row = el("div", "plan-item");
          const link = el("a", null, b.title);
          link.addEventListener("click", () => openDrawer(b.id));
          row.append(el("span", "plan-bang", "!"), link,
                     el("span", "muted", " " + b.status));
          bl.append(row);
        }
        secP.append(bl);
      }
      const rem = plan.remaining_items || [];
      if (rem.length) {
        const rm = el("div");
        rm.append(el("h4", null, `Remaining (${rem.length})`));
        for (const r of rem.slice(0, 15)) {
          const row = el("div", "plan-item");
          const link = el("a", null, r.title);
          link.addEventListener("click", () => openDrawer(r.id));
          row.append(el("span", `status-pill st-${r.status}`, r.status), link,
                     r.owner ? el("span", "muted", " · " + r.owner) : el("span"));
          rm.append(row);
        }
        if (rem.length > 15) rm.append(el("div", "muted", `… ${rem.length - 15} more`));
        secP.append(rm);
      }
      if ((plan.remaining_notes || []).length) {
        const rn = el("div");
        rn.append(el("h4", null, "Remaining (noted in plan)"));
        for (const item of plan.remaining_notes.slice(0, 8))
          rn.append(el("div", "plan-item", "· " + item));
        secP.append(rn);
      }
      body.append(secP);
    }

    // bound threads
    const sec3 = el("div", "drawer-section");
    sec3.append(el("h3", null, "Bound threads"));
    if (bindings && bindings.length) {
      for (const b of bindings) {
        const row = el("div", "thread-row");
        row.append(el("span", "chip platform-chip", b.platform));
        row.append(el("span", null, b.room_id));
        if (b.thread_id) row.append(el("span", "muted", "· " + b.thread_id));
        sec3.append(row);
      }
    } else {
      sec3.append(el("div", "muted", "No threads bound."));
    }
    body.append(sec3);

    // event timeline
    const sec4 = el("div", "drawer-section");
    sec4.append(el("h3", null, "Timeline"));
    const list = el("ul", "timeline");
    const events = [...(lin.events || [])].sort((a, b) =>
      (b.ts || "").localeCompare(a.ts || ""));
    if (!events.length) list.append(el("li", "muted", "No events."));
    for (const ev of events) {
      const li = el("li", "t-event");
      li.append(el("span", `t-dot k-${ev.kind}`));
      const head = el("div", "t-kind");
      head.textContent = ev.kind.replace(/_/g, " ");
      const actor = ev.actor?.name;
      if (actor && actor !== "unknown") {
        const a = el("span", "t-actor", " · " + actor);
        head.append(a);
      }
      li.append(head);
      li.append(el("div", "t-text", eventText(ev)));
      li.append(el("div", "t-meta", `${ev.id} · ${(ev.ts || "").replace("T", " ").slice(0, 19)}`));
      list.append(li);
    }
    sec4.append(list);
    body.append(sec4);
  }

  function eventText(ev) {
    const p = ev.payload || {};
    switch (ev.kind) {
      case "status_changed": return `${p.from || "?"} → ${p.to || "?"}`;
      case "artifact": return p.name || "artifact produced";
      case "decision": return p.text || "decision recorded";
      case "approval_receipt": return "approved by " + (p.approver || ev.actor?.name || "?");
      case "thread_message": return `[${p.platform || "chat"}] ${p.text || ""}`;
      case "created": return p.title ? `Created “${p.title}”` : "Ticket created";
      case "updated": return (p.fields || []).length ? "Updated " + p.fields.join(", ") : "Updated";
      case "comment": return p.text || "";
      default: return p.text || JSON.stringify(p);
    }
  }

  $("#drawer-close").addEventListener("click", () => {
    $("#drawer").classList.add("hidden");
    state.drawerId = null;
  });

  // ---------------------------------------------------------------- updates
  async function renderUpdates() {
    await loadTickets();
    const select = $("#updates-project");
    const prev = select.value;
    // projects = root tickets (no ancestry)
    const projects = [];
    for (const t of state.tickets) {
      try {
        const lin = await api(`/tickets/${t.id}/lineage`);
        if (!(lin.ancestry || []).length) projects.push(t);
      } catch {}
    }
    select.textContent = "";
    if (!projects.length) {
      $("#updates-body").textContent = "";
      $("#updates-body").append(el("p", "muted", "No projects yet."));
      return;
    }
    for (const p of projects) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.title;
      select.append(opt);
    }
    if (prev && projects.some((p) => p.id === prev)) select.value = prev;
    await showDigest(select.value);
  }

  async function showDigest(projectId) {
    const body = $("#updates-body");
    body.textContent = "";
    let digest;
    try { digest = await api(`/tickets/${projectId}/digest?format=json`); }
    catch (err) { body.append(el("p", "muted", err.message)); return; }
    const head = el("div", "digest-head");
    head.append(el("h2", null, digest.project_title || digest.project));
    head.append(el("div", "digest-date",
      (digest.updated_at || "").replace("T", " ").slice(0, 16) + " · " + digest.project));
    body.append(head);
    const ul = el("ul", "digest-bullets");
    if (!digest.bullets.length) ul.append(el("li", "muted", "No updates yet."));
    for (const b of digest.bullets) {
      const li = el("li");
      li.append(document.createTextNode(b.text));
      li.append(el("span", "cite", b.event_ids.join(" ")));
      ul.append(li);
    }
    body.append(ul);
    $("#copy-digest-btn").onclick = async () => {
      const res = await fetch(`/tickets/${projectId}/digest?format=md`);
      await navigator.clipboard.writeText(await res.text());
      toast("Digest copied");
    };
  }

  $("#updates-project").addEventListener("change", (e) => showDigest(e.target.value));

  // ---------------------------------------------------------------- helpers
  function apiSafe(path, opts) {
    // Like api() but resolves to null on any failure (endpoint absent yet).
    return api(path, opts).catch(() => null);
  }
  function fmtTs(ts) {
    if (!ts) return "unknown";
    return String(ts).replace("T", " ").slice(0, 19);
  }
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText)
      return navigator.clipboard.writeText(text).then(() => toast("Copied"));
    return Promise.reject(new Error("clipboard unavailable"));
  }
  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  // ---------------------------------------------------------------- markdown
  // Small dependency-free renderer: headings, bold/italic, inline code,
  // fenced code, links, [[WikiLink]] tokens, lists, blockquotes.
  function renderMarkdown(md) {
    const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
                        .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    function inline(s) {
      s = esc(s);
      s = s.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
      s = s.replace(/\[\[([^\]]+)\]\]/g, (_, w) => {
        const slug = w.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");
        return `<a class="wikilink" data-slug="${slug}">${esc(w.trim())}</a>`;
      });
      s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+|\/[^)\s]*)\)/g,
        (_, txt, href) => `<a href="${href}" target="_blank" rel="noopener">${txt}</a>`);
      s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
      return s;
    }
    const lines = String(md || "").split(/\r?\n/);
    const out = [];
    let i = 0, para = [], list = null, quote = [];
    const flushPara = () => {
      if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; }
    };
    const flushList = () => {
      if (list) {
        out.push(`<${list.tag}>` + list.items.map((it) => `<li>${inline(it)}</li>`).join("") + `</${list.tag}>`);
        list = null;
      }
    };
    const flushQuote = () => {
      if (quote.length) { out.push(`<blockquote>${quote.map((q) => `<p>${inline(q)}</p>`).join("")}</blockquote>`); quote = []; }
    };
    const flushAll = () => { flushPara(); flushList(); flushQuote(); };
    while (i < lines.length) {
      const line = lines[i];
      if (/^```/.test(line.trim())) {                    // fenced code
        flushAll();
        const code = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i].trim())) { code.push(lines[i]); i++; }
        i++;
        out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
        continue;
      }
      const h = line.match(/^(#{1,6})\s+(.*)/);
      if (h) { flushAll(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i++; continue; }
      const ul = line.match(/^\s*[-*+]\s+(.*)/);
      if (ul) {
        flushPara(); flushQuote();
        if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; }
        list.items.push(ul[1]); i++; continue;
      }
      const ol = line.match(/^\s*\d+[.)]\s+(.*)/);
      if (ol) {
        flushPara(); flushQuote();
        if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; }
        list.items.push(ol[1]); i++; continue;
      }
      const bq = line.match(/^\s*>\s?(.*)/);
      if (bq) { flushPara(); flushList(); quote.push(bq[1]); i++; continue; }
      if (/^\s*$/.test(line)) { flushAll(); i++; continue; }
      flushList(); flushQuote();
      para.push(line); i++;
    }
    flushAll();
    return out.join("\n");
  }

  function mountMarkdown(container, md) {
    container.innerHTML = renderMarkdown(md);
    container.querySelectorAll("a.wikilink").forEach((a) => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        openWikiPage(a.dataset.slug);
      });
    });
  }

  // ---------------------------------------------------------------- wiki
  const wiki = {
    pages: [],
    slug: null,
    searchSeq: 0,
    loaded: false,
  };

  async function renderWiki() {
    if (!wiki.loaded) {
      const pages = await apiSafe("/wiki");
      wiki.pages = Array.isArray(pages) ? pages : [];
      wiki.loaded = true;
    }
    renderWikiSidebar(wiki.pages);
    if (wiki.slug) await openWikiPage(wiki.slug, true);
  }

  function renderWikiSidebar(pages) {
    const root = $("#wiki-categories");
    root.textContent = "";
    if (!pages.length) {
      root.append(el("div", "muted", "No wiki pages yet."));
      return;
    }
    const groups = {};
    for (const p of pages) {
      const cat = (p.tags && p.tags.length) ? label(p.tags[0]) : "Uncategorized";
      (groups[cat] = groups[cat] || []).push(p);
    }
    for (const cat of Object.keys(groups).sort()) {
      const sec = el("div", "wiki-cat");
      sec.append(el("div", "wiki-cat-name", cat));
      for (const p of groups[cat]) {
        const item = el("button", "wiki-page-link" + (p.slug === wiki.slug ? " active" : ""), p.title || p.slug);
        item.addEventListener("click", () => openWikiPage(p.slug));
        sec.append(item);
      }
      root.append(sec);
    }
  }

  async function openWikiPage(slug, keepSearch) {
    wiki.slug = slug;
    if (!keepSearch) { $("#wiki-search").value = ""; }
    closeWikiEditor();
    renderWikiSidebar(wiki.pages.length ? wiki.pages : []);
    const content = $("#wiki-content");
    content.textContent = "";
    content.append(el("p", "muted", "Loading…"));
    const data = await apiSafe(`/wiki/${encodeURIComponent(slug)}`);
    if (!data || !data.page) {
      content.textContent = "";
      content.append(el("p", "muted", "Page not found (wiki API unavailable?)."));
      return;
    }
    const page = data.page;
    const head = el("div", "wiki-meta");
    head.append(el("h2", "wiki-page-title", page.title || page.slug));
    const metaRow = el("div", "wiki-meta-row");
    metaRow.append(el("span", "wiki-rev", `rev ${page.rev != null ? page.rev : "?"}`));
    metaRow.append(el("span", "wiki-actor", page.actor || "unknown"));
    metaRow.append(el("span", "wiki-ts", fmtTs(page.ts)));
    (page.tags || []).forEach((t) => metaRow.append(el("span", "chip", t)));
    const editBtn = el("button", "btn wiki-edit-btn", "Edit");
    editBtn.addEventListener("click", () => openWikiEditor(page));
    metaRow.append(editBtn);
    head.append(metaRow);
    content.append(head);

    const body = el("div", "wiki-body");
    mountMarkdown(body, page.body_md || "");
    content.append(body);

    const hist = el("div", "wiki-history");
    hist.append(el("h3", null, "History"));
    const items = Array.isArray(data.history) ? data.history : [];
    if (!items.length) hist.append(el("div", "muted", "No history."));
    for (const h of items) {
      const row = el("div", "wiki-hist-row");
      row.append(el("span", "wiki-rev", `rev ${h.rev != null ? h.rev : "?"}`));
      row.append(el("span", null, h.actor || "unknown"));
      row.append(el("span", "muted", fmtTs(h.ts)));
      if (h.rev !== page.rev) {
        row.append(el("span", "wiki-hist-note", "older-rev rendering pending"));
      } else {
        row.append(el("span", "wiki-hist-note", "current"));
      }
      hist.append(row);
    }
    content.append(hist);
  }

  function openWikiEditor(page) {
    $("#wiki-edit-title").value = page.title || "";
    $("#wiki-edit-tags").value = (page.tags || []).join(", ");
    $("#wiki-edit-body").value = page.body_md || "";
    $("#wiki-editor").classList.remove("hidden");
    $("#wiki-edit-save").dataset.slug = page.slug;
  }
  function closeWikiEditor() { $("#wiki-editor").classList.add("hidden"); }

  $("#wiki-edit-cancel").addEventListener("click", closeWikiEditor);
  $("#wiki-edit-save").addEventListener("click", async (e) => {
    const slug = e.currentTarget.dataset.slug || wiki.slug;
    if (!slug) return;
    const body = {
      title: $("#wiki-edit-title").value.trim(),
      body_md: $("#wiki-edit-body").value,
      tags: $("#wiki-edit-tags").value.split(",").map((s) => s.trim()).filter(Boolean),
    };
    try {
      const res = await api(`/wiki/${encodeURIComponent(slug)}`, { method: "POST", body });
      toast(res && res.rev != null ? `Saved rev ${res.rev}` : "Saved new revision");
      closeWikiEditor();
      wiki.loaded = false;               // refresh index for new title/tags
      await renderWiki();
      await openWikiPage(slug);
    } catch (err) {
      toast("Save failed: " + err.message);
    }
  });

  $("#wiki-search").addEventListener("input", debounce(async (e) => {
    const q = e.target.value.trim();
    const seq = ++wiki.searchSeq;
    if (!q) { renderWikiSidebar(wiki.pages); return; }
    const results = await apiSafe("/wiki-search?q=" + encodeURIComponent(q));
    if (seq !== wiki.searchSeq) return;   // stale response
    const root = $("#wiki-categories");
    root.textContent = "";
    if (!results || !results.length) {
      root.append(el("div", "muted", "No matches."));
      return;
    }
    root.append(el("div", "wiki-cat-name", "Search results"));
    for (const r of results) {
      const item = el("button", "wiki-page-link", r.title || r.slug);
      if (r.snippet) item.append(el("div", "wiki-snippet", r.snippet));
      item.addEventListener("click", () => openWikiPage(r.slug));
      root.append(item);
    }
  }, 250));

  // ---------------------------------------------------------------- monitoring
  async function renderMonitoring() {
    const root = $("#monitoring-body");
    root.textContent = "";
    const status = await apiSafe("/admin/status");
    if (!status) {
      root.append(el("div", "mon-card", "Monitoring data unavailable (/admin/status not reachable)."));
    } else {
      root.append(healthCard(status.health));
      root.append(servicesCard(status.services));
      root.append(backupsCard(status.backups));
      root.append(kvCard("Maintenance", [
        ["Last run", fmtTs(status.maintenance && status.maintenance.last_run)],
        ["Summary", (status.maintenance && status.maintenance.summary) || "unknown"],
      ]));
      root.append(kvCard("Hygiene", [
        ["Result", (status.hygiene && status.hygiene.result) || "unknown"],
        ["At", fmtTs(status.hygiene && status.hygiene.ts)],
      ]));
      if (status.seats_note) {
        const seats = el("div", "mon-card mon-seats");
        seats.append(el("div", "mon-card-title", "Seats"));
        seats.append(el("div", null, String(status.seats_note)));
        root.append(seats);
      }
    }
    await renderBlockedInbox();
  }

  function healthCard(health) {
    const h = health || {};
    const card = el("div", "mon-card " + (h.ok ? "mon-ok" : "mon-fail"));
    const badge = el("span", "mon-badge " + (h.ok ? "mon-badge-ok" : "mon-badge-fail"),
      h.ok === true ? "OK" : (h.ok === false ? "FAIL" : "UNKNOWN"));
    card.append(el("div", "mon-card-title", "Hub health"), badge);
    card.append(el("div", "mon-sub", "as of " + fmtTs(h.ts)));
    if (h.last_error) card.append(el("div", "mon-error", String(h.last_error)));
    return card;
  }

  function servicesCard(services) {
    const card = el("div", "mon-card mon-card-wide");
    card.append(el("div", "mon-card-title", "Services"));
    const list = Array.isArray(services) ? services : [];
    if (!list.length) { card.append(el("div", "muted", "unknown")); return card; }
    const table = el("table", "mon-table");
    const thead = el("tr");
    ["Label", "Loaded", "PID", "State"].forEach((h) => thead.append(el("th", null, h)));
    table.append(thead);
    for (const s of list) {
      const tr = el("tr");
      tr.append(el("td", null, s.label || "unknown"));
      const loaded = s.loaded === true ? "loaded" : (s.loaded === false ? "unloaded" : "unknown");
      tr.append(el("td", loaded === "loaded" ? "mon-yes" : "mon-no", loaded));
      tr.append(el("td", null, s.pid != null ? String(s.pid) : "—"));
      tr.append(el("td", null, s.state || "unknown"));
      table.append(tr);
    }
    card.append(table);
    return card;
  }

  function backupsCard(backups) {
    const b = backups || {};
    const stale = typeof b.age_h === "number" && b.age_h > 26;
    const card = el("div", "mon-card" + (stale ? " mon-alert" : ""));
    card.append(el("div", "mon-card-title", "Backups" + (stale ? " — STALE" : "")));
    card.append(el("div", null, "Newest: " + (b.newest || "unknown")));
    card.append(el("div", null, "Age: " + (b.age_h != null ? b.age_h + "h (budget 26h)" : "unknown")));
    card.append(el("div", null, "Count: " + (b.count != null ? b.count : "unknown")));
    return card;
  }

  function kvCard(title, rows) {
    const card = el("div", "mon-card");
    card.append(el("div", "mon-card-title", title));
    for (const [k, v] of rows) {
      const row = el("div", "mon-kv");
      row.append(el("span", "muted", k));
      row.append(el("span", null, String(v)));
      card.append(row);
    }
    return card;
  }

  async function renderBlockedInbox() {
    const root = $("#blocked-inbox");
    root.textContent = "";
    let blocked = [];
    try {
      const data = await api("/board");
      const cols = data.columns || {};
      for (const s of Object.keys(cols))
        if (String(s).toLowerCase() === "blocked") blocked = cols[s] || [];
    } catch {
      root.append(el("div", "muted", "Board unavailable."));
      return;
    }
    if (!blocked.length) {
      root.append(el("div", "muted", "Nothing blocked. 🎉"));
      return;
    }
    for (const t of blocked) {
      const row = el("div", "card blocked-row");
      row.append(el("div", "card-title", t.title));
      const meta = el("div", "card-meta");
      meta.append(el("span", `status-pill st-${t.status}`, label(t.status)));
      if (t.id) meta.append(el("span", "chip", t.id));
      row.append(meta);
      row.addEventListener("click", () => openDrawer(t.id));
      root.append(row);
    }
  }

  // ---------------------------------------------------------------- admin
  const ADMIN_COMMANDS = [
    { label: "Load server", cmd: "launchctl load ~/Library/LaunchAgents/com.ticket-hub.server.plist" },
    { label: "Unload server", cmd: "launchctl unload ~/Library/LaunchAgents/com.ticket-hub.server.plist" },
    { label: "Load outbound", cmd: "launchctl load ~/Library/LaunchAgents/com.ticket-hub.outbound.plist" },
    { label: "Unload outbound", cmd: "launchctl unload ~/Library/LaunchAgents/com.ticket-hub.outbound.plist" },
    { label: "Load maintenance", cmd: "launchctl load ~/Library/LaunchAgents/com.ticket-hub.maintenance.plist" },
    { label: "Unload maintenance", cmd: "launchctl unload ~/Library/LaunchAgents/com.ticket-hub.maintenance.plist" },
    { label: "Hygiene check", cmd: "scripts/hygiene_check.sh" },
    { label: "Backup", cmd: "scripts/backup.sh" },
    { label: "Healthcheck", cmd: "scripts/healthcheck.sh" },
    { label: "Restore drill", cmd: "scripts/restore_drill.sh" },
  ];

  async function renderAdmin() {
    const root = $("#admin-body");
    root.textContent = "";
    const status = await apiSafe("/admin/status");

    const countsCard = el("div", "mon-card admin-panel");
    countsCard.append(el("div", "mon-card-title", "Database"));
    const counts = (status && status.counts) || {};
    const grid = el("div", "admin-counts");
    [["Tickets", counts.tickets], ["Edges", counts.edges],
     ["Events", counts.events], ["Wiki pages", counts.wiki_pages]].forEach(([k, v]) => {
      const box = el("div", "admin-count");
      box.append(el("div", "admin-count-num", v != null ? String(v) : "unknown"));
      box.append(el("div", "muted", k));
      grid.append(box);
    });
    countsCard.append(grid);
    countsCard.append(el("div", "mon-sub", "DB path: " +
      ((status && (status.db_path || status.counts && status.counts.db_path)) || "unknown")));
    root.append(countsCard);

    const cmdCard = el("div", "mon-card admin-panel admin-panel-wide");
    cmdCard.append(el("div", "mon-card-title", "Commands (print-only — run these yourself)"));
    for (const c of ADMIN_COMMANDS) {
      const row = el("div", "admin-cmd-row");
      row.append(el("div", "admin-cmd-label", c.label));
      const pre = el("pre", "admin-cmd");
      pre.append(el("code", null, c.cmd));
      row.append(pre);
      const copy = el("button", "btn admin-copy-btn", "Copy");
      copy.addEventListener("click", () => copyText(c.cmd).catch(() => toast("Clipboard unavailable")));
      row.append(copy);
      cmdCard.append(row);
    }
    root.append(cmdCard);

    const logCard = el("div", "mon-card admin-panel admin-panel-wide");
    logCard.append(el("div", "mon-card-title", "Log tail — server"));
    const logBody = el("pre", "admin-log");
    logCard.append(logBody);
    root.append(logCard);
    try {
      const res = await fetch("/admin/log?name=server");
      if (res.status === 404) {
        logBody.textContent = "log tailing unavailable in this build";
      } else if (!res.ok) {
        logBody.textContent = "log tail unavailable (" + res.status + ")";
      } else {
        const text = await res.text();
        let parsed = null;
        try { parsed = JSON.parse(text); } catch {}
        logBody.textContent = (parsed && (parsed.lines || parsed.log || parsed.text)) || text || "(empty)";
      }
    } catch {
      logBody.textContent = "log tailing unavailable in this build";
    }
  }

  // ---------------------------------------------------------------- roles
  const FALLBACK_ROLES = [
    { key: "design", mission: "UX/interaction quality bar for anything user-facing.",
      quality_bar: "Every user-facing surface meets the calm, Linear-class bar.",
      inputs: ["Mockups", "Live UI", "User feedback"],
      outputs: ["Design critiques", "Interaction specs"],
      seat: "Any strong visual-reasoning seat" },
    { key: "pmf", mission: "Product-market-fit challenge: who wants this, why now.",
      quality_bar: "Answers the 'what would make the owner stop using it' question.",
      inputs: ["Feature proposals", "Usage signals"],
      outputs: ["PMF verdicts", "Kill/scale recommendations"],
      seat: "Any strong reasoning seat" },
    { key: "code-design", mission: "Architecture review: contracts, edges, failure modes.",
      quality_bar: "Distinct from execution; reviews interfaces, not diffs.",
      inputs: ["Design docs", "API contracts"],
      outputs: ["Architecture reviews", "Contract revisions"],
      seat: "GLM-class reviewer" },
    { key: "execution", mission: "Builders; the swarm's bulk. Pull assigned tickets.",
      quality_bar: "Work in sandbox lanes; report with evidence.",
      inputs: ["Assigned tickets", "Packets"],
      outputs: ["Code in lanes", "Evidence-backed reports"],
      seat: "Assigned remote seat" },
    { key: "measurement", mission: "Defines and tracks the metrics that prove a feature works.",
      quality_bar: "Kills vanity claims; every claim has a number.",
      inputs: ["Test counts", "Latency data", "Adoption signals"],
      outputs: ["Metric definitions", "Measurement reports"],
      seat: "Any seat; deterministic tools preferred" },
    { key: "product-owner", mission: "Planner/reviewer: sequences scope, accepts or returns work per ticket.",
      quality_bar: "Reviewer ≠ author; acceptance is the owner's alone.",
      inputs: ["Ticket queue", "Execution reports"],
      outputs: ["Sequenced scope", "Accept/return decisions"],
      seat: "GLM seat (default engine)" },
    { key: "orchestrator", mission: "Coordinates tickets, spawns lanes, keeps the ticket as the shared packet.",
      quality_bar: "Never reviews its own output.",
      inputs: ["Full ticket graph", "Seat telemetry"],
      outputs: ["Lane assignments", "Merged integrations"],
      seat: "Hermes" },
  ];

  async function renderRoles() {
    const root = $("#roles-body");
    root.textContent = "";
    let roles = null;
    try {
      const res = await fetch("/roles");
      if (res.ok) {
        const data = await res.json();
        roles = Array.isArray(data) ? data : (data && Array.isArray(data.roles) ? data.roles : null);
      }
    } catch {}
    if (!roles || !roles.length) roles = FALLBACK_ROLES;
    for (const r of roles) {
      root.append(roleCard(r));
    }
    // seat roster: health / load / open assignments
    try {
      const roster = await api("/seats");
      const sec = el("div", "roles-section");
      sec.append(el("h2", null, "Seat roster"));
      const table = el("table", "seats-table");
      const thead = el("thead");
      const hrow = el("tr");
      ["Seat", "Health", "Load", "Licensed roles"].forEach((h) => hrow.append(el("th", null, h)));
      thead.append(hrow);
      table.append(thead);
      const tb = el("tbody");
      for (const [name, s] of Object.entries(roster.seats || {}).sort()) {
        const tr = el("tr");
        tr.append(el("td", null, name));
        const healthCell = el("td");
        healthCell.append(el("span", "status-pill st-" + (s.health === "up" ? "done" : "blocked"), s.health));
        tr.append(healthCell, el("td", null, String(s.load)));
        tr.append(el("td", "muted", (s.roles || []).join(", ")));
        tb.append(tr);
      }
      table.append(tb);
      sec.append(table);
      const asn = roster.assignments || [];
      if (asn.length) {
        sec.append(el("h2", null, `Open assignments (${asn.length})`));
        for (const a of asn) {
          const row = el("div", "plan-item");
          const link = el("a", null, a.title);
          link.addEventListener("click", () => openDrawer(a.ticket_id));
          row.append(el("span", "chip chip-family", a.seat + "@" + a.role), link,
                     el("span", "muted", " " + a.status));
          sec.append(row);
        }
      }
      root.append(sec);
    } catch {}
  }

  function roleCard(r) {
    const card = el("div", "role-card");
    card.append(el("h3", "role-name", label(r.key || r.name || "role")));
    const field = (name, val) => {
      if (val == null) return;
      const sec = el("div", "role-field");
      sec.append(el("div", "role-field-name", name));
      if (Array.isArray(val)) {
        const ul = el("ul", "role-list");
        val.forEach((v) => ul.append(el("li", null, String(v))));
        sec.append(ul);
      } else {
        sec.append(el("div", "role-field-val", String(val)));
      }
      card.append(sec);
    };
    field("Mission", r.mission);
    field("Quality bar", r.quality_bar || r.qualityBar);
    field("Inputs", r.inputs);
    field("Outputs", r.outputs);
    field("Seat preference", r.seat || r.seat_preference || r.seatPreference);
    return card;
  }


  // ---------------------------------------------------------------- new ticket
  $("#new-ticket-btn").addEventListener("click", () => $("#modal").classList.remove("hidden"));
  $("#modal-cancel").addEventListener("click", () => $("#modal").classList.add("hidden"));
  $("#modal").addEventListener("click", (e) => {
    if (e.target === $("#modal")) $("#modal").classList.add("hidden");
  });

  $("#new-ticket-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const ownerName = f.get("owner")?.trim();
    const family = f.get("family")?.trim() || ownerName || "unknown";
    const body = {
      title: f.get("title").trim(),
      body: (f.get("body") || "").trim(),
      priority: f.get("priority"),
      owner: ownerName ? { name: ownerName, family, kind: "agent" } : null,
      parent: f.get("parent")?.trim() || null,
    };
    try {
      const t = await api("/tickets", { method: "POST", body });
      $("#modal").classList.add("hidden");
      e.target.reset();
      toast(`Created ${t.id}`);
      await refresh();
      openDrawer(t.id);
    } catch (err) {
      toast("Error: " + err.message);
    }
  });

  // ---------------------------------------------------------------- boot
  async function refresh() {
    try {
      if (state.view === "board") { await loadBoard(); renderBoard(); }
      else if (state.view === "graph") { await renderGraph(); }
      else if (state.view === "updates") { await renderUpdates(); }
      else if (state.view === "wiki") { await renderWiki(); }
      else if (state.view === "monitoring") { await renderMonitoring(); }
      else if (state.view === "admin") { await renderAdmin(); }
      else if (state.view === "roles") { await renderRoles(); }
    } catch (err) {
      toast("Load failed: " + err.message);
    }
  }

  refresh();
  setInterval(() => { if (!document.hidden && state.view === "board") refresh(); }, 15000);
  setInterval(() => { if (!document.hidden && state.view === "monitoring") refresh(); }, 30000);
})();
