package com.captainclaw.hud

import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.graphics.Bitmap
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.os.Message
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.MimeTypeMap
import android.webkit.RenderProcessGoneDetail
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.FrameLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.SystemBarStyle
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.isVisible
import androidx.lifecycle.lifecycleScope
import androidx.webkit.JavaScriptReplyProxy
import androidx.webkit.ScriptHandler
import androidx.webkit.WebMessageCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * The kiosk: one WebView on the deck's own address. Flight Deck knows the app
 * by its user-agent token and shows it the users' kiosk — an admin, the kiosk
 * or the Admin console (flight-deck/src/services/desktopShell.ts isAppShell).
 * Everything the page does is same-origin with the deck (relative /fd/...
 * calls, the chat WebSockets, the httpOnly fd_refresh cookie), so the app adds
 * no auth of its own — it keeps that cookie, keeps navigation on the deck,
 * handles Back, the window insets, file picking, downloads and sign-in popups,
 * and lends the page a speech engine.
 *
 * None of the kiosk ships in the app: every start (a new activity) loads it
 * from the deck, no-cache, and coming back to the app reloads it if the deck
 * has deployed a different build since — a deck update never needs a new APK.
 *
 * The start screen (DecksActivity) opens it for one saved deck and account,
 * signed in. Leaving it keeps that account's latest session (the deck replaces
 * it on every refresh) for the next switch back; signing out in the kiosk, or
 * its Settings → Switch deck, returns to the start screen.
 */
class MainActivity : ComponentActivity() {
    private lateinit var root: View
    private lateinit var webView: WebView
    private lateinit var errorPanel: View
    private lateinit var errorText: TextView
    private lateinit var popupPanel: View
    private lateinit var popupHolder: FrameLayout
    private lateinit var popupTitle: TextView
    private lateinit var origin: String
    /** The account the kiosk is signed in as, when known (DecksActivity, or the kiosk's report). */
    private var account: String? = null
    private var popup: WebView? = null
    /** The sign-in window has shown a page of its own (not only handed out a file). */
    private var popupShowedPage = false
    private var speech: Speech? = null
    private var downloads: Downloads? = null
    private var replyProxy: JavaScriptReplyProxy? = null
    private val scripts = mutableListOf<ScriptHandler>()
    /** The /assets/… script of the kiosk build on screen (to spot a new deploy). */
    private var shownBuild: String? = null
    private var resumedOnce = false
    /** The page's light theme (system-bar icons go dark on it). */
    private var lightPage = false
    private var fileCallback: ValueCallback<Array<Uri>>? = null

    private val pickFiles = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        fileCallback?.onReceiveValue(if (result.resultCode == RESULT_OK) pickedUris(result.data) else null)
        fileCallback = null
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        styleBars(light = false)
        super.onCreate(savedInstanceState)
        val opened = intent.getStringExtra(EXTRA_ORIGIN)?.takeIf { DeckStore.deck(this, it) != null }
        if (opened == null) {
            startActivity(Intent(this, DecksActivity::class.java))
            finish()
            return
        }
        origin = opened
        account = intent.getStringExtra(EXTRA_EMAIL)
        setContentView(R.layout.activity_main)
        root = findViewById(R.id.root)
        webView = findViewById(R.id.web)
        errorPanel = findViewById(R.id.error_panel)
        errorText = findViewById(R.id.error_text)
        popupPanel = findViewById(R.id.popup_panel)
        popupHolder = findViewById(R.id.popup_holder)
        popupTitle = findViewById(R.id.popup_title)
        findViewById<Button>(R.id.error_retry).setOnClickListener { loadKiosk() }
        findViewById<Button>(R.id.error_settings).setOnClickListener { openDecks() }
        findViewById<View>(R.id.popup_close).setOnClickListener { closePopup() }

        // Edge-to-edge (targetSdk 35+): pad the content by the system bars,
        // cutout and keyboard, so the kiosk's composer sits above the IME
        // (adjustResize alone no longer resizes the window).
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            v.setPadding(bars.left, bars.top, bars.right, maxOf(bars.bottom, ime.bottom))
            WindowInsetsCompat.CONSUMED
        }

        if (0 != applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) WebView.setWebContentsDebuggingEnabled(true)
        downloads = Downloads(this, origin)
        setUpWebView()

        // Back (predictive back too — onBackPressed is never called at
        // targetSdk 36): a sign-in window, the kiosk's open dialog, its
        // previous screen, else close the app, so the next launch is a start
        // that loads the kiosk afresh (see goBack).
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() = goBack()
        })

        loadKiosk()
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configure(view: WebView) {
        view.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            cacheMode = WebSettings.LOAD_DEFAULT
            // window.open (a sign-in, a PDF's "View") gets its own window —
            // onCreateWindow — instead of replacing the kiosk.
            setSupportMultipleWindows(true)
            allowFileAccess = false
            allowContentAccess = false
            builtInZoomControls = false
            userAgentString = "$userAgentString $UA_TOKEN/${BuildConfig.VERSION_NAME}"
        }
        view.setDownloadListener { url, userAgent, contentDisposition, mimetype, _ ->
            downloads?.onDownloadStart(url, userAgent, contentDisposition, mimetype)
            // A window opened only for the file (a PDF's "View") has nothing to show.
            if (view === popup && !popupShowedPage) closePopup()
        }
    }

    private fun setUpWebView() {
        configure(webView)
        CookieManager.getInstance().apply {
            setAcceptCookie(true)
            setAcceptThirdPartyCookies(webView, false)
        }
        webView.webChromeClient = KioskChrome()
        webView.webViewClient = KioskClient()

        // The bridge object, for the deck's origin only (never
        // addJavascriptInterface, which every frame of every origin would see).
        val rules = setOf(origin)
        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            WebViewCompat.addWebMessageListener(webView, BRIDGE, rules) { _, message, sourceOrigin, isMainFrame, proxy ->
                if (isMainFrame && sourceOrigin.toString().trimEnd('/') == origin) onBridgeMessage(message, proxy)
            }
            if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
                for (script in listOf(Speech.SHIM, Downloads.HOOK, THEME_WATCH)) {
                    scripts += WebViewCompat.addDocumentStartJavaScript(webView, script, rules)
                }
            }
        }
    }

    private fun onBridgeMessage(message: WebMessageCompat, proxy: JavaScriptReplyProxy) {
        val msg = try {
            JSONObject(message.data ?: return)
        } catch (_: Exception) {
            return
        }
        when (msg.optString("t")) {
            "settings" -> openDecks()
            "session" -> onSessionReport(msg)
            "signed-out" -> onSignedOut()
            "dl" -> downloads?.handle(msg)
            "theme" -> onPageTheme(msg)
            "tts" -> {
                replyProxy = proxy
                val s = speech ?: Speech(this) { json -> replyProxy?.postMessage(json) }.also { speech = it }
                s.handle(msg)
            }
        }
    }

    /** The kiosk's light / dark theme: the system bars and the edges follow it. */
    private fun onPageTheme(msg: JSONObject) {
        val bg = try {
            Color.parseColor(msg.optString("bg"))
        } catch (_: IllegalArgumentException) {
            return
        }
        root.setBackgroundColor(bg)
        webView.setBackgroundColor(bg)
        val light = msg.optBoolean("light")
        if (light != lightPage) styleBars(light)
    }

    private fun styleBars(light: Boolean) {
        lightPage = light
        val style = if (light) {
            SystemBarStyle.light(Color.TRANSPARENT, Color.TRANSPARENT)
        } else {
            SystemBarStyle.dark(Color.TRANSPARENT)
        }
        enableEdgeToEdge(statusBarStyle = style, navigationBarStyle = style)
    }

    /**
     * Back: a sign-in window closes (or goes back); then the page — its open
     * dialog gets Escape, as a keyboard would close it, or history.back()
     * while `navigation.canGoBack` (WebView.canGoBack() alone would skip the
     * entries the kiosk pushes without a tap: Chromium's history
     * intervention). Then webView.goBack() when it can, else finish.
     */
    private fun goBack() {
        popup?.let { p ->
            if (p.canGoBack()) p.goBack() else closePopup()
            return
        }
        askPage(PAGE_BACK) { result ->
            when {
                result.startsWith("esc:") -> {
                    // Did the dialog close? If Escape did nothing, step back past it.
                    val before = result.removePrefix("esc:").toIntOrNull() ?: 0
                    webView.postDelayed({
                        askPage(DIALOG_COUNT) { now ->
                            if ((now.toIntOrNull() ?: 0) >= before) askPage(PAGE_HISTORY_BACK) { r -> if (r != "back") nativeBack() }
                        }
                    }, 250)
                }
                result != "back" -> nativeBack()
            }
        }
    }

    private fun nativeBack() {
        if (webView.canGoBack()) webView.goBack() else finish()
    }

    /** Run a script that returns a string; "" when the page doesn't answer in time. */
    private fun askPage(script: String, then: (String) -> Unit) {
        var answered = false
        val timeout = Runnable {
            if (!answered) {
                answered = true
                then("")
            }
        }
        webView.evaluateJavascript(script) { raw ->
            if (answered) return@evaluateJavascript
            answered = true
            webView.removeCallbacks(timeout)
            then(raw?.trim('"').orEmpty())
        }
        webView.postDelayed(timeout, 600)
    }

    /** Load the deck's current kiosk from the network (never a cached page). */
    private fun loadKiosk() {
        errorPanel.isVisible = false
        closePopup()
        speech?.cancel()
        lifecycleScope.launch {
            // Clear text only while the deck is still on the local network.
            if (!withContext(Dispatchers.IO) { Deck.plainHttpAllowed(origin) }) {
                showError(Deck.PLAIN_HTTP_REFUSED)
                return@launch
            }
            webView.loadUrl("$origin/", mapOf("Cache-Control" to "no-cache", "Pragma" to "no-cache"))
        }
    }

    /** Note which kiosk build the page on screen runs. */
    private fun noteShownBuild(view: WebView) {
        view.evaluateJavascript(
            "(function(){var s=document.querySelector('script[type=\"module\"][src]');return s?s.getAttribute('src'):''})()",
        ) { result ->
            val src = result?.trim('"').orEmpty()
            shownBuild = src.takeIf { it.startsWith("/assets/") }
        }
    }

    /** The kiosk build the deck serves now: its /assets/… entry script, or null. Blocking. */
    private fun deckBuild(): String? = try {
        val conn = URL("$origin/").openConnection() as HttpURLConnection
        try {
            conn.connectTimeout = 5_000
            conn.readTimeout = 5_000
            conn.setRequestProperty("Cache-Control", "no-cache")
            if (conn.responseCode != 200) {
                null
            } else {
                val html = conn.inputStream.use { it.bufferedReader().readText().take(64_000) }
                Regex("""<script[^>]+src="(/assets/[^"]+\.js)"""").find(html)?.groupValues?.get(1)
            }
        } finally {
            conn.disconnect()
        }
    } catch (_: Exception) {
        null
    }

    override fun onResume() {
        super.onResume()
        if (!::webView.isInitialized) return
        if (!resumedOnce) {
            resumedOnce = true
            return
        }
        // Back from the background: pick up a kiosk the deck deployed meanwhile.
        // Not while a sign-in window or the file picker is open: that would
        // pull the page out from under it.
        lifecycleScope.launch {
            val now = withContext(Dispatchers.IO) { deckBuild() } ?: return@launch
            if (shownBuild != null && now != shownBuild && !errorPanel.isVisible && popup == null && fileCallback == null) {
                shownBuild = now
                webView.reload()
            }
        }
    }

    override fun onPause() {
        super.onPause()
        // The httpOnly fd_refresh cookie is the session: get it to disk now.
        CookieManager.getInstance().flush()
        if (::webView.isInitialized) keepSession()
    }

    /** The account's session as it is now, for switching back to it from the start screen. */
    private fun keepSession() {
        val who = account ?: return
        val token = Session.current(origin)
        if (token != null) Vault.put(this, Vault.sessionId(origin, who), token) else Vault.remove(this, Vault.sessionId(origin, who))
    }

    /** The kiosk says who is signed in (a sign-in on its own page included). */
    private fun onSessionReport(msg: JSONObject) {
        val email = msg.optString("email").ifEmpty { return }
        DeckStore.saveAccount(this, origin, email, msg.optString("name"), msg.optString("role"))
        account = email
    }

    /** Signed out in the kiosk (or its session ended): its session is gone; back to the start screen. */
    private fun onSignedOut() {
        account?.let { Vault.remove(this, Vault.sessionId(origin, it)) }
        account = null
        openDecks()
    }

    override fun onDestroy() {
        speech?.shutdown()
        speech = null
        fileCallback?.onReceiveValue(null)
        fileCallback = null
        if (::webView.isInitialized) {
            closePopup()
            scripts.forEach { it.remove() }
            (webView.parent as? ViewGroup)?.removeView(webView)
            webView.destroy()
        }
        downloads?.shutdown()
        downloads = null
        super.onDestroy()
    }

    private fun openDecks() {
        startActivity(Intent(this, DecksActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP))
        finish()
    }

    private fun showError(text: String) {
        errorText.text = text
        errorPanel.isVisible = true
    }

    private fun isDeck(uri: Uri): Boolean {
        val u = uri.toString()
        return u == origin || u.startsWith("$origin/") || u.startsWith("$origin?") || u.startsWith("$origin#")
    }

    /** Hand an address to whatever opens it (the browser, the mail app…). */
    private fun openOutside(uri: Uri) {
        if (uri.scheme !in setOf("http", "https", "mailto", "tel", "geo")) return
        try {
            startActivity(Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE))
        } catch (_: ActivityNotFoundException) {
            // nothing can open it
        }
    }

    // ── Windows the page opens ──

    /**
     * A window the page opened (window.open, a target=_blank link). Its first
     * real address decides: the deck's (a Google / Microsoft / MCP sign-in
     * starts at the deck's /fd/…/login) keeps it here, over the kiosk, for the
     * whole sign-in; anything else goes to the browser and the window closes.
     */
    private fun openPopup(resultMsg: Message): Boolean {
        closePopup()
        val w = WebView(this)
        configure(w)
        CookieManager.getInstance().setAcceptThirdPartyCookies(w, true) // identity providers
        w.webChromeClient = object : WebChromeClient() {
            override fun onCloseWindow(window: WebView) = closePopup()
            override fun onReceivedTitle(view: WebView, title: String?) {
                popupTitle.text = title.orEmpty()
            }
        }
        w.webViewClient = PopupClient()
        popupHolder.addView(w, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        popup = w
        popupShowedPage = false
        popupTitle.text = ""
        (resultMsg.obj as WebView.WebViewTransport).webView = w
        resultMsg.sendToTarget()
        return true
    }

    private fun closePopup() {
        val w = popup ?: return
        popup = null
        popupPanel.isVisible = false
        popupHolder.removeView(w)
        w.stopLoading()
        w.destroy()
    }

    private inner class PopupClient : WebViewClient() {
        private var decided = false

        /** The first real address: true to keep the window. */
        private fun decide(view: WebView, uri: Uri): Boolean {
            decided = true
            if (isDeck(uri)) {
                popupPanel.isVisible = true
                return true
            }
            openOutside(uri)
            view.post { if (view === popup) closePopup() }
            return false
        }

        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val uri = request.url
            if (uri.scheme == "about" || uri.scheme == "blob" || uri.scheme == "data") return false
            if (!decided) return !decide(view, uri)
            // Google refuses to sign anyone in inside an app's WebView: say so
            // rather than show its error page.
            if (uri.host == "accounts.google.com") {
                Toast.makeText(this@MainActivity, R.string.google_in_browser, Toast.LENGTH_LONG).show()
                view.post { if (view === popup) closePopup() }
                return true
            }
            // In a sign-in: the providers' pages stay; other apps' links leave.
            if (uri.scheme == "http" || uri.scheme == "https") return false
            openOutside(uri)
            return true
        }

        override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
            val uri = Uri.parse(url)
            if (!decided && uri.scheme != "about") {
                if (!decide(view, uri)) view.stopLoading()
            }
        }

        override fun onPageCommitVisible(view: WebView, url: String) {
            if (view === popup && !url.startsWith("about:")) popupShowedPage = true
        }
    }

    // ── The kiosk's own window ──

    private inner class KioskChrome : WebChromeClient() {
        override fun onCreateWindow(view: WebView, isDialog: Boolean, isUserGesture: Boolean, resultMsg: Message): Boolean =
            openPopup(resultMsg)

        /** An <input type=file> (chat attachments, uploads): the system picker. */
        override fun onShowFileChooser(
            view: WebView,
            callback: ValueCallback<Array<Uri>>,
            params: FileChooserParams,
        ): Boolean {
            fileCallback?.onReceiveValue(null)
            fileCallback = callback
            return try {
                pickFiles.launch(fileChooserIntent(params))
                true
            } catch (_: ActivityNotFoundException) {
                fileCallback = null
                false
            }
        }
    }

    private fun fileChooserIntent(params: WebChromeClient.FileChooserParams): Intent {
        // accept="image/*,.pdf": the types it names, when each one maps to a
        // MIME type; anything it can't map means any file.
        val accepted = params.acceptTypes.orEmpty().flatMap { it.split(',') }.map { it.trim().lowercase() }.filter { it.isNotEmpty() }
        val mimes = accepted.map { t ->
            when {
                t.contains('/') -> t
                t.startsWith('.') -> MimeTypeMap.getSingleton().getMimeTypeFromExtension(t.drop(1))
                else -> null
            }
        }
        val types = if (mimes.isEmpty() || mimes.any { it == null }) emptyList() else mimes.filterNotNull().distinct()
        return Intent(Intent.ACTION_GET_CONTENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = types.singleOrNull() ?: "*/*"
            if (types.size > 1) putExtra(Intent.EXTRA_MIME_TYPES, types.toTypedArray())
            if (params.mode == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE) putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
        }
    }

    private fun pickedUris(data: Intent?): Array<Uri>? {
        data ?: return null
        val clip = data.clipData
        val uris = if (clip != null) List(clip.itemCount) { clip.getItemAt(it).uri } else listOfNotNull(data.data)
        return uris.toTypedArray().takeIf { it.isNotEmpty() }
    }

    private inner class KioskClient : WebViewClient() {
        /** Pages of the deck stay here; anything else opens in the browser. */
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val uri = request.url
            if (isDeck(uri)) return false
            if (uri.scheme == "about" || uri.scheme == "blob" || uri.scheme == "data") return false
            if (request.isForMainFrame) openOutside(uri)
            return true
        }

        override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
            speech?.cancel()
        }

        override fun onPageFinished(view: WebView, url: String) {
            if (isDeck(Uri.parse(url))) noteShownBuild(view)
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (!request.isForMainFrame) return
            showError("Can't reach Flight Deck at $origin\n(${error.description})")
        }

        override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
            if (!request.isForMainFrame) return
            val code = response.statusCode
            showError(
                if (code == 403 || code == 400) {
                    "Flight Deck at $origin refused this address (HTTP $code). A deck reached through its own " +
                        "domain needs that name in FD_ALLOWED_HOSTS or FD_PUBLIC_URL."
                } else {
                    "Flight Deck at $origin answered HTTP $code."
                },
            )
        }

        override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
            // The page's renderer died (killed for memory): start over cleanly.
            recreate()
            return true
        }
    }

    companion object {
        /**
         * User-agent token Flight Deck knows the app by
         * (flight-deck/src/services/desktopShell.ts, src/hud/device.ts). It
         * keeps the name the app started with, when it showed the HUD.
         */
        const val UA_TOKEN = "CaptainClawHUD"
        /** The page-side name of the bridge object (desktopShell.ts, device.ts, the scripts here). */
        const val BRIDGE = "CaptainClawHUD"
        /** The deck to open (its origin) and, when known, the account signed in there. */
        const val EXTRA_ORIGIN = "origin"
        const val EXTRA_EMAIL = "email"

        private const val DIALOGS = "document.querySelectorAll('[role=\"dialog\"],[aria-modal=\"true\"]').length"

        /**
         * The page's Back: Escape to its open dialog ("esc:<dialogs open>"),
         * else history.back() when it has somewhere to go ("back"), else "".
         */
        private val PAGE_BACK = """
            (function(){try{
              var n=$DIALOGS;
              if(n){var t=document.activeElement&&document.activeElement!==document.body?document.activeElement:document;
                t.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',code:'Escape',keyCode:27,which:27,bubbles:true,cancelable:true}));
                return 'esc:'+n}
              if(window.navigation&&navigation.canGoBack){history.back();return 'back'}
            }catch(e){}return ''})()
        """.trimIndent()

        private val DIALOG_COUNT = "(function(){try{return String($DIALOGS)}catch(e){return ''}})()"

        private const val PAGE_HISTORY_BACK =
            "(function(){try{if(window.navigation&&navigation.canGoBack){history.back();return 'back'}}catch(e){}return ''})()"

        /**
         * Runs at document start in the deck's pages: reports the page's
         * background and whether its light theme is on (<html class="light">),
         * now and whenever the theme changes.
         */
        private val THEME_WATCH = """
            (function () {
              'use strict';
              var bridge = window.$BRIDGE;
              if (!bridge) return;
              var last = '';
              function hex(color) {
                try {
                  var c = document.createElement('canvas');
                  c.width = c.height = 1;
                  var g = c.getContext('2d');
                  g.fillStyle = color;
                  g.fillRect(0, 0, 1, 1);
                  var d = g.getImageData(0, 0, 1, 1).data;
                  if (d[3] === 0) return '';
                  return '#' + [d[0], d[1], d[2]].map(function (v) { return (v < 16 ? '0' : '') + v.toString(16); }).join('');
                } catch (e) { return ''; }
              }
              function report() {
                if (!document.body) return;
                var bg = hex(getComputedStyle(document.body).backgroundColor);
                if (!bg) return;
                var light = document.documentElement.classList.contains('light');
                var key = bg + light;
                if (key === last) return;
                last = key;
                try { bridge.postMessage(JSON.stringify({ t: 'theme', bg: bg, light: light })); } catch (e) {}
              }
              new MutationObserver(function () { setTimeout(report, 0); })
                .observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style'] });
              document.addEventListener('DOMContentLoaded', report);
              window.addEventListener('load', report);
            })();
        """.trimIndent()
    }
}
