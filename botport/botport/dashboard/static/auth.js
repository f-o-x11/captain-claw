/* BotPort dashboard auth: attaches the admin token to /api requests.
 *
 * The token comes from ?token=... (then removed from the address bar) or the
 * dashboard's sign-in screen, and is kept in localStorage. A SameSite=Strict
 * cookie carries it for plain links (file downloads) and the live-event
 * socket; the server accepts the cookie for GET requests only, so other sites
 * cannot use it to change state. A 401 raises "botport:unauthorized", which
 * the dashboard answers with its sign-in screen.
 */
(function () {
  const KEY = "botport_admin_token";

  function readToken() {
    try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; }
  }

  function setCookie(t) {
    const secure = location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `botport_token=${encodeURIComponent(t)}; Path=/; SameSite=Strict${secure}`;
  }

  function saveToken(t) {
    try { localStorage.setItem(KEY, t); } catch (e) { /* private mode: the cookie still works */ }
    setCookie(t);
  }

  function clearToken() {
    try { localStorage.removeItem(KEY); } catch (e) { /* nothing stored */ }
    document.cookie = "botport_token=; Path=/; Max-Age=0; SameSite=Strict";
  }

  const fromUrl = new URLSearchParams(location.search).get("token");
  if (fromUrl) {
    saveToken(fromUrl);
    const params = new URLSearchParams(location.search);
    params.delete("token");
    const rest = params.toString();
    history.replaceState(null, "", location.pathname + (rest ? "?" + rest : "") + location.hash);
  } else if (readToken()) {
    setCookie(readToken());
  }

  window.BotPortAuth = {
    has: () => !!readToken(),
    set: saveToken,
    clear: clearToken,
  };

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
    if (res.status === 401) window.dispatchEvent(new CustomEvent("botport:unauthorized"));
    return res;
  };
})();
