/* BotPort hub dashboard: vanilla JS, no build step.
 *
 * Network: a live map of the hub, its Flight Decks and their agents. Each
 * request is drawn as it travels (asker -> hub -> agent; progress and the
 * answer come back the same way). Requests: everything recent, searchable.
 * Swarm: swarm.js.
 *
 * Data: GET /api/overview (polled) and /ws/dashboard (live events). The
 * helpers $, esc, truncate, shortId and timeAgo are shared with swarm.js.
 */
"use strict";

function $(sel, root) {
  return (root || document).querySelector(sel);
}

function esc(s) {
  if (s === null || s === undefined || s === "") return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function truncate(s, max) {
  if (!s) return "";
  s = String(s);
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

function shortId(id) {
  if (!id) return "—";
  return id.length > 12 ? id.slice(0, 8) + "…" : id;
}

function timeAgo(iso) {
  if (!iso) return "";
  const diff = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 5) return "just now";
  if (diff < 60) return Math.floor(diff) + "s ago";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  return Math.floor(diff / 86400) + "d ago";
}

(function () {
  const POLL_MS = { network: 4000, requests: 6000 };
  const SVGNS = "http://www.w3.org/2000/svg";
  const REDUCED = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  const TERMINAL = new Set(["closed", "failed", "timeout"]);
  const STATUS = {
    pending: { label: "Queued", tone: "work" },
    assigned: { label: "Routed", tone: "work" },
    in_progress: { label: "Working", tone: "work" },
    responded: { label: "Answered", tone: "done" },
    closed: { label: "Done", tone: "done" },
    failed: { label: "Failed", tone: "fail" },
    timeout: { label: "Timed out", tone: "warn" },
  };
  const svgIcon = (d, extra) =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"${extra || ""}>${d}</svg>`;
  const ICON = {
    arrow: svgIcon('<path d="M5 12h14M13 6l6 6-6 6"/>'),
    check: svgIcon('<path d="M5 12.5l4.5 4.5L19 7.5"/>'),
    cross: svgIcon('<path d="M6 6l12 12M18 6L6 18"/>'),
    clock: svgIcon('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
    dash: svgIcon('<path d="M6 12h12"/>'),
    inbox: svgIcon('<path d="M4 13h4l2 3h4l2-3h4"/><path d="M5.5 5h13L20 13v6H4v-6z"/>'),
    radar: svgIcon('<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.5"/><path d="M12 12l6-6"/>'),
  };
  const RESULT_ICON = { done: ICON.check, fail: ICON.cross, warn: ICON.clock, idle: ICON.dash, work: ICON.clock };

  const S = {
    route: null,
    ov: null, // last /api/overview
    ovAt: 0,
    skew: 0, // server clock - local clock (ms)
    byId: new Map(),
    nodes: new Map(), // instance id -> { el, sig }
    edges: new Map(), // instance id -> { base, flow }
    geo: new Map(), // instance id -> { x, y, ctrl }
    hub: null,
    list: false,
    particles: [],
    raf: 0,
    seenCreate: new Set(),
    seenDone: new Set(),
    seenTick: new Set(),
    lastSpark: new Map(),
    ws: null,
    wsOpen: false,
    wsDelay: 1000,
    wsTimer: 0,
    httpOk: true,
    pollTimer: 0,
    soonTimer: 0,
    drawer: null,
    drawerSeq: 0,
    lastFocus: null,
    closeTimer: 0,
    kiosk: false,
    idleTimer: 0,
    filter: "all",
    deck: "",
    q: "",
    signinTried: false,
  };
  let el = {};

  /* ---- Small helpers ---- */

  function ms(iso) {
    const t = Date.parse(iso || "");
    return isNaN(t) ? 0 : t;
  }

  function now() {
    return Date.now() + S.skew;
  }

  function fmtDur(v) {
    if (!(v >= 0)) return "—";
    const s = Math.round(v / 1000);
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60);
    if (m < 60) return m + "m " + String(s % 60).padStart(2, "0") + "s";
    const h = Math.floor(m / 60);
    if (h < 48) return h + "h " + String(m % 60).padStart(2, "0") + "m";
    return Math.floor(h / 24) + "d " + (h % 24) + "h";
  }

  function clockTime(iso) {
    const d = iso ? new Date(iso) : new Date();
    if (isNaN(d.getTime())) return "";
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }

  function dateTime(iso) {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }

  function plural(n, word) {
    return `${n} ${word}${n === 1 ? "" : "s"}`;
  }

  function status(c) {
    const st = STATUS[c.status] || { label: c.status || "?", tone: "idle" };
    if (c.status === "closed" && ended(c.end_reason, c.answered)) return { label: "Ended", tone: "idle" };
    return st;
  }

  // Closed without an answer (the asker gave up, or went away): not "Done".
  function ended(reason, answered) {
    return !!reason && reason !== "completed" && !answered;
  }

  function isLive(c) {
    return !TERMINAL.has(c.status);
  }

  function took(c) {
    return (isLive(c) ? now() : ms(c.updated_at)) - ms(c.created_at);
  }

  function deckOf(id) {
    return id && id.startsWith("deck:") ? id.slice(5) : "";
  }

  function instName(id) {
    if (!id) return "";
    const i = S.byId.get(id);
    return i ? i.name : deckOf(id) || shortId(id);
  }

  // A concern's asker and target as { agent, place } ("Jev" @ "glasses").
  function fromParts(c) {
    return { agent: c.from_agent || "", place: c.from_name || instName(c.from) || "?" };
  }

  function toParts(c) {
    const n = c.to_name || "";
    const at = n.lastIndexOf("@");
    if (at > 0) return { agent: n.slice(0, at), place: n.slice(at + 1) };
    if (c.target_deck) return { agent: c.target_agent || "", place: c.target_deck };
    return { agent: "", place: n || (c.to ? instName(c.to) : "unassigned") };
  }

  function plain(p) {
    return p.agent ? `${p.agent}@${p.place}` : p.place;
  }

  function who(p) {
    return p.agent
      ? `<span class="who">${esc(p.agent)}<i>@${esc(p.place)}</i></span>`
      : `<span class="who">${esc(p.place)}</span>`;
  }

  function routeHTML(c) {
    return `<span class="route">${who(fromParts(c))}${ICON.arrow}${who(toParts(c))}</span>`;
  }

  function emptyHTML(icon, title, text) {
    return `<div class="empty">${icon}<b>${esc(title)}</b>${text ? `<span>${esc(text)}</span>` : ""}</div>`;
  }

  function svgEl(tag, attrs) {
    const node = document.createElementNS(SVGNS, tag);
    for (const k in attrs) node.setAttribute(k, attrs[k]);
    return node;
  }

  function fromHTML(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }

  /* Keyed list update: keeps each item's element (so animations don't
   * restart), rewrites its contents only when its signature changes. */
  function syncList(box, items, keyOf, sigOf, render, emptyMarkup) {
    if (!items.length) {
      if (box.dataset.empty !== emptyMarkup) {
        box.innerHTML = emptyMarkup;
        box.dataset.empty = emptyMarkup;
      }
      return;
    }
    if (box.dataset.empty !== undefined) {
      box.innerHTML = "";
      delete box.dataset.empty;
    }
    const have = new Map();
    for (const child of Array.from(box.children)) {
      if (child.dataset.key) have.set(child.dataset.key, child);
      else child.remove();
    }
    const animate = box.dataset.ready === "1";
    let prev = null;
    for (const it of items) {
      const key = keyOf(it);
      const sig = sigOf(it);
      let node = have.get(key);
      if (node) {
        have.delete(key);
        if (node.dataset.sig !== sig) {
          const fresh = fromHTML(render(it));
          node.innerHTML = fresh.innerHTML;
          node.className = fresh.className;
          node.dataset.sig = sig;
        }
      } else {
        node = fromHTML(render(it));
        node.dataset.key = key;
        node.dataset.sig = sig;
        if (animate && !REDUCED) {
          node.classList.add("fresh");
          node.addEventListener("animationend", () => node.classList.remove("fresh"), { once: true });
        }
      }
      const next = prev ? prev.nextElementSibling : box.firstElementChild;
      if (node !== next) box.insertBefore(node, next);
      prev = node;
    }
    for (const gone of have.values()) gone.remove();
    box.dataset.ready = "1";
  }

  /* ---- Network ---- */

  async function getJSON(url) {
    let res;
    try {
      res = await fetch(url, { cache: "no-store" });
    } catch (e) {
      setHttp(false);
      return null;
    }
    setHttp(true);
    if (!res.ok) return null;
    try {
      return await res.json();
    } catch (e) {
      return null;
    }
  }

  function setHttp(ok) {
    if (S.httpOk !== ok) {
      S.httpOk = ok;
      updateLive();
    }
  }

  function updateLive() {
    const state = !S.httpOk ? "down" : S.wsOpen ? "live" : "polling";
    const text = { live: "Live", polling: "Polling", down: "Hub unreachable" }[state];
    const title = {
      live: "Connected: events arrive as they happen",
      polling: "Live events are reconnecting; refreshing every few seconds",
      down: "The hub does not answer; retrying",
    }[state];
    el.live.dataset.state = state;
    el.live.title = title;
    $(".live-label", el.live).textContent = text;
  }

  function connectWS() {
    clearTimeout(S.wsTimer);
    let ws;
    try {
      ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/dashboard`);
    } catch (e) {
      retryWS();
      return;
    }
    S.ws = ws;
    ws.onopen = () => {
      S.wsOpen = true;
      S.wsDelay = 1000;
      updateLive();
      soon();
    };
    ws.onmessage = (e) => {
      let ev;
      try {
        ev = JSON.parse(e.data);
      } catch (err) {
        return;
      }
      onEvent(ev);
    };
    ws.onclose = () => {
      if (S.ws !== ws) return;
      S.ws = null;
      S.wsOpen = false;
      updateLive();
      retryWS();
    };
  }

  function retryWS() {
    clearTimeout(S.wsTimer);
    S.wsTimer = setTimeout(connectWS, S.wsDelay);
    S.wsDelay = Math.min(S.wsDelay * 2, 15000);
  }

  function reconnectWS() {
    const old = S.ws;
    S.ws = null;
    S.wsOpen = false;
    if (old) {
      try {
        old.close();
      } catch (e) {
        /* already closed */
      }
    }
    S.wsDelay = 1000;
    connectWS();
  }

  /* ---- Refresh ---- */

  let refreshing = false;
  let refreshAgain = false;

  async function refresh() {
    if (S.route === "swarm") return;
    if (refreshing) {
      refreshAgain = true;
      return;
    }
    refreshing = true;
    try {
      const ov = await getJSON(`/api/overview?limit=${S.route === "requests" ? 200 : 40}`);
      if (ov && ov.hub) apply(ov);
    } finally {
      refreshing = false;
      if (refreshAgain) {
        refreshAgain = false;
        refresh();
      }
    }
  }

  function soon() {
    clearTimeout(S.soonTimer);
    S.soonTimer = setTimeout(refresh, 250);
  }

  function startPolling() {
    clearInterval(S.pollTimer);
    S.pollTimer = setInterval(refresh, POLL_MS[S.route] || 5000);
    refresh();
  }

  function stopPolling() {
    clearInterval(S.pollTimer);
    S.pollTimer = 0;
  }

  function apply(ov) {
    const prev = S.ov;
    S.ov = ov;
    S.ovAt = Date.now();
    S.skew = ms(ov.now) ? ms(ov.now) - Date.now() : 0;
    S.byId = new Map(ov.instances.map((i) => [i.id, i]));
    el.signout.hidden = !(window.BotPortAuth && BotPortAuth.has());
    el.version.textContent = "v" + ov.hub.version;
    noticeChanges(prev, ov);
    if (S.route === "network") {
      renderKpis(ov);
      renderMap(ov);
      renderRail(ov);
    } else if (S.route === "requests") {
      renderRequests();
    }
    if (S.drawer) refreshDrawer();
    tick1s();
  }

  /* ---- Live events ---- */

  function onEvent(ev) {
    if (!ev || !ev.type) return;
    switch (ev.type) {
      case "concern_created":
        if (!S.seenCreate.has(ev.concern_id)) {
          S.seenCreate.add(ev.concern_id);
          animateRequest(ev);
          addTick("request", ev);
        }
        soon();
        break;
      case "concern_progress":
        if (ev.text) updateLiveText(ev.concern_id, ev.text);
        spark(ev);
        break;
      case "concern_result":
        if (!S.seenDone.has(ev.concern_id)) {
          S.seenDone.add(ev.concern_id);
          const kind = resultKind(ev.status, ev.reason, ev.answered);
          animateResult(ev, kind);
          addTick(kind, ev);
        }
        soon();
        break;
      case "instance_connected":
        if (ev.instance) addTick("online", { name: ev.instance.name, key: "on:" + ev.instance.id + ":" + ev.instance.connected_at });
        soon();
        break;
      case "instance_disconnected":
        addTick("offline", { name: instName(ev.instance_id), key: "off:" + ev.instance_id + ":" + Date.now() });
        soon();
        break;
      default:
        break;
    }
    trimSets();
  }

  function resultKind(st, reason, answered) {
    if (st === "failed") return "fail";
    if (st === "timeout") return "warn";
    if (st === "closed" && ended(reason, answered)) return "ended";
    return "answer";
  }

  // Without live events (or after a reconnect), animate what changed between polls.
  function noticeChanges(prev, ov) {
    if (!prev) {
      for (const c of ov.active) S.seenCreate.add(c.id);
      for (const c of ov.recent) S.seenDone.add(c.id);
      seedTicks(ov);
      return;
    }
    for (const c of ov.active) {
      if (!S.seenCreate.has(c.id)) {
        S.seenCreate.add(c.id);
        animateRequest(c);
        addTick("request", c);
      }
    }
    const still = new Set(ov.active.map((c) => c.id));
    for (const c of prev.active) {
      if (still.has(c.id) || S.seenDone.has(c.id)) continue;
      const done = ov.recent.find((x) => x.id === c.id);
      if (!done) continue;
      S.seenDone.add(c.id);
      const kind = resultKind(done.status, done.end_reason, done.answered);
      animateResult(done, kind);
      addTick(kind, done);
    }
    trimSets();
  }

  function trimSets() {
    for (const set of [S.seenCreate, S.seenDone, S.seenTick]) {
      if (set.size > 3000) {
        const keep = Array.from(set).slice(-1500);
        set.clear();
        keep.forEach((k) => set.add(k));
      }
    }
  }

  function updateLiveText(cid, text) {
    if (S.ov) {
      const c = S.ov.active.find((x) => x.id === cid);
      if (c) c.live = { text };
    }
    document.querySelectorAll(`[data-key="${CSS.escape(cid)}"] .live-text`).forEach((n) => {
      n.textContent = text;
    });
    if (S.drawer && S.drawer.kind === "concern" && S.drawer.id === cid) {
      const d = $(".d-live-text", el.drawer);
      if (d) d.textContent = text;
    }
  }

  /* ---- Ticker ---- */

  function addTick(kind, x, historic) {
    const key = x.key || `${kind}:${x.concern_id || x.id || ""}`;
    if (S.seenTick.has(key)) return;
    S.seenTick.add(key);
    const f = esc(plain(fromParts(x)));
    const t = esc(plain(toParts(x)));
    const text = {
      request: `<b>${f}</b> asked <b>${t}</b>`,
      answer: `<b>${t}</b> answered <b>${f}</b>`,
      fail: `<b>${t}</b> failed for <b>${f}</b>`,
      warn: `<b>${f}</b> → <b>${t}</b> timed out`,
      ended: `<b>${f}</b> → <b>${t}</b> ended`,
      online: `<b>${esc(x.name)}</b> joined the network`,
      offline: `<b>${esc(x.name)}</b> went offline`,
    }[kind];
    const cid = x.concern_id || (x.from !== undefined ? x.id : "");
    const node = document.createElement("span");
    node.className = `tick${historic || REDUCED ? "" : " fresh"}`;
    if (cid) {
      node.dataset.cid = cid;
      node.setAttribute("role", "button");
      node.tabIndex = 0;
    }
    const when = historic ? clockTime(x.updated_at || x.created_at) : clockTime();
    node.innerHTML = `<i class="k-${kind}"></i><time>${esc(when)}</time><span>${text}</span>`;
    if (historic) el.ticks.appendChild(node);
    else el.ticks.prepend(node);
    while (el.ticks.children.length > 30) el.ticks.lastElementChild.remove();
  }

  function seedTicks(ov) {
    for (const c of ov.active.slice().sort((a, b) => ms(b.created_at) - ms(a.created_at))) addTick("request", c, true);
    for (const c of ov.recent.slice(0, 12)) addTick(resultKind(c.status, c.end_reason, c.answered), c, true);
  }

  /* ---- KPIs ---- */

  function renderKpis(ov) {
    const decks = ov.instances.filter((i) => i.kind === "deck");
    const online = decks.filter((d) => d.status === "connected");
    const links = ov.instances.filter((i) => i.kind !== "deck" && i.status === "connected").length;
    const agents = online.reduce((n, d) => n + (d.agents || []).length, 0);
    const running = online.reduce((n, d) => n + (d.agents || []).filter((a) => a.status === "running").length, 0);
    const st = ov.stats || {};
    const by = st.by_status || {};
    const failed = (by.failed || 0) + (by.timeout || 0);
    const answers = ov.recent
      .filter((c) => c.status === "closed" && !ended(c.end_reason, c.answered))
      .map((c) => ms(c.updated_at) - ms(c.created_at))
      .filter((v) => v >= 0)
      .sort((a, b) => a - b);
    const median = answers.length ? answers[Math.floor(answers.length / 2)] : null;
    const queued = ov.hub.queued_results || 0;

    setKpi("decks", online.length, `of ${decks.length} known${links ? ` · ${plural(links, "agent link")}` : ""}`);
    setKpi("agents", agents, agents ? `${running} running` : "none shared yet");
    setKpi("inflight", ov.active.length, queued ? `${plural(queued, "answer")} waiting for delivery` : "right now");
    setKpi("today", st.last_24h || 0, `${st.total || 0} all time`);
    setKpi("success", Math.round(st.success_rate != null ? st.success_rate : 100), failed ? `${failed} failed or timed out` : "no failures", "%");
    setKpi("median", median != null ? fmtDur(median) : "—", answers.length ? `over the last ${plural(answers.length, "answer")}` : "no answers yet");
  }

  function setKpi(key, value, sub, unit) {
    const box = el.kpis[key];
    if (!box) return;
    const v = $(".kpi-value", box);
    if (typeof value === "number") countTo(v, value, unit || "");
    else if (v.textContent !== value) v.textContent = value;
    $(".kpi-sub", box).textContent = sub;
  }

  function countTo(node, to, unit) {
    const html = (n) => `${n}${unit ? `<span class="unit">${unit}</span>` : ""}`;
    const from = node.dataset.v === undefined ? to : Number(node.dataset.v);
    node.dataset.v = String(to);
    if (from === to || REDUCED) {
      node.innerHTML = html(to);
      return;
    }
    const t0 = performance.now();
    const step = (t) => {
      if (node.dataset.v !== String(to)) return;
      const k = Math.min(1, (t - t0) / 700);
      node.innerHTML = html(Math.round(from + (to - from) * (1 - Math.pow(1 - k, 3))));
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  /* ---- Map ---- */

  function renderMap(ov) {
    const map = el.map;
    const w = map.clientWidth;
    const h = map.clientHeight;
    if (!w) return;
    const list = w < 640;
    if (list !== S.list) {
      S.list = list;
      map.classList.toggle("list", list);
      clearParticles();
    }

    const insts = ov.instances
      .slice()
      .sort((a, b) => (a.kind === "deck" ? 0 : 1) - (b.kind === "deck" ? 0 : 1) || a.name.localeCompare(b.name));
    const n = insts.length;
    const density = n > 10 ? "dense" : n > 6 ? "compact" : "";
    const decks = insts.filter((i) => i.kind === "deck");
    const online = decks.filter((d) => d.status === "connected").length;
    const shared = decks.reduce((k, d) => k + (d.status === "connected" ? (d.agents || []).length : 0), 0);
    el.mapSub.textContent = n
      ? `${online} of ${plural(decks.length, "deck")} online · ${plural(shared, "agent")} shared`
      : "No Flight Decks yet";
    el.hubSub.textContent = `${plural(online, "deck")} · ${ov.active.length} in flight`;
    el.empty.hidden = n > 0;

    // Who is working for whom right now.
    const busyAgents = new Map();
    const busy = new Set();
    const asking = new Set();
    for (const c of ov.active) {
      if (c.to) {
        busy.add(c.to);
        const a = toParts(c).agent.toLowerCase();
        if (!busyAgents.has(c.to)) busyAgents.set(c.to, new Set());
        if (a) busyAgents.get(c.to).add(a);
      }
      if (c.from) asking.add(c.from);
    }

    const cx = w / 2;
    const cy = h / 2;
    S.hub = { x: cx, y: cy };
    el.hub.style.setProperty("--x", cx.toFixed(1) + "px");
    el.hub.style.setProperty("--y", cy.toFixed(1) + "px");

    const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
    const nodeW = (density === "dense" ? 9 : density === "compact" ? 11 : 14.5) * rem;
    const nodeH = (density === "dense" ? 3.4 : density === "compact" ? 5 : 8.4) * rem;
    const rx = Math.max(nodeW * 0.95, w / 2 - nodeW / 2 - 1.2 * rem);
    const ry = Math.max(nodeH * 1.05, h / 2 - nodeH / 2 - 2.4 * rem);
    // Even counts are turned half a step so two decks sit left and right and
    // four sit on the diagonals, clear of the hub's label.
    const start = n === 1 ? Math.PI : -Math.PI / 2 + (n % 2 === 0 ? Math.PI / n : 0);

    if (!el.orbit) {
      el.orbit = svgEl("ellipse", { class: "orbit" });
      el.edges.before(el.orbit);
    }
    el.orbit.setAttribute("cx", cx.toFixed(1));
    el.orbit.setAttribute("cy", cy.toFixed(1));
    el.orbit.setAttribute("rx", (n ? rx : 0).toFixed(1));
    el.orbit.setAttribute("ry", (n ? ry : 0).toFixed(1));

    const seen = new Set();
    insts.forEach((inst, k) => {
      seen.add(inst.id);
      let node = S.nodes.get(inst.id);
      if (!node) {
        const div = document.createElement("div");
        div.className = "node enter";
        el.nodes.appendChild(div);
        node = { el: div, sig: "" };
        S.nodes.set(inst.id, node);
        requestAnimationFrame(() => requestAnimationFrame(() => div.classList.remove("enter")));
      }
      const a = start + (k * 2 * Math.PI) / Math.max(n, 1);
      const x = cx + rx * Math.cos(a);
      const y = cy + ry * Math.sin(a);
      node.el.style.setProperty("--x", x.toFixed(1) + "px");
      node.el.style.setProperty("--y", y.toFixed(1) + "px");
      node.el.style.order = String(k);

      const on = inst.status === "connected";
      const max = Number(inst.max_concurrent) || 0;
      const act = Number(inst.active_concerns) || 0;
      const cls = node.el.classList;
      cls.toggle("deck", inst.kind === "deck");
      cls.toggle("agent", inst.kind !== "deck");
      cls.toggle("off", !on);
      cls.toggle("busy", on && busy.has(inst.id));
      cls.toggle("asking", on && asking.has(inst.id) && !busy.has(inst.id));
      cls.toggle("hot", max > 0 && act / max >= 0.8);
      cls.toggle("compact", density === "compact");
      cls.toggle("dense", density === "dense");

      const ctx = busyAgents.get(inst.id) || new Set();
      const sig = JSON.stringify([inst.name, inst.status, inst.version, act, max, inst.agents, inst.personas,
        (inst.tools || []).length, Array.from(ctx), on ? "" : timeAgo(inst.disconnected_at)]);
      if (sig !== node.sig) {
        node.sig = sig;
        node.el.innerHTML = nodeHTML(inst, ctx);
      }

      // The edge bends a little, alternately, so lines don't look ruled.
      const dx = x - cx;
      const dy = y - cy;
      const len = Math.hypot(dx, dy) || 1;
      const bend = 0.1 * len * (k % 2 ? 1 : -1);
      const ctrl = { x: (cx + x) / 2 - (dy / len) * bend, y: (cy + y) / 2 + (dx / len) * bend };
      S.geo.set(inst.id, { x, y, ctrl });
      let edge = S.edges.get(inst.id);
      if (!edge) {
        edge = { base: svgEl("path", { class: "edge" }), flow: svgEl("path", { class: "flow" }) };
        el.edges.appendChild(edge.base);
        el.edges.appendChild(edge.flow);
        S.edges.set(inst.id, edge);
      }
      const d = `M${cx.toFixed(1)},${cy.toFixed(1)} Q${ctrl.x.toFixed(1)},${ctrl.y.toFixed(1)} ${x.toFixed(1)},${y.toFixed(1)}`;
      if (edge.base.getAttribute("d") !== d) {
        edge.base.setAttribute("d", d);
        edge.flow.setAttribute("d", d);
      }
      edge.base.classList.toggle("off", !on);
      edge.flow.classList.toggle("on", on && (busy.has(inst.id) || asking.has(inst.id)));
      edge.flow.classList.toggle("up", on && asking.has(inst.id) && !busy.has(inst.id));
    });

    for (const [id, node] of Array.from(S.nodes)) {
      if (seen.has(id)) continue;
      S.nodes.delete(id);
      S.geo.delete(id);
      node.el.classList.add("leave");
      setTimeout(() => node.el.remove(), 500);
      const edge = S.edges.get(id);
      if (edge) {
        edge.base.remove();
        edge.flow.remove();
        S.edges.delete(id);
      }
    }
  }

  function nodeHTML(inst, busyAgents) {
    const deck = inst.kind === "deck";
    const on = inst.status === "connected";
    const agents = deck
      ? inst.agents || []
      : (inst.personas || []).map((p) => ({ name: p.name, description: p.description, status: "running" }));
    const isBusy = (a) => busyAgents.has(String(a.name).toLowerCase()) || busyAgents.has(String(a.slug || "").toLowerCase());
    const sorted = agents.slice().sort((a, b) => {
      const ba = isBusy(a) ? 0 : 1;
      const bb = isBusy(b) ? 0 : 1;
      if (ba !== bb) return ba - bb;
      const ra = a.status === "running" ? 0 : 1;
      const rb = b.status === "running" ? 0 : 1;
      return ra - rb || String(a.name).localeCompare(String(b.name));
    });
    const MAX = 5;
    let chips = sorted
      .slice(0, MAX)
      .map((a) => {
        const cls = isBusy(a) ? " busy" : a.status && a.status !== "running" ? " off" : "";
        const tip = a.status && a.status !== "running" ? `${a.name} (${a.status})` : a.description || a.name;
        return `<span class="chip${cls}" title="${esc(tip)}">${esc(a.name)}</span>`;
      })
      .join("");
    if (sorted.length > MAX) chips += `<span class="chip more">+${sorted.length - MAX}</span>`;
    if (!chips) chips = `<span class="chip more">${deck ? "no agents shared" : "no personas"}</span>`;
    const max = Number(inst.max_concurrent) || 0;
    const act = Number(inst.active_concerns) || 0;
    const pct = max ? Math.min(100, Math.round((act / max) * 100)) : 0;
    const sub = !on
      ? `offline · ${esc(timeAgo(inst.disconnected_at)) || "just now"}`
      : deck
        ? `Flight Deck${inst.version ? " " + esc(inst.version) : ""} · ${plural(agents.length, "agent")}`
        : `Captain Claw agent · ${plural((inst.tools || []).length, "tool")}`;
    return `<button class="node-card" type="button" data-deck="${esc(inst.id)}" aria-label="${esc(inst.name)}: details">
      <span class="node-head"><span class="node-dot"></span><span class="node-name">${esc(inst.name)}</span><span class="node-load" title="Requests running / capacity">${act}/${max}</span></span>
      <span class="node-sub">${sub}</span>
      <span class="chips">${chips}</span>
      <span class="load"><i style="width:${pct}%"></i></span>
    </button>`;
  }

  /* ---- Travelling requests ---- */

  function animateRequest(c) {
    comet(c.from, c.to, "request", () => ping(c.to, "request"), hubBeat);
  }

  function animateResult(c, kind) {
    const ping2 = () => ping(c.from, kind === "answer" ? "answer" : kind === "fail" ? "fail" : "warn");
    if (kind === "answer") comet(c.to, c.from, "answer", ping2, hubBeat);
    else comet(null, c.from, kind, ping2);
  }

  function spark(ev) {
    const last = S.lastSpark.get(ev.concern_id) || 0;
    const t = performance.now();
    if (t - last < 1400) return;
    S.lastSpark.set(ev.concern_id, t);
    if (S.lastSpark.size > 200) S.lastSpark.clear();
    comet(ev.to, ev.from, "progress");
  }

  function canAnimate() {
    return S.route === "network" && !S.list && !REDUCED && !document.hidden && S.hub;
  }

  function comet(src, dst, kind, onEnd, onHub) {
    if (!canAnimate()) {
      if (onEnd) onEnd();
      return;
    }
    const a = src ? S.geo.get(src) : null;
    const b = dst ? S.geo.get(dst) : null;
    const segs = [];
    if (a) segs.push([a, a.ctrl, S.hub]);
    if (b) segs.push([S.hub, b.ctrl, b]);
    if (!segs.length) {
      if (onEnd) onEnd();
      return;
    }
    const lens = segs.map(([p0, p1, p2]) => (dist(p0, p2) + dist(p0, p1) + dist(p1, p2)) / 2);
    const total = lens.reduce((x, y) => x + y, 0) || 1;
    const dur = Math.max(700, Math.min(2600, total / (kind === "progress" ? 0.3 : 0.4)));
    const r = { request: 8, answer: 9, progress: 5 }[kind] || 7.5;
    const g = svgEl("g", { class: "pt pt-" + kind });
    g.appendChild(svgEl("circle", { r: (r * 2.3).toFixed(1), class: "pt-glow" }));
    g.appendChild(svgEl("circle", { r: (r * 0.36).toFixed(2), class: "pt-core" }));
    const trail = kind === "progress" ? 2 : 5;
    for (let i = 1; i <= trail; i++) {
      const f = 1 - i / (trail + 1);
      g.appendChild(svgEl("circle", { r: Math.max(0.6, r * 0.34 * f).toFixed(2), class: "pt-trail", opacity: (0.8 * f).toFixed(2) }));
    }
    el.parts.appendChild(g);
    S.particles.push({ segs, lens, total, dur, start: performance.now(), g, onEnd, onHub, hubAt: a && b ? lens[0] / total : -1 });
    while (S.particles.length > 48) {
      const p = S.particles.shift();
      p.g.remove();
    }
    if (!S.raf) S.raf = requestAnimationFrame(frame);
  }

  function dist(p, q) {
    return Math.hypot(p.x - q.x, p.y - q.y);
  }

  function pointAt(p, u) {
    let d = u * p.total;
    for (let i = 0; i < p.segs.length; i++) {
      if (d <= p.lens[i] || i === p.segs.length - 1) {
        const t = p.lens[i] ? Math.max(0, Math.min(1, d / p.lens[i])) : 1;
        const [p0, p1, p2] = p.segs[i];
        const v = 1 - t;
        return { x: v * v * p0.x + 2 * v * t * p1.x + t * t * p2.x, y: v * v * p0.y + 2 * v * t * p1.y + t * t * p2.y };
      }
      d -= p.lens[i];
    }
    return p.segs[0][0];
  }

  function frame(t) {
    S.raf = 0;
    const done = [];
    for (const p of S.particles) {
      const raw = Math.min(1, Math.max(0, (t - p.start) / p.dur));
      const u = raw < 0.5 ? 4 * raw * raw * raw : 1 - Math.pow(-2 * raw + 2, 3) / 2;
      const kids = p.g.childNodes;
      for (let i = 0; i < kids.length; i++) {
        const q = pointAt(p, Math.max(0, u - Math.max(0, i - 1) * 0.02));
        kids[i].setAttribute("cx", q.x.toFixed(1));
        kids[i].setAttribute("cy", q.y.toFixed(1));
      }
      if (p.onHub && p.hubAt >= 0 && u >= p.hubAt) {
        p.onHub();
        p.onHub = null;
      }
      if (raw >= 1) done.push(p);
    }
    for (const p of done) {
      p.g.remove();
      const i = S.particles.indexOf(p);
      if (i >= 0) S.particles.splice(i, 1);
      if (p.onEnd) p.onEnd();
    }
    if (S.particles.length) S.raf = requestAnimationFrame(frame);
  }

  function clearParticles() {
    for (const p of S.particles) p.g.remove();
    S.particles = [];
    if (S.raf) cancelAnimationFrame(S.raf);
    S.raf = 0;
  }

  function hubBeat() {
    if (!canAnimate()) return;
    const ring = document.createElement("span");
    ring.className = "ripple";
    el.hub.appendChild(ring);
    ring.addEventListener("animationend", () => ring.remove(), { once: true });
    el.hub.classList.remove("beat");
    void el.hub.offsetWidth;
    el.hub.classList.add("beat");
  }

  function ping(id, kind) {
    const node = id && S.nodes.get(id);
    if (!node || REDUCED) return;
    const cls = node.el.classList;
    cls.remove("ping-request", "ping-answer", "ping-fail", "ping-warn");
    void node.el.offsetWidth;
    cls.add("ping-" + kind);
    clearTimeout(node.pingTimer);
    node.pingTimer = setTimeout(() => cls.remove("ping-" + kind), 1100);
  }

  /* ---- Rail ---- */

  function renderRail(ov) {
    const act = ov.active.slice().sort((a, b) => ms(b.created_at) - ms(a.created_at));
    el.count.textContent = String(act.length);
    el.count.classList.toggle("zero", !act.length);
    syncList(
      el.inflight,
      act,
      (c) => c.id,
      (c) => [c.status, c.live && c.live.text, c.to_name, c.task].join("\u0001"),
      flightHTML,
      emptyHTML(ICON.radar, "Quiet right now", "Requests between decks appear here while they run."),
    );
    syncList(
      el.recent,
      ov.recent.slice(0, 30),
      (c) => c.id,
      (c) => [c.status, c.updated_at, c.end_reason].join("\u0001"),
      rowHTML,
      emptyHTML(ICON.inbox, "No requests yet", "Finished requests are listed here."),
    );
  }

  function flightHTML(c) {
    const st = status(c);
    const live = (c.live && c.live.text) || (c.status === "in_progress" ? "working…" : "waiting for the agent…");
    return `<div class="flight" role="button" tabindex="0" data-cid="${esc(c.id)}">
      <div class="flight-top"><span class="pill t-${st.tone}">${esc(st.label)}</span><span>${esc(c.mode === "delegate" ? "delegated" : "consult")}</span><span class="t" data-since="${esc(c.created_at)}">${fmtDur(now() - ms(c.created_at))}</span></div>
      ${routeHTML(c)}
      <div class="flight-task">${esc(c.task)}</div>
      <div class="flight-live"><span class="spinner"></span><span class="live-text">${esc(live)}</span></div>
    </div>`;
  }

  function rowHTML(c) {
    const st = status(c);
    return `<div class="row" role="button" tabindex="0" data-cid="${esc(c.id)}" title="${esc(st.label)}">
      <span class="sicon t-${st.tone}">${RESULT_ICON[st.tone] || ICON.dash}</span>
      <span class="row-main">${routeHTML(c)}<span class="row-task" style="display:block">${esc(c.task)}</span></span>
      <span class="row-meta"><span class="mono"${isLive(c) ? ` data-since="${esc(c.created_at)}"` : ""}>${fmtDur(took(c))}</span><span data-ago="${esc(c.updated_at)}">${esc(timeAgo(c.updated_at))}</span></span>
    </div>`;
  }

  /* ---- Requests page ---- */

  function involves(c, name) {
    return fromParts(c).place === name || toParts(c).place === name || deckOf(c.from) === name || deckOf(c.to) === name;
  }

  function renderRequests() {
    const ov = S.ov;
    if (!ov) return;
    const all = ov.active.slice().sort((a, b) => ms(b.created_at) - ms(a.created_at)).concat(ov.recent);
    const groups = {
      all,
      live: all.filter(isLive),
      done: all.filter((c) => c.status === "closed" || c.status === "responded"),
      failed: all.filter((c) => c.status === "failed" || c.status === "timeout"),
    };
    document.querySelectorAll(".seg [data-n]").forEach((n) => {
      n.textContent = String(groups[n.dataset.n].length);
    });
    document.querySelectorAll(".seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.filter === S.filter)));

    const names = new Set(ov.instances.map((i) => i.name));
    for (const c of all) {
      names.add(fromParts(c).place);
      names.add(toParts(c).place);
    }
    names.delete("");
    names.delete("?");
    names.delete("unassigned");
    const opts = ["", ...Array.from(names).sort((a, b) => a.localeCompare(b))];
    const sigOpts = opts.join("\u0001");
    if (el.reqDeck.dataset.sig !== sigOpts) {
      el.reqDeck.innerHTML = opts.map((o) => `<option value="${esc(o)}">${o ? esc(o) : "All decks"}</option>`).join("");
      el.reqDeck.dataset.sig = sigOpts;
      el.reqDeck.value = opts.includes(S.deck) ? S.deck : "";
    }

    const q = S.q.trim().toLowerCase();
    const rows = groups[S.filter].filter(
      (c) =>
        (!S.deck || involves(c, S.deck)) &&
        (!q ||
          [c.task, plain(fromParts(c)), plain(toParts(c)), c.id, c.mode, status(c).label]
            .join(" ")
            .toLowerCase()
            .includes(q)),
    );
    syncList(
      el.reqRows,
      rows,
      (c) => c.id,
      (c) => [c.status, c.updated_at, c.end_reason, c.to_name].join("\u0001"),
      trowHTML,
      emptyHTML(ICON.inbox, all.length ? "Nothing matches" : "No requests yet", all.length ? "Try another filter or search." : "Requests between Flight Decks are listed here."),
    );
  }

  function trowHTML(c) {
    const st = status(c);
    const live = isLive(c);
    return `<div class="trow" role="button" tabindex="0" data-cid="${esc(c.id)}">
      <span><span class="pill t-${st.tone}">${esc(st.label)}</span></span>
      ${routeHTML(c)}
      <span class="task" title="${esc(c.task)}">${esc(c.task)}</span>
      <span class="mode">${esc(c.mode === "delegate" ? "delegated" : "consult")}</span>
      <span class="when" title="${esc(dateTime(c.created_at))}" data-ago="${esc(c.created_at)}">${esc(timeAgo(c.created_at))}</span>
      <span class="num"${live ? ` data-since="${esc(c.created_at)}"` : ""}>${fmtDur(took(c))}</span>
    </div>`;
  }

  /* ---- Drawer ---- */

  function openDrawer(kind, id) {
    if (!id) return;
    clearTimeout(S.closeTimer);
    el.drawer.classList.remove("closing");
    if (el.drawer.hidden) {
      S.lastFocus = document.activeElement;
      el.drawer.hidden = false;
    }
    S.drawer = { kind, id, data: null };
    el.drawerBody.scrollTop = 0;
    if (kind === "concern") {
      setDrawer("Request", "Loading…", `<div class="empty"><span class="spinner"></span></div>`);
      loadConcern(id);
    } else {
      renderDeckDrawer();
    }
    requestAnimationFrame(() => el.drawerClose.focus());
  }

  function closeDrawer() {
    if (el.drawer.hidden || el.drawer.classList.contains("closing")) return;
    S.drawer = null;
    S.drawerSeq++;
    el.drawer.classList.add("closing");
    S.closeTimer = setTimeout(() => {
      el.drawer.hidden = true;
      el.drawer.classList.remove("closing");
    }, REDUCED ? 0 : 170);
    const back = S.lastFocus;
    S.lastFocus = null;
    if (back && document.contains(back) && typeof back.focus === "function") back.focus();
  }

  function setDrawer(kicker, title, body) {
    // Keep the reader's place (scroll and opened sections) across refreshes.
    const top = el.drawerBody.scrollTop;
    const open = Array.from(el.drawerBody.querySelectorAll("details[open][data-k]")).map((d) => d.dataset.k);
    el.drawerKicker.textContent = kicker;
    el.drawerTitle.textContent = title;
    el.drawerBody.innerHTML = body;
    for (const k of open) {
      const d = el.drawerBody.querySelector(`details[data-k="${CSS.escape(k)}"]`);
      if (d) d.open = true;
    }
    el.drawerBody.scrollTop = top;
  }

  function refreshDrawer() {
    const d = S.drawer;
    if (!d) return;
    if (d.kind === "deck") {
      renderDeckDrawer();
      return;
    }
    const running = S.ov && S.ov.active.some((c) => c.id === d.id);
    if (running || !d.data || isLive(d.data)) loadConcern(d.id);
  }

  async function loadConcern(id) {
    const seq = ++S.drawerSeq;
    const c = await getJSON(`/api/concerns/${encodeURIComponent(id)}`);
    if (!S.drawer || S.drawer.kind !== "concern" || S.drawer.id !== id || seq !== S.drawerSeq) return;
    if (!c || c.error) {
      if (!S.drawer.data) setDrawer("Request", shortId(id), emptyHTML(ICON.inbox, "Not found", "The hub no longer keeps this request."));
      return;
    }
    S.drawer.data = c;
    // Rebuild only when what it shows changed: focus and selection survive refreshes.
    const sig = JSON.stringify({ ...c, live: null, updated_at: null });
    if (S.drawer.sig === sig) {
      if (c.live && c.live.text) updateLiveText(c.id, c.live.text);
      return;
    }
    S.drawer.sig = sig;
    renderConcernDrawer(c);
  }

  function renderConcernDrawer(c) {
    const st = status(c);
    const live = isLive(c);
    // The full record names its ends differently from the list views.
    const f = fromParts({ from_agent: c.from_agent, from_name: c.from_instance_name, from: c.from_instance });
    const t = toParts({ to_name: c.assigned_instance_name, target_deck: c.target_deck, target_agent: c.target_agent, to: c.assigned_instance });
    const meta = c.metadata || {};
    const err = meta.error || meta.fail_reason || "";
    const msgs = c.messages || [];
    const tags = c.expertise_tags || [];
    const elapsed = (live ? now() : ms(c.updated_at)) - ms(c.created_at);
    const liveText = (c.live && c.live.text) || (c.status === "in_progress" ? "Working…" : "Waiting for the agent to pick it up…");

    let html = `<div class="d-hero"><span class="pill t-${st.tone}">${esc(st.label)}</span><span>${c.mode === "delegate" ? "Delegated task" : "Consultation"}</span>
      <span class="stat">${live ? "Running for" : "Took"}<span class="mono"${live ? ` data-since="${esc(c.created_at)}"` : ""}>${fmtDur(elapsed)}</span></span></div>`;
    if (live) html += `<div class="d-live"><span class="spinner"></span><span class="d-live-text">${esc(liveText)}</span></div>`;
    if ((c.status === "failed" || c.status === "timeout") && err) {
      html += `<div class="d-error"><b>What went wrong</b><span>${esc(err)}</span></div>`;
    }
    html += `<div class="d-route">
        <button class="d-stop" type="button" data-deck="${esc(c.from_instance)}"><small>From</small><b>${esc(f.place)}</b><span>${esc(f.agent || "—")}</span></button>
        <span class="d-arrow">${ICON.arrow}</span>
        <div class="d-stop via"><small>Via</small><b>BotPort</b><span>${esc(c.mode || "consult")}</span></div>
        <span class="d-arrow">${ICON.arrow}</span>
        <button class="d-stop" type="button" data-deck="${esc(c.assigned_instance || "")}"${c.assigned_instance ? "" : " disabled"}><small>To</small><b>${esc(t.place)}</b><span>${esc(t.agent || "—")}</span></button>
      </div>`;
    html += `<dl class="facts">
        <div class="fact"><dt>Started</dt><dd><span>${esc(dateTime(c.created_at))}</span></dd></div>
        <div class="fact"><dt>Last update</dt><dd><span data-ago="${esc(c.updated_at)}">${esc(timeAgo(c.updated_at))}</span></dd></div>
        ${meta.persona_name ? `<div class="fact"><dt>Persona</dt><dd><span>${esc(meta.persona_name)}</span></dd></div>` : ""}
        ${tags.length ? `<div class="fact"><dt>Expertise</dt><dd><span>${esc(tags.join(", "))}</span></dd></div>` : ""}
        <div class="fact wide"><dt>Request ID</dt><dd><span class="mono" data-copy-src>${esc(c.id)}</span><button class="copy" type="button" data-copy="${esc(c.id)}">Copy</button></dd></div>
      </dl>`;
    html += `<section class="d-sec"><h3>Task</h3><div class="prose">${esc(c.task)}</div></section>`;
    if (msgs.length) {
      html += `<section class="d-sec"><h3>Conversation <span class="n">${msgs.length}</span></h3><div class="timeline">${msgs.map(msgHTML).join("")}</div></section>`;
    }
    if (c.context && Object.keys(c.context).length) {
      html += `<section class="d-sec"><details class="ctx" data-k="ctx"><summary>Context sent with the request</summary><div class="code">${esc(JSON.stringify(c.context, null, 2))}</div></details></section>`;
    }
    setDrawer("Request", `${plain(f)} → ${plain(t)}`, html);
  }

  function msgHTML(m, i) {
    const label = {
      request: "Request",
      response: "Answer",
      follow_up: "Follow-up",
      context_request: "Asked for context",
      context_reply: "Context",
    }[m.direction] || m.direction;
    const by = m.from_instance ? instName(m.from_instance) : "";
    const text = m.content || "";
    const LIMIT = 1500;
    const body =
      text.length > LIMIT
        ? `${esc(text.slice(0, LIMIT))}<details class="more" data-k="m${i}"><summary>Show ${text.length - LIMIT} more characters</summary>${esc(text.slice(LIMIT))}</details>`
        : esc(text) || '<span style="color:var(--faint)">(empty)</span>';
    return `<div class="msg ${esc(m.direction)}"><div class="msg-head"><b>${esc(label)}</b>${by ? `<span>${esc(by)}</span>` : ""}<time title="${esc(dateTime(m.timestamp))}">${esc(clockTime(m.timestamp))}</time></div><div class="prose">${body}</div></div>`;
  }

  function renderDeckDrawer() {
    const id = S.drawer.id;
    const inst = S.byId.get(id);
    const traffic = S.ov ? S.ov.active.concat(S.ov.recent).filter((c) => c.from === id || c.to === id) : [];
    const sig = JSON.stringify([inst || null, traffic.map((c) => [c.id, c.status, c.updated_at])]);
    if (S.drawer.sig === sig) return;
    S.drawer.sig = sig;
    if (!inst) {
      setDrawer("Flight Deck", deckOf(id) || shortId(id), emptyHTML(ICON.radar, "Not connected", "The hub no longer knows this connection."));
      return;
    }
    const deck = inst.kind === "deck";
    const on = inst.status === "connected";
    const max = Number(inst.max_concurrent) || 0;
    const act = Number(inst.active_concerns) || 0;
    const agents = deck
      ? inst.agents || []
      : (inst.personas || []).map((p) => ({ name: p.name, description: p.description, expertise_tags: p.expertise_tags, status: "running" }));
    const all = S.ov ? S.ov.active.concat(S.ov.recent) : [];
    const mine = all.filter((c) => c.from === id || c.to === id).slice(0, 25);
    const asked = mine.filter((c) => c.from === id).length;
    const served = mine.filter((c) => c.to === id).length;

    let html = `<div class="d-hero"><span class="pill ${on ? "t-done" : "t-idle"}">${on ? "Online" : "Offline"}</span>
      <span>${deck ? "Flight Deck" : "Captain Claw agent"}${inst.version ? " " + esc(inst.version) : ""}</span>
      <span class="stat">Busy<span class="mono">${act} of ${max}</span></span></div>`;
    html += `<dl class="facts">
        <div class="fact"><dt>${on ? "Connected" : "Went offline"}</dt><dd><span>${esc(dateTime(on ? inst.connected_at : inst.disconnected_at))}</span></dd></div>
        <div class="fact"><dt>Last heartbeat</dt><dd><span data-ago="${esc(inst.last_heartbeat)}">${esc(timeAgo(inst.last_heartbeat) || "—")}</span></dd></div>
        <div class="fact"><dt>Address</dt><dd><span class="mono">${esc(inst.remote_addr || "—")}</span></dd></div>
        <div class="fact"><dt>Protocol</dt><dd><span>${esc(String(inst.protocol || 1))}</span></dd></div>
        <div class="fact"><dt>Asked others</dt><dd><span>${asked} recently</span></dd></div>
        <div class="fact"><dt>Answered for others</dt><dd><span>${served} recently</span></dd></div>
      </dl>`;
    html += `<section class="d-sec"><h3>${deck ? "Shared agents" : "Personas"} <span class="n">${agents.length}</span></h3>`;
    html += agents.length
      ? `<div class="agents-list">${agents
          .map((a) => {
            const running = !a.status || a.status === "running";
            return `<div class="agent-card"><div class="top"><b>${esc(a.name)}</b><span class="pill ${running ? "t-done" : "t-idle"}">${esc(running ? "running" : a.status)}</span></div>
              ${a.description ? `<p>${esc(a.description)}</p>` : ""}
              ${(a.expertise_tags || []).map((tg) => `<span class="tag">${esc(tg)}</span>`).join("")}</div>`;
          })
          .join("")}</div>`
      : emptyHTML(ICON.radar, "Nothing shared", deck ? "This deck offers no agents to the network yet." : "");
    html += `</section>`;
    html += `<section class="d-sec"><h3>Recent requests <span class="n">${mine.length}</span></h3>`;
    html += mine.length ? `<div>${mine.map(rowHTML).join("")}</div>` : emptyHTML(ICON.inbox, "None yet", "");
    html += `</section>`;
    setDrawer(deck ? "Flight Deck" : "Agent connection", inst.name, html);
  }

  async function copyText(text, btn) {
    let ok = false;
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch (e) {
      const src = btn.parentElement && btn.parentElement.querySelector("[data-copy-src]");
      if (src) {
        const r = document.createRange();
        r.selectNodeContents(src);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(r);
        try {
          ok = document.execCommand("copy");
        } catch (err) {
          ok = false;
        }
      }
    }
    btn.textContent = ok ? "Copied" : "Selected";
    setTimeout(() => {
      btn.textContent = "Copy";
    }, 1400);
  }

  /* ---- Routing, clock, kiosk, sign-in ---- */

  function route() {
    const page = location.hash.replace(/^#\/?/, "").split(/[/?]/)[0];
    show(page === "requests" ? "requests" : page === "swarm" ? "swarm" : "network");
  }

  function show(name) {
    if (S.route === name) return;
    if (S.route === "swarm" && typeof window.teardownSwarmPage === "function") window.teardownSwarmPage();
    S.route = name;
    document.querySelectorAll(".page").forEach((p) => {
      p.hidden = p.id !== "page-" + name;
    });
    document.querySelectorAll(".tab").forEach((t) => {
      if (t.dataset.route === name) t.setAttribute("aria-current", "page");
      else t.removeAttribute("aria-current");
    });
    clearParticles();
    if (name === "swarm") {
      stopPolling();
      if (typeof window.initSwarmPage === "function") window.initSwarmPage();
    } else {
      startPolling();
      if (S.ov && name === "network") {
        requestAnimationFrame(() => {
          renderKpis(S.ov);
          renderMap(S.ov);
          renderRail(S.ov);
        });
      }
    }
  }

  function tick1s() {
    el.clock.textContent = clockTime();
    if (S.ov) {
      const up = (S.ov.hub.uptime_seconds || 0) * 1000 + (Date.now() - S.ovAt);
      el.uptime.textContent = "up " + fmtDur(up).replace(/ \d+s$/, "");
    }
    document.querySelectorAll("[data-since]").forEach((n) => {
      n.textContent = fmtDur(now() - ms(n.dataset.since));
    });
    document.querySelectorAll("[data-ago]").forEach((n) => {
      const t = timeAgo(n.dataset.ago);
      if (t && n.textContent !== t) n.textContent = t;
    });
  }

  function setKiosk(on, fromUser) {
    S.kiosk = on;
    document.documentElement.classList.toggle("kiosk", on);
    el.kioskBtn.title = on ? "Leave kiosk mode (K)" : "Kiosk mode (K)";
    if (fromUser) {
      const root = document.documentElement;
      if (on && !document.fullscreenElement && root.requestFullscreen) root.requestFullscreen().catch(() => {});
      if (!on && document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(() => {});
    }
    if (on && S.route !== "network") location.hash = "#/";
    nudgeIdle();
    if (S.ov && S.route === "network") requestAnimationFrame(() => renderMap(S.ov));
  }

  function nudgeIdle() {
    document.documentElement.classList.remove("idle");
    clearTimeout(S.idleTimer);
    if (S.kiosk) S.idleTimer = setTimeout(() => document.documentElement.classList.add("idle"), 2500);
  }

  function showSignIn() {
    if (!el.signin.hidden) {
      el.signinErr.textContent = S.signinTried ? "That token was not accepted." : "";
      return;
    }
    el.signinErr.textContent = S.signinTried ? "That token was not accepted." : "";
    el.signin.hidden = false;
    setTimeout(() => el.signinToken.focus(), 30);
  }

  function trapFocus(e) {
    const box = $(".drawer", el.drawer);
    const items = Array.from(box.querySelectorAll('button:not([disabled]), [href], [tabindex]:not([tabindex="-1"]), summary, input, select'))
      .filter((n) => n.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    } else if (!box.contains(document.activeElement)) {
      e.preventDefault();
      first.focus();
    }
  }

  function typing(t) {
    return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
  }

  function init() {
    el = {
      live: $("#live"),
      clock: $("#clock"),
      uptime: $("#uptime"),
      version: $("#hub-version"),
      kioskBtn: $("#kiosk-btn"),
      signout: $("#signout-btn"),
      map: $("#map"),
      mapSub: $("#map-sub"),
      nodes: $("#map-nodes"),
      hub: $("#hub"),
      hubSub: $("#hub-sub"),
      edges: $("#edges"),
      parts: $("#particles"),
      empty: $("#map-empty"),
      count: $("#inflight-count"),
      inflight: $("#inflight"),
      recent: $("#recent"),
      ticks: $("#ticks"),
      reqRows: $("#req-rows"),
      reqDeck: $("#req-deck"),
      reqSearch: $("#req-search"),
      drawer: $("#drawer"),
      drawerKicker: $("#drawer-kicker"),
      drawerTitle: $("#drawer-title"),
      drawerBody: $("#drawer-body"),
      drawerClose: $("#drawer .close"),
      signin: $("#signin"),
      signinToken: $("#signin-token"),
      signinErr: $("#signin-err"),
      kpis: {},
    };
    document.querySelectorAll(".kpi").forEach((k) => {
      el.kpis[k.dataset.k] = k;
    });

    document.addEventListener("click", (e) => {
      const t = e.target;
      if (!(t instanceof Element)) return;
      if (t.closest("[data-close]")) {
        closeDrawer();
        return;
      }
      const copy = t.closest("[data-copy]");
      if (copy) {
        copyText(copy.dataset.copy, copy);
        return;
      }
      const deck = t.closest("[data-deck]");
      if (deck && deck.dataset.deck && !deck.disabled) {
        openDrawer("deck", deck.dataset.deck);
        return;
      }
      const c = t.closest("[data-cid]");
      if (c && c.dataset.cid) openDrawer("concern", c.dataset.cid);
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if (!el.drawer.hidden) {
          e.preventDefault();
          closeDrawer();
          return;
        }
        const sm = $("#swarm-modal");
        if (sm && !sm.classList.contains("hidden") && typeof window.closeSwarmModal === "function") {
          window.closeSwarmModal();
        }
        return;
      }
      if (e.key === "Tab" && !el.drawer.hidden) {
        trapFocus(e);
        return;
      }
      if ((e.key === "Enter" || e.key === " ") && e.target instanceof Element && e.target.matches('[role="button"][data-cid]')) {
        e.preventDefault();
        openDrawer("concern", e.target.dataset.cid);
        return;
      }
      if (typing(e.target) || e.metaKey || e.ctrlKey || e.altKey || !el.signin.hidden) return;
      if (e.key === "k" || e.key === "K") setKiosk(!S.kiosk, true);
      if (e.key === "/" && S.route === "requests") {
        e.preventDefault();
        el.reqSearch.focus();
      }
    });

    el.kioskBtn.addEventListener("click", () => setKiosk(!S.kiosk, true));
    el.signout.addEventListener("click", () => {
      if (window.BotPortAuth) BotPortAuth.clear();
      location.reload();
    });
    document.addEventListener("mousemove", nudgeIdle, { passive: true });

    document.querySelectorAll(".seg button").forEach((b) =>
      b.addEventListener("click", () => {
        S.filter = b.dataset.filter;
        renderRequests();
      }),
    );
    el.reqDeck.addEventListener("change", () => {
      S.deck = el.reqDeck.value;
      renderRequests();
    });
    let searchTimer = 0;
    el.reqSearch.addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        S.q = el.reqSearch.value;
        renderRequests();
      }, 120);
    });

    $("#signin-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const token = el.signinToken.value.trim();
      if (!token || !window.BotPortAuth) return;
      BotPortAuth.set(token);
      S.signinTried = true;
      el.signinToken.value = "";
      el.signin.hidden = true;
      reconnectWS();
      if (S.route === "swarm" && typeof window.initSwarmPage === "function") window.initSwarmPage();
      else refresh();
    });
    window.addEventListener("botport:unauthorized", showSignIn);

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        clearParticles();
        refresh();
      }
    });
    if (window.ResizeObserver) {
      let raf = 0;
      new ResizeObserver(() => {
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(() => {
          if (S.ov && S.route === "network") renderMap(S.ov);
        });
      }).observe(el.map);
    }

    const params = new URLSearchParams(location.search);
    const kiosk = params.get("kiosk");
    if (kiosk === "1" || kiosk === "true" || kiosk === "") setKiosk(true, false);

    window.addEventListener("hashchange", route);
    route();
    connectWS();
    setInterval(tick1s, 1000);
    tick1s();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
