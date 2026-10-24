/* BotPort dashboard auth: attaches the admin token to /api requests.
 *
 * The token comes from ?token=... (then removed from the address bar) or a
 * prompt after the first 401, and is kept in localStorage. A SameSite=Strict
 * cookie carries it for plain links (file downloads); the server accepts the
 * cookie for GET requests only, so other sites cannot use it to change state.
 */
(function () {
  const KEY = "botport_admin_token";
  let asked = false;

  function readToken() {
    try { return localStorage.getItem(KEY) || ""; } catch { return ""; }
  }

  function saveToken(t) {
    try { localStorage.setItem(KEY, t); } catch { /* private mode */ }
    setCookie(t);
  }

  function setCookie(t) {
    const secure = location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `botport_token=${encodeURIComponent(t)}; Path=/; SameSite=Strict${secure}`;
  }

  const fromUrl = new URLSearchParams(location.search).get("token");
  if (fromUrl) {
    saveToken(fromUrl);
    const clean = location.pathname + location.hash;
    history.replaceState(null, "", clean);
  } else if (readToken()) {
    setCookie(readToken());
  }

  const origFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const isApi = url.startsWith("/api") || url.startsWith(location.origin + "/api");
    if (!isApi) return origFetch(input, init);

    const opts = Object.assign({}, init || {});
    const headers = new Headers(opts.headers || (typeof input !== "string" && input.headers) || {});
    const token = readToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    opts.headers = headers;

    const res = await origFetch(input, opts);
    if (res.status === 401 && !asked) {
      asked = true;
      const entered = window.prompt("BotPort admin token (server.admin_token):");
      if (entered) {
        saveToken(entered.trim());
        location.reload();
      }
    }
    return res;
  };
})();
