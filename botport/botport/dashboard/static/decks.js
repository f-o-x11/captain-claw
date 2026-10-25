/* BotPort hub dashboard: the Decks page (vanilla JS, no build step).
 *
 * Every Flight Deck that may join this hub, with the label and description
 * its operator gave it. Adding one makes its key and secret here and shows
 * them once, with a join code the deck's Network settings take in one paste;
 * the deck can connect at once (the hub reads keys live). Rotating or
 * removing a key drops the deck that uses it.
 *
 * Data: GET /api/decks; changes go to /api/decks (JSON). dashboard.js shows
 * and hides the page and forwards the live events that concern decks. Uses
 * the helpers $, esc and timeAgo from dashboard.js.
 */
"use strict";

(function () {
  const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/;
  const LABEL_MAX = 80;
  const DESC_MAX = 500;
  const ADDR_KEY = "botport_deck_hub_address";
  const svg = (d) =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  const I = {
    plus: svg('<path d="M12 5v14M5 12h14"/>'),
    edit: svg('<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>'),
    rotate: svg('<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>'),
    trash: svg('<path d="M4 7h16M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13M9 7V4h6v3"/>'),
    key: svg('<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M15 8l2 2"/>'),
    deck: svg('<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>'),
    check: svg('<path d="M5 12.5l4.5 4.5L19 7.5"/>'),
    close: svg('<path d="M6 6l12 12M18 6L6 18"/>'),
    copy: svg('<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>'),
    warn: svg('<path d="M12 3l9.5 17h-19z"/><path d="M12 10v4M12 17.5v.01"/>'),
    shield: svg('<path d="M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/>'),
    link: svg('<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>'),
  };

  const D = {
    on: false,
    data: null,
    timer: 0,
    soonTimer: 0,
    loading: false,
    modal: null, // { kind, ... }
    lastFocus: null,
    pendingEdit: "",
  };
  let el = null;

  /* ---- Helpers ---- */

  function ago(iso) {
    return (window.timeAgo && timeAgo(iso)) || "";
  }

  function initials(text) {
    const words = String(text || "?").replace(/[-_.]+/g, " ").trim().split(/\s+/).filter(Boolean);
    const s = words.length > 1 ? words[0][0] + words[1][0] : (words[0] || "?").slice(0, 2);
    return s.toUpperCase();
  }

  // A stable hue per deck, so each one keeps its colour across views.
  function hue(name) {
    let h = 0;
    for (const ch of String(name).toLowerCase()) h = (h * 31 + ch.charCodeAt(0)) % 360;
    return h;
  }

  function slug(text) {
    return String(text || "")
      .normalize("NFKD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9_.-]+/g, "-")
      .replace(/^[^a-z0-9]+/, "")
      .replace(/-+/g, "-")
      .replace(/-$/, "")
      .slice(0, 40);
  }

  function hubAddress() {
    try {
      const saved = localStorage.getItem(ADDR_KEY);
      if (saved) return saved;
    } catch (e) {
      /* storage blocked: use the page's own address */
    }
    return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
  }

  function rememberAddress(v) {
    try {
      const own = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
      if (v && v !== own) localStorage.setItem(ADDR_KEY, v);
      else localStorage.removeItem(ADDR_KEY);
    } catch (e) {
      /* not remembered: fine */
    }
  }

  function loopbackHost(host) {
    return /^(localhost|127\.0\.0\.1|\[::1\]|::1)$/i.test(host || "");
  }

  // What the hub-address field says under it, for the address in it now.
  function urlHint(u) {
    let host = "";
    try {
      host = new URL(/^[a-z]+:\/\//i.test(u) ? u : "ws://" + u).hostname;
    } catch (e) {
      return { warn: true, html: "That doesn't look like an address: <code>ws://host:port/ws</code> or <code>wss://name/ws</code>." };
    }
    if (loopbackHost(host)) {
      return {
        warn: true,
        html: "Only a deck on this same machine reaches the hub at this address. For the others, put the one they use (its LAN or public name), e.g. <code>ws://192.168.1.20:23180/ws</code>.",
      };
    }
    if (u === `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`) {
      return { warn: false, html: "Taken from this page's address. Change it if decks reach the hub another way (a different host, port or a TLS proxy)." };
    }
    return { warn: false, html: "The deck connects to this address." };
  }

  function b64url(text) {
    const bytes = new TextEncoder().encode(text);
    let bin = "";
    bytes.forEach((b) => {
      bin += String.fromCharCode(b);
    });
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  // Everything a deck needs, in one paste: its Network settings decode it.
  function joinCode(c, url) {
    return "bpj1." + b64url(JSON.stringify({ u: url, n: c.deck_name, k: c.key, s: c.secret }));
  }

  function envBlock(c, url) {
    return [
      "FD_BOTPORT_ENABLED=true",
      `FD_BOTPORT_URL=${url}`,
      `FD_BOTPORT_DECK_NAME=${c.deck_name}`,
      `FD_BOTPORT_KEY=${c.key}`,
      `FD_BOTPORT_SECRET=${c.secret}`,
    ].join("\n");
  }

  async function api(method, url, body) {
    const opts = { method, cache: "no-store", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(url, opts);
    } catch (e) {
      throw new Error("The hub does not answer. Check that it is running, then try again.");
    }
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = null;
    }
    if (!res.ok) {
      if (res.status === 401) throw new Error("Sign in with the hub's admin token first.");
      throw new Error((data && (data.error || data.detail)) || `The hub said ${res.status}.`);
    }
    return data;
  }

  async function copy(text, btn) {
    let ok = false;
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch (e) {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        ok = document.execCommand("copy");
      } catch (err) {
        ok = false;
      }
      ta.remove();
    }
    if (btn) {
      const label = btn.querySelector(".lbl");
      const was = label ? label.textContent : "";
      btn.classList.toggle("ok", ok);
      if (label) label.textContent = ok ? "Copied" : "Copy failed";
      clearTimeout(btn._t);
      btn._t = setTimeout(() => {
        btn.classList.remove("ok");
        if (label) label.textContent = was;
      }, 1500);
    }
    if (ok && D.modal && D.modal.kind === "creds") D.modal.copied = true;
    return ok;
  }

  function toast(text) {
    let box = document.getElementById("deck-toast");
    if (!box) {
      box = document.createElement("div");
      box.id = "deck-toast";
      box.className = "dtoast";
      box.setAttribute("role", "alert");
      document.body.appendChild(box);
    }
    box.innerHTML = `${I.warn}<span>${esc(text)}</span>`;
    box.classList.remove("show");
    void box.offsetWidth;
    box.classList.add("show");
    clearTimeout(box._t);
    box._t = setTimeout(() => box.classList.remove("show"), 5000);
  }

  /* ---- Page ---- */

  async function load() {
    if (D.loading) return;
    D.loading = true;
    try {
      const data = await api("GET", "/api/decks");
      D.data = data;
      render();
      if (D.pendingEdit) {
        const name = D.pendingEdit;
        D.pendingEdit = "";
        const d = findDeck(name);
        if (d) openEdit(d);
      }
    } catch (e) {
      if (!D.data) {
        el.grid.innerHTML = `<div class="deck-empty"><div class="de-icon">${I.warn}</div><b>Cannot load the decks</b><span>${esc(e.message)}</span></div>`;
      }
    } finally {
      D.loading = false;
    }
  }

  function soon() {
    clearTimeout(D.soonTimer);
    D.soonTimer = setTimeout(load, 250);
  }

  function findDeck(name) {
    const low = String(name || "").toLowerCase();
    return D.data ? D.data.decks.find((d) => d.name.toLowerCase() === low) : null;
  }

  function render() {
    const data = D.data;
    if (!data) return;
    const decks = data.decks;
    const online = decks.filter((d) => d.status === "online").length;
    el.sub.textContent = decks.length
      ? `${online} of ${decks.length} online. Add one and it can connect at once, no restart needed.`
      : "Each Flight Deck that joins this hub gets its own key. Add one and it can connect at once, no restart needed.";
    const warnings = (data.warnings || [])
      .map((w) => `<div class="deck-banner">${I.warn}<div><b>Two keys for one deck</b><span>${esc(w)}</span></div></div>`)
      .join("");
    el.banner.innerHTML =
      (data.auth_enabled
        ? ""
        : `<div class="deck-banner">${I.warn}<div><b>This hub is open: any Flight Deck that can reach it joins, without a key.</b>
         <span>Set <code>auth.enabled: true</code> in botport.yaml and restart once; then add decks here, and only decks with a key (from this page or the file) can connect. Names and descriptions work either way.</span></div></div>`) + warnings;
    if (!decks.length) {
      el.grid.innerHTML = `<div class="deck-empty">
        <div class="de-orbit"><span></span><span></span><i>${I.deck}</i></div>
        <b>No Flight Decks yet</b>
        <span>Add the first one: you get a join code to paste into that deck, and it connects right away.</span>
        <button class="btn-grad" type="button" data-deck-act="add">${I.plus}<span>Add Flight Deck</span></button>
      </div>`;
      return;
    }
    const focusKey = document.activeElement && document.activeElement.closest && document.activeElement.closest("[data-deck-name]");
    const focusAct = focusKey && document.activeElement.dataset.deckAct;
    const focusName = focusKey && focusKey.dataset.deckName;
    el.grid.innerHTML = decks.map(cardHTML).join("");
    if (focusName && focusAct) {
      const again = el.grid.querySelector(`[data-deck-name="${CSS.escape(focusName)}"] [data-deck-act="${focusAct}"]`);
      if (again) again.focus();
    }
  }

  function statusLine(d) {
    if (d.status === "online") return `<span class="ds-on">Online</span> · since <span data-ago="${esc(d.connected_at)}">${esc(ago(d.connected_at))}</span>`;
    if (d.status === "offline") return `<span class="ds-off">Offline</span> · seen <span data-ago="${esc(d.disconnected_at || d.last_heartbeat)}">${esc(ago(d.disconnected_at || d.last_heartbeat))}</span>`;
    return `<span class="ds-never">Waiting for its first connection</span>`;
  }

  function cardHTML(d) {
    const title = d.label || d.name;
    const agents = d.agents || [];
    const chips = agents
      .slice(0, 5)
      .map((a) => `<span class="dchip${a.available ? "" : " off"}">${esc(a.name)}</span>`)
      .join("") + (agents.length > 5 ? `<span class="dchip more">+${agents.length - 5}</span>` : "");
    const auth = D.data && D.data.auth_enabled;
    const source = d.managed
      ? `<span class="dsrc dash" title="Made on this page: rotate or remove it here">${I.key}Dashboard key</span>${
          d.in_file ? `<span class="dsrc file" title="It also has a key in botport.yaml">${I.key}botport.yaml</span>` : ""}`
      : d.in_file
        ? `<span class="dsrc file" title="This deck's key is in botport.yaml (auth.keys)">${I.key}botport.yaml</span>`
        : auth
          ? `<span class="dsrc open" title="Auth is on: without a key this deck cannot connect">No key yet</span>`
          : `<span class="dsrc open" title="The hub's auth is off: this deck connected without a key">No key · open hub</span>`;
    // A deck with a name here but no key: make it one, or forget the name.
    const keyless = !d.managed && !d.in_file;
    const desc = d.description
      ? `<p class="deck-desc">${esc(d.description)}</p>`
      : `<button class="deck-desc empty" type="button" data-deck-act="edit">Add a description, so everyone can tell what ${esc(title)} is for</button>`;
    const busy = d.status === "online" && d.max_concurrent ? `<span class="dmeta"><small>Busy</small>${d.active_concerns} of ${d.max_concurrent}</span>` : "";
    return `<article class="deck-card s-${esc(d.status)}" data-deck-name="${esc(d.name)}" style="--h:${hue(d.name)}">
      <header class="deck-top">
        <span class="deck-ava" aria-hidden="true">${esc(initials(title))}</span>
        <div class="deck-id">
          <h3 title="${esc(title)}">${esc(title)}</h3>
          <div class="deck-name"><code>${esc(d.name)}</code>${d.version ? `<span>Flight Deck ${esc(d.version)}</span>` : ""}</div>
        </div>
        <span class="pill ${d.status === "online" ? "t-done" : d.status === "offline" ? "t-idle" : "t-warn"}">${d.status === "online" ? "Online" : d.status === "offline" ? "Offline" : "New"}</span>
      </header>
      ${desc}
      <div class="deck-status">${statusLine(d)}</div>
      <div class="deck-agents">${agents.length ? chips : `<span class="dchip none">${d.status === "never" ? "Agents it shares appear here" : "No agents shared"}</span>`}</div>
      <div class="deck-facts">
        <span class="dmeta"><small>Agents</small>${agents.length}</span>
        ${busy}
        ${d.remote_addr ? `<span class="dmeta"><small>From</small><span class="mono">${esc(d.remote_addr)}</span></span>` : ""}
        ${d.rotated_at ? `<span class="dmeta"><small>Secret replaced</small><span data-ago="${esc(d.rotated_at)}">${esc(ago(d.rotated_at))}</span></span>` : ""}
      </div>
      <footer class="deck-foot">
        <div class="deck-key">${source}${d.key ? `<code title="Key id (not secret)">${esc(d.key)}</code>` : ""}</div>
        <div class="deck-acts">
          <button class="dbtn" type="button" data-deck-act="edit" title="Name and description">${I.edit}<span>Edit</span></button>
          ${d.managed ? `${auth ? `<button class="dbtn" type="button" data-deck-act="rotate" title="Make a new secret">${I.rotate}<span>Rotate</span></button>` : ""}
          <button class="dbtn danger" type="button" data-deck-act="remove" title="Remove from this hub">${I.trash}<span>Remove</span></button>` : ""}
          ${keyless && auth ? `<button class="dbtn" type="button" data-deck-act="makekey" title="Give this deck a key and a join code">${I.key}<span>Make key</span></button>` : ""}
          ${keyless && d.has_record && d.status !== "online" ? `<button class="dbtn danger" type="button" data-deck-act="forget" title="Forget its name and description">${I.trash}<span>Forget</span></button>` : ""}
        </div>
      </footer>
    </article>`;
  }

  /* ---- Modals ---- */

  function openModal(m, html) {
    if (!D.modal) D.lastFocus = document.activeElement;
    D.modal = m;
    el.card.className = "dm-card" + (m.wide ? " wide" : "") + (m.tone ? " " + m.tone : "");
    el.card.innerHTML = html;
    el.modal.hidden = false;
    el.modal.classList.remove("closing");
    requestAnimationFrame(() => {
      const first = el.card.querySelector("[autofocus]") || el.card.querySelector("input, textarea, button.dm-primary, button");
      if (first) first.focus();
    });
  }

  function closeModal(force) {
    const m = D.modal;
    if (!m) return;
    if (m.busy && !force) return;
    if (m.kind === "creds" && !m.copied && !force && !m.confirmLeave) {
      m.confirmLeave = true;
      const btn = el.card.querySelector("[data-m='done']");
      if (btn) {
        btn.textContent = "Close without copying?";
        btn.classList.add("warn");
      }
      const note = el.card.querySelector(".dm-leave");
      if (note) note.hidden = false;
      return;
    }
    D.modal = null;
    el.modal.classList.add("closing");
    setTimeout(() => {
      if (D.modal) return;
      el.modal.hidden = true;
      el.modal.classList.remove("closing");
      el.card.innerHTML = "";
      if (D.lastFocus && document.contains(D.lastFocus)) D.lastFocus.focus();
    }, 170);
  }

  function head(icon, title, sub, tone) {
    return `<header class="dm-head${tone ? " " + tone : ""}">
      <span class="dm-icon">${icon}</span>
      <div class="dm-titles"><h2 id="dm-title">${title}</h2>${sub ? `<p>${sub}</p>` : ""}</div>
      <button class="icon-btn dm-x" type="button" data-m="close" aria-label="Close" title="Close (Esc)">${I.close}</button>
    </header>`;
  }

  function setBusy(on, label) {
    if (!D.modal) return;
    D.modal.busy = on;
    el.card.querySelectorAll("button, input, textarea").forEach((n) => {
      if (n.dataset.m === "close") return;
      n.disabled = on;
    });
    const p = el.card.querySelector(".dm-primary");
    if (p) {
      if (on) {
        p.dataset.label = p.innerHTML;
        p.innerHTML = `<span class="spinner"></span><span>${esc(label || "Working…")}</span>`;
      } else if (p.dataset.label) {
        p.innerHTML = p.dataset.label;
      }
    }
  }

  function showError(text) {
    const box = el.card.querySelector(".dm-error");
    if (!box) return;
    box.textContent = text || "";
    box.hidden = !text;
  }

  /* Add: name it, then its credentials. */

  // An open hub (auth off) makes no keys: any deck joins without one, and a
  // key made while anyone can reach this page could outlive that.
  function openAuthOff() {
    openModal(
      { kind: "authoff", tone: "warn" },
      `${head(I.shield, "Turn on auth first", "", "warn")}
      <div class="dm-body">
        <ul class="dm-points">
          <li>${I.link}<span>This hub runs with auth off: any Flight Deck that reaches it can join, no key needed. On such a hub, keys change nothing.</span></li>
          <li>${I.key}<span>Set <code>auth.enabled: true</code> in botport.yaml and restart the hub once. Then add each Flight Deck here: it gets a key and a join code, and connects without further restarts.</span></li>
          <li>${I.shield}<span>Keys are made only once auth is on, so none can be created while anyone may use this page.</span></li>
        </ul>
      </div>
      <footer class="dm-foot">
        <button class="btn-grad dm-primary" type="button" data-m="close">Got it</button>
      </footer>`,
    );
  }

  function openAdd() {
    if (D.data && !D.data.auth_enabled) {
      openAuthOff();
      return;
    }
    openModal(
      { kind: "add", nameEdited: false },
      `${head(I.deck, "Add a Flight Deck", "It gets its own key. Paste the join code into that deck and it connects, no restart.")}
      <form class="dm-body" id="dm-form" novalidate>
        <label class="dm-field">
          <span class="dm-lbl">Display name</span>
          <input class="field" name="label" maxlength="${LABEL_MAX}" placeholder="Kitchen Pi" autocomplete="off" autofocus>
          <span class="dm-hint">What people see on this dashboard and in every Flight Deck's Network view.</span>
        </label>
        <label class="dm-field">
          <span class="dm-lbl">Deck name <em>its address on the network</em></span>
          <span class="dm-name"><input class="field mono" name="name" maxlength="63" placeholder="kitchen-pi" autocomplete="off" spellcheck="false"></span>
          <span class="dm-hint" data-hint="name">Other decks reach its agents as <b class="dm-addr">Researcher@<i>kitchen-pi</i></b>. Letters, digits, <code>-</code> <code>_</code> <code>.</code>; it can't be changed later.</span>
        </label>
        <label class="dm-field">
          <span class="dm-lbl">Description <em>optional</em></span>
          <textarea class="field" name="description" rows="3" maxlength="${DESC_MAX}" placeholder="Who runs it, what's on it, what it's for"></textarea>
          <span class="dm-hint dm-count"><span data-count>0</span>/${DESC_MAX}</span>
        </label>
        <div class="dm-error" role="alert" hidden></div>
      </form>
      <footer class="dm-foot">
        <button class="dbtn ghost" type="button" data-m="close">Cancel</button>
        <button class="btn-grad dm-primary" type="submit" form="dm-form">${I.key}<span>Create key</span></button>
      </footer>`,
    );
    const form = $("#dm-form", el.card);
    const label = form.elements.label;
    const name = form.elements.name;
    const desc = form.elements.description;
    const addr = $(".dm-addr i", el.card);
    const hint = $("[data-hint='name']", el.card);
    const sync = () => {
      const v = name.value.trim();
      addr.textContent = v || "kitchen-pi";
      const bad = v && !NAME_RE.test(v);
      name.classList.toggle("bad", !!bad);
      hint.classList.toggle("bad", !!bad);
    };
    label.addEventListener("input", () => {
      if (!D.modal.nameEdited) name.value = slug(label.value);
      sync();
    });
    name.addEventListener("input", () => {
      D.modal.nameEdited = name.value.trim() !== "";
      sync();
    });
    desc.addEventListener("input", () => {
      $("[data-count]", el.card).textContent = String(desc.value.length);
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const n = name.value.trim();
      if (!NAME_RE.test(n)) {
        showError(n ? "The deck name may use letters, digits, '-', '_' and '.', and must start with a letter or digit." : "Give the deck a name.");
        name.focus();
        return;
      }
      showError("");
      setBusy(true, "Creating key…");
      try {
        const out = await api("POST", "/api/decks", { name: n, label: label.value, description: desc.value });
        setBusy(false);
        soon();
        openCreds(out.deck, out.credentials, "new");
      } catch (err) {
        setBusy(false);
        showError(err.message);
      }
    });
  }

  function openCreds(deck, c, why) {
    const title = esc(deck.label || deck.name);
    const url = hubAddress();
    const heading = why === "new" ? `${title} is ready to join` : `New secret for ${title}`;
    const sub =
      why === "new"
        ? "Paste the join code into that deck: <b>Admin console → BotPort → Join code</b>. It connects as soon as you save."
        : "The old secret no longer works. Paste the new join code into the deck's <b>Admin console → BotPort</b> to bring it back.";
    openModal(
      { kind: "creds", deck: deck.name, c, copied: false, wide: true, online: deck.status === "online" && why !== "rotate" },
      `${head(I.check, heading, sub, "ok")}
      <div class="dm-body">
        <div class="dm-join">
          <div class="dm-join-top"><span class="dm-lbl">Join code</span><span class="dm-once">${I.shield}Shown only now</span></div>
          <div class="dm-code mono" data-join></div>
          <button class="btn-grad dm-copy-main" type="button" data-copy-join>${I.copy}<span class="lbl">Copy join code</span></button>
        </div>

        <label class="dm-field">
          <span class="dm-lbl">Hub address <em>as the deck reaches it</em></span>
          <input class="field mono" data-url value="${esc(url)}" spellcheck="false" autocomplete="off">
          <span class="dm-hint" data-url-hint></span>
        </label>

        <details class="dm-more">
          <summary>Enter the fields one by one</summary>
          <dl class="dm-kv">
            ${kv("Hub URL", "", "url")}
            ${kv("Deck name", c.deck_name)}
            ${kv("Key", c.key)}
            ${kv("Secret", c.secret, "", true)}
          </dl>
        </details>
        <details class="dm-more">
          <summary>Headless deck: environment variables</summary>
          <div class="dm-env"><pre class="mono" data-env></pre><button class="dbtn" type="button" data-copy-env>${I.copy}<span class="lbl">Copy</span></button></div>
        </details>

        <div class="dm-wait" data-wait>
          <span class="spinner"></span><span>Waiting for <b>${title}</b> to connect…</span>
        </div>
        <p class="dm-foot-note">${I.shield}<span>The hub keeps only a fingerprint of this secret, never the secret itself. Lost it? Rotate to make a new one.</span></p>
        <p class="dm-leave" hidden>You haven't copied the join code or the secret. Once this closes they can't be shown again (you can rotate for a new secret).</p>
      </div>
      <footer class="dm-foot">
        <button class="btn-grad dm-done" type="button" data-m="done">Done</button>
      </footer>`,
    );
    const input = $("[data-url]", el.card);
    const fill = () => {
      const u = input.value.trim();
      $("[data-join]", el.card).textContent = joinCode(c, u);
      $("[data-env]", el.card).textContent = envBlock(c, u);
      const kvUrl = $("[data-kv='url']", el.card);
      if (kvUrl) kvUrl.textContent = u;
      $("[data-kv-copy='url']", el.card).dataset.value = u;
      const hint = urlHint(u);
      const box = $("[data-url-hint]", el.card);
      box.innerHTML = hint.html;
      box.classList.toggle("warn", hint.warn);
    };
    input.addEventListener("input", () => {
      fill();
      rememberAddress(input.value.trim());
    });
    fill();
    updateWait();
  }

  function kv(label, value, key, secret) {
    const k = key || label.toLowerCase().replace(/\s+/g, "-");
    return `<div class="dm-kv-row"><dt>${esc(label)}</dt><dd><code class="mono${secret ? " secret" : ""}" data-kv="${k}">${esc(value)}</code>
      <button class="dbtn icon" type="button" data-kv-copy="${k}" data-value="${esc(value)}" aria-label="Copy ${esc(label)}">${I.copy}<span class="lbl">Copy</span></button></dd></div>`;
  }

  function updateWait() {
    const m = D.modal;
    if (!m || m.kind !== "creds") return;
    const box = $("[data-wait]", el.card);
    if (!box) return;
    const title = esc((findDeck(m.deck) || {}).label || m.deck);
    if (m.online) {
      box.classList.add("on");
      box.innerHTML = `<span class="dm-tick">${I.check}</span><span><b>${title}</b> is online and on the network.</span>`;
    }
  }

  /* Edit: label and description. */

  function openEdit(d) {
    const title = esc(d.label || d.name);
    openModal(
      { kind: "edit", deck: d.name },
      `${head(I.edit, `Edit ${title}`, `Deck name <code>${esc(d.name)}</code>${d.in_file ? " · key in botport.yaml" : ""}`)}
      <form class="dm-body" id="dm-form" novalidate>
        <label class="dm-field">
          <span class="dm-lbl">Display name</span>
          <input class="field" name="label" maxlength="${LABEL_MAX}" value="${esc(d.label)}" placeholder="${esc(d.name)}" autocomplete="off" autofocus>
        </label>
        <label class="dm-field">
          <span class="dm-lbl">Description</span>
          <textarea class="field" name="description" rows="4" maxlength="${DESC_MAX}" placeholder="Who runs it, what's on it, what it's for">${esc(d.description)}</textarea>
          <span class="dm-hint dm-count"><span data-count>${d.description.length}</span>/${DESC_MAX}</span>
        </label>
        <p class="dm-hint">Every Flight Deck on the network sees these, next to <code>${esc(d.name)}</code>.</p>
        <div class="dm-error" role="alert" hidden></div>
      </form>
      <footer class="dm-foot">
        <button class="dbtn ghost" type="button" data-m="close">Cancel</button>
        <button class="btn-grad dm-primary" type="submit" form="dm-form">${I.check}<span>Save</span></button>
      </footer>`,
    );
    const form = $("#dm-form", el.card);
    form.elements.description.addEventListener("input", () => {
      $("[data-count]", el.card).textContent = String(form.elements.description.value.length);
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      showError("");
      setBusy(true, "Saving…");
      try {
        await api("PATCH", `/api/decks/${encodeURIComponent(d.name)}`, {
          label: form.elements.label.value,
          description: form.elements.description.value,
        });
        setBusy(false);
        closeModal(true);
        soon();
      } catch (err) {
        setBusy(false);
        showError(err.message);
      }
    });
  }

  /* Rotate and remove: say what happens, then do it. */

  function openRotate(d) {
    const title = esc(d.label || d.name);
    const live = d.status === "online";
    openModal(
      { kind: "rotate", deck: d.name, tone: "warn" },
      `${head(I.rotate, `Replace ${title}'s secret?`, "", "warn")}
      <div class="dm-body">
        <ul class="dm-points">
          <li>${I.key}<span>The hub makes a new secret for key <code>${esc(d.key)}</code>. The old one stops working at once.</span></li>
          <li>${I.link}<span>${live ? `${title} is online: it disconnects now, and comes back when you paste the new join code into it.` : `${title} needs the new join code before it can connect again.`}</span></li>
          <li>${I.shield}<span>Use this when a secret may have leaked, or was lost.</span></li>
        </ul>
        <div class="dm-error" role="alert" hidden></div>
      </div>
      <footer class="dm-foot">
        <button class="dbtn ghost" type="button" data-m="close">Cancel</button>
        <button class="btn-grad warn dm-primary" type="button" data-m="go">${I.rotate}<span>Replace secret</span></button>
      </footer>`,
    );
    D.modal.go = async () => {
      setBusy(true, "Replacing…");
      try {
        const out = await api("POST", `/api/decks/${encodeURIComponent(d.name)}/rotate`, {});
        setBusy(false);
        soon();
        openCreds(out.deck, out.credentials, "rotate");
      } catch (err) {
        setBusy(false);
        showError(err.message);
      }
    };
  }

  function openRemove(d) {
    const title = esc(d.label || d.name);
    const live = d.status === "online";
    openModal(
      { kind: "remove", deck: d.name, tone: "danger" },
      `${head(I.trash, `Remove ${title} from this hub?`, "", "danger")}
      <div class="dm-body">
        <ul class="dm-points">
          <li>${I.key}<span>Its key <code>${esc(d.key)}</code> stops working at once${live && d.via === "dashboard" ? `, and ${title} disconnects now` : ""}.</span></li>
          <li>${I.link}<span>Other decks can no longer reach its agents. Requests still running with it will fail.</span></li>
          <li>${I.edit}<span>Its display name and description are forgotten. To bring it back, add it again and paste the new join code into it.</span></li>
          ${d.in_file ? `<li>${I.warn}<span>It also has a key in botport.yaml, which keeps working: remove that entry and restart the hub to shut it out.</span></li>` : ""}
        </ul>
        <div class="dm-error" role="alert" hidden></div>
      </div>
      <footer class="dm-foot">
        <button class="dbtn ghost" type="button" data-m="close">Cancel</button>
        <button class="btn-grad danger dm-primary" type="button" data-m="go">${I.trash}<span>Remove deck</span></button>
      </footer>`,
    );
    D.modal.go = async () => {
      setBusy(true, "Removing…");
      try {
        await api("DELETE", `/api/decks/${encodeURIComponent(d.name)}`);
        setBusy(false);
        closeModal(true);
        soon();
      } catch (err) {
        setBusy(false);
        showError(err.message);
      }
    };
  }

  /* A deck the hub knows without a key: give it one (its name and
   * description stay), or forget it. */

  async function makeKey(d, btn) {
    btn.disabled = true;
    try {
      const out = await api("POST", "/api/decks", { name: d.name, label: d.label, description: d.description });
      soon();
      openCreds(out.deck, out.credentials, "new");
    } catch (err) {
      toast(err.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function forget(d, btn) {
    btn.disabled = true;
    try {
      await api("DELETE", `/api/decks/${encodeURIComponent(d.name)}`);
      soon();
    } catch (err) {
      toast(err.message);
    } finally {
      btn.disabled = false;
    }
  }

  function trap(e) {
    const items = Array.from(el.card.querySelectorAll("button:not([disabled]), input:not([disabled]), textarea:not([disabled]), summary, [href]")).filter(
      (n) => n.offsetParent !== null,
    );
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    } else if (!el.card.contains(document.activeElement)) {
      e.preventDefault();
      first.focus();
    }
  }

  /* ---- Wiring ---- */

  function init() {
    if (el) return; // dashboard.js may show the page before DOMContentLoaded reaches us
    el = {
      page: $("#page-decks"),
      sub: $("#decks-sub"),
      banner: $("#decks-banner"),
      grid: $("#deck-grid"),
      modal: $("#deck-modal"),
      card: $("#deck-modal .dm-card"),
    };
    if (!el.page) return;

    el.page.addEventListener("click", (e) => {
      const btn = e.target instanceof Element && e.target.closest("[data-deck-act]");
      if (!btn) return;
      const act = btn.dataset.deckAct;
      if (act === "add") {
        openAdd();
        return;
      }
      const card = btn.closest("[data-deck-name]");
      const d = card && findDeck(card.dataset.deckName);
      if (!d) return;
      if (act === "edit") openEdit(d);
      else if (act === "rotate") openRotate(d);
      else if (act === "remove") openRemove(d);
      else if (act === "makekey") makeKey(d, btn);
      else if (act === "forget") forget(d, btn);
    });

    el.modal.addEventListener("click", (e) => {
      const t = e.target instanceof Element ? e.target : null;
      if (!t) return;
      if (t.matches("[data-mclose]")) {
        // A click outside never throws away credentials: those close with Done.
        if (D.modal && D.modal.kind !== "creds") closeModal();
        return;
      }
      const m = t.closest("[data-m]");
      if (m) {
        if (m.dataset.m === "close") closeModal();
        else if (m.dataset.m === "done") closeModal();
        else if (m.dataset.m === "go" && D.modal && D.modal.go && !D.modal.busy) D.modal.go();
        return;
      }
      const cm = D.modal && D.modal.c;
      if (!cm) return;
      const join = t.closest("[data-copy-join]");
      if (join) {
        copy($("[data-join]", el.card).textContent, join);
        return;
      }
      const env = t.closest("[data-copy-env]");
      if (env) {
        copy($("[data-env]", el.card).textContent, env);
        return;
      }
      const one = t.closest("[data-kv-copy]");
      if (one) copy(one.dataset.value, one);
    });

    document.addEventListener("keydown", (e) => {
      if (!D.modal) return;
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopImmediatePropagation();
        closeModal();
      } else if (e.key === "Tab") {
        trap(e);
      }
    }, true);
  }

  window.BotPortDecks = {
    show(opts) {
      if (!el) init();
      if (!el.page) return;
      D.on = true;
      if (opts && opts.edit) D.pendingEdit = opts.edit;
      load();
      clearInterval(D.timer);
      D.timer = setInterval(() => {
        if (!document.hidden && !D.modal) load();
      }, 8000);
    },
    hide() {
      D.on = false;
      clearInterval(D.timer);
      D.timer = 0;
      if (D.modal) closeModal(true);
    },
    add() {
      if (!el) init();
      openAdd();
    },
    onEvent(ev) {
      if (!D.on) return;
      if (ev.type === "instance_connected" && ev.instance && ev.instance.kind === "deck" && D.modal && D.modal.kind === "creds") {
        if (String(ev.instance.name).toLowerCase() === String(D.modal.deck).toLowerCase()) {
          D.modal.online = true;
          updateWait();
        }
      }
      if (ev.type === "instance_disconnected" && D.modal && D.modal.kind === "creds" && ev.instance_id === "deck:" + String(D.modal.deck).toLowerCase()) {
        D.modal.online = false;
      }
      soon();
    },
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
