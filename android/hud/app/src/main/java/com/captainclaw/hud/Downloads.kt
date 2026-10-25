package com.captainclaw.hud

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.ContentValues
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.util.Base64
import android.webkit.CookieManager
import android.webkit.MimeTypeMap
import android.webkit.URLUtil
import android.widget.Toast
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLDecoder
import java.util.concurrent.Executors

/**
 * Files the kiosk hands out. A WebView saves nothing itself — it reports a
 * download (onDownloadStart) and drops it — so:
 *
 * - a deck URL (the kiosk's /download and /view routes carry their fd_token)
 *   is fetched again here with the WebView's cookies and user agent: an
 *   attachment goes to Downloads, anything else (a PDF's "View") opens in an
 *   app that shows it;
 * - a blob: or data: URL (a file the page built itself) can't be fetched from
 *   outside the page, so [HOOK] — injected for the deck's origin only — sends
 *   its bytes over the bridge, in chunks;
 * - any other address opens in the browser.
 *
 * Page → app:  {"t":"dl","op":"start","id","name","mime"} · {"t":"dl","op":"chunk","id","data":<base64>}
 *              · {"t":"dl","op":"end","id"} · {"t":"dl","op":"abort","id"}
 */
class Downloads(private val activity: Activity, private val origin: String) {
    /** One thread, so a file's chunks are written in the order they came. */
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    /** Files the page is still sending (io thread only). */
    private val incoming = HashMap<String, Target>()

    /** WebView.setDownloadListener, main thread. */
    fun onDownloadStart(url: String, userAgent: String, contentDisposition: String?, mimeType: String?) {
        val uri = Uri.parse(url)
        when {
            url == origin || url.startsWith("$origin/") -> io.execute { fetch(url, userAgent, contentDisposition, mimeType) }
            uri.scheme == "blob" || uri.scheme == "data" -> toast(activity.getString(R.string.download_failed_page))
            uri.scheme == "http" || uri.scheme == "https" -> try {
                activity.startActivity(Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE))
            } catch (_: ActivityNotFoundException) {
                toast(activity.getString(R.string.download_failed_page))
            }
        }
    }

    /** A {"t":"dl"} message from the page (main thread). */
    fun handle(msg: JSONObject) {
        val id = msg.optString("id").ifEmpty { return }
        when (msg.optString("op")) {
            "start" -> {
                val name = safeName(msg.optString("name"), msg.optString("mime"))
                val mime = msg.optString("mime").ifEmpty { mimeFor(name) }
                io.execute {
                    incoming.remove(id)?.discard()
                    try {
                        incoming[id] = Target.download(activity, name, mime)
                    } catch (e: IOException) {
                        toast(activity.getString(R.string.download_failed, name, e.message ?: ""))
                    }
                }
            }
            "chunk" -> {
                val data = msg.optString("data")
                io.execute {
                    val t = incoming[id] ?: return@execute
                    try {
                        t.out.write(Base64.decode(data, Base64.DEFAULT))
                    } catch (e: Exception) {
                        incoming.remove(id)
                        t.discard()
                        toast(activity.getString(R.string.download_failed, t.name, e.message ?: ""))
                    }
                }
            }
            "end" -> io.execute {
                val t = incoming.remove(id) ?: return@execute
                finishDownload(t)
            }
            "abort" -> io.execute { incoming.remove(id)?.discard() }
        }
    }

    fun shutdown() {
        io.execute { incoming.values.forEach { it.discard() }; incoming.clear() }
        io.shutdown()
    }

    // ── io thread ──

    private fun fetch(url: String, userAgent: String, disposition: String?, mimeType: String?) {
        val conn = try {
            URL(url).openConnection() as HttpURLConnection
        } catch (_: Exception) {
            return toast(activity.getString(R.string.download_failed_page))
        }
        var target: Target? = null
        try {
            conn.connectTimeout = 15_000
            conn.readTimeout = 60_000
            conn.setRequestProperty("User-Agent", userAgent)
            CookieManager.getInstance().getCookie(url)?.let { conn.setRequestProperty("Cookie", it) }
            val code = conn.responseCode
            if (code != 200) return toast(activity.getString(R.string.download_failed_http, code))
            val cd = conn.getHeaderField("Content-Disposition") ?: disposition
            val mime = (conn.contentType ?: mimeType)?.substringBefore(';')?.trim()?.lowercase()
                ?.takeIf { it.isNotEmpty() && it != "application/octet-stream" }
            val name = safeName(fileName(cd) ?: URLUtil.guessFileName(url, cd, mime), mime)
            val type = mime ?: mimeFor(name)
            val save = cd?.trim()?.lowercase()?.startsWith("attachment") == true
            val t = if (save) Target.download(activity, name, type) else Target.view(activity, name, type)
            target = t
            conn.inputStream.use { it.copyTo(t.out) }
            target = null
            if (save) finishDownload(t) else finishView(t)
        } catch (e: IOException) {
            target?.discard()
            toast(activity.getString(R.string.download_failed_any, e.message ?: e.javaClass.simpleName))
        } finally {
            conn.disconnect()
        }
    }

    private fun finishDownload(t: Target) {
        try {
            t.commit()
        } catch (e: IOException) {
            t.discard()
            return toast(activity.getString(R.string.download_failed, t.name, e.message ?: ""))
        }
        if (t.public) {
            toast(activity.getString(R.string.download_saved, t.name))
        } else {
            // Before Android 10 it lands in the app's own folder: open it, so it
            // isn't lost there.
            main.post { if (!openFile(t)) toast(activity.getString(R.string.download_saved_app, t.name)) }
        }
    }

    private fun finishView(t: Target) {
        try {
            t.commit()
        } catch (e: IOException) {
            t.discard()
            return toast(activity.getString(R.string.download_failed, t.name, e.message ?: ""))
        }
        main.post {
            if (!openFile(t)) {
                // Nothing here opens it: keep it in Downloads instead.
                io.execute {
                    try {
                        val d = Target.download(activity, t.name, t.mime)
                        activity.contentResolver.openInputStream(t.uri)?.use { it.copyTo(d.out) }
                        finishDownload(d)
                    } catch (e: IOException) {
                        toast(activity.getString(R.string.download_failed, t.name, e.message ?: ""))
                    }
                }
            }
        }
    }

    // ── main thread ──

    private fun openFile(t: Target): Boolean = !activity.isDestroyed && try {
        activity.startActivity(
            Intent(Intent.ACTION_VIEW)
                .setDataAndType(t.uri, t.mime)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION),
        )
        true
    } catch (_: ActivityNotFoundException) {
        false
    }

    private fun toast(text: String) {
        main.post { Toast.makeText(activity.applicationContext, text, Toast.LENGTH_LONG).show() }
    }

    /** Where a file is written: [out], then [commit] or [discard]. */
    private class Target(
        val name: String,
        val mime: String,
        val uri: Uri,
        val out: OutputStream,
        /** In the public Downloads folder (Android 10+), where Files finds it. */
        val public: Boolean,
        private val onCommit: () -> Unit,
        private val onDiscard: () -> Unit,
    ) {
        fun commit() {
            out.close()
            onCommit()
        }

        fun discard() {
            try {
                out.close()
            } catch (_: IOException) {
            }
            onDiscard()
        }

        companion object {
            /** A new file in Downloads (before Android 10: the app's own Download folder). */
            fun download(activity: Activity, name: String, mime: String): Target {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    val resolver = activity.contentResolver
                    val values = ContentValues().apply {
                        put(MediaStore.MediaColumns.DISPLAY_NAME, name)
                        put(MediaStore.MediaColumns.MIME_TYPE, mime)
                        put(MediaStore.MediaColumns.IS_PENDING, 1)
                    }
                    val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                        ?: throw IOException("Downloads refused the file")
                    val out = resolver.openOutputStream(uri) ?: run {
                        resolver.delete(uri, null, null)
                        throw IOException("Downloads refused the file")
                    }
                    return Target(
                        name, mime, uri, out, public = true,
                        onCommit = {
                            resolver.update(uri, ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }, null, null)
                        },
                        onDiscard = { resolver.delete(uri, null, null) },
                    )
                }
                val dir = activity.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: File(activity.filesDir, "Download")
                return inFolder(activity, dir, name, mime, public = false)
            }

            /** A file to open in another app; the cache keeps only the latest few. */
            fun view(activity: Activity, name: String, mime: String): Target {
                val dir = File(activity.cacheDir, "view")
                dir.listFiles()?.sortedByDescending { it.lastModified() }?.drop(VIEW_KEEP)?.forEach { it.delete() }
                return inFolder(activity, dir, name, mime, public = false)
            }

            private fun inFolder(activity: Activity, dir: File, name: String, mime: String, public: Boolean): Target {
                if (!dir.isDirectory && !dir.mkdirs()) throw IOException("Can't write to ${dir.path}")
                val file = unique(dir, name)
                val uri = FileProvider.getUriForFile(activity, "${activity.packageName}.files", file)
                return Target(name, mime, uri, file.outputStream(), public, onCommit = {}, onDiscard = { file.delete() })
            }

            private fun unique(dir: File, name: String): File {
                var file = File(dir, name)
                val stem = name.substringBeforeLast('.', name)
                val ext = name.substringAfterLast('.', "").let { if (it.isEmpty()) "" else ".$it" }
                var n = 1
                while (file.exists()) file = File(dir, "$stem (${n++})$ext")
                return file
            }

            private const val VIEW_KEEP = 8
        }
    }

    companion object {
        /** The filename in a Content-Disposition header (RFC 6266: filename* first). */
        fun fileName(disposition: String?): String? {
            disposition ?: return null
            Regex("""filename\*\s*=\s*([^']*)'[^']*'([^;]+)""", RegexOption.IGNORE_CASE).find(disposition)?.let { m ->
                val charset = m.groupValues[1].ifBlank { "UTF-8" }
                runCatching { URLDecoder.decode(m.groupValues[2].trim().replace("+", "%2B"), charset) }.getOrNull()
                    ?.takeIf { it.isNotBlank() }?.let { return it }
            }
            Regex("""filename\s*=\s*("((?:[^"\\]|\\.)*)"|[^;]+)""", RegexOption.IGNORE_CASE).find(disposition)?.let { m ->
                val name = if (m.groupValues[2].isNotEmpty()) m.groupValues[2].replace(Regex("""\\(.)"""), "$1") else m.groupValues[1].trim()
                if (name.isNotBlank()) return name
            }
            return null
        }

        /** A name that is safe as a file name, with an extension when the type gives one. */
        fun safeName(raw: String, mime: String?): String {
            var name = raw.substringAfterLast('/').substringAfterLast('\\')
                .replace(Regex("""[\u0000-\u001f\u007f:*?"<>|]"""), "_").trim().trimStart('.')
            if (name.isEmpty()) name = "download"
            if (!name.contains('.')) {
                mime?.let { MimeTypeMap.getSingleton().getExtensionFromMimeType(it) }?.let { name = "$name.$it" }
            }
            return name.take(120)
        }

        fun mimeFor(name: String): String =
            MimeTypeMap.getSingleton().getMimeTypeFromExtension(name.substringAfterLast('.', "").lowercase())
                ?: "application/octet-stream"

        /**
         * Runs at document start in the deck's pages: a download link to a
         * blob: or data: URL (an <a download> the page clicks, or the user
         * does) sends the file's bytes to the app instead. The page usually
         * revokes a blob URL right after the click, so the blobs behind recent
         * URLs are kept a while (the latest 20).
         */
        val HOOK = """
            (function () {
              'use strict';
              var bridge = window.${MainActivity.BRIDGE};
              if (!bridge || window.__captainClawDownloads) return;
              window.__captainClawDownloads = true;
              var blobs = new Map();
              var create = URL.createObjectURL;
              URL.createObjectURL = function (obj) {
                var url = create.apply(URL, arguments);
                if (obj instanceof Blob) {
                  blobs.set(url, obj);
                  if (blobs.size > 20) blobs.delete(blobs.keys().next().value);
                }
                return url;
              };
              var seq = 0;
              function post(m) { bridge.postMessage(JSON.stringify(m)); }
              function send(blob, name) {
                var id = 'd' + (++seq) + '-' + Date.now();
                var CHUNK = 786432;
                var off = 0;
                try { post({ t: 'dl', op: 'start', id: id, name: name || '', mime: blob.type || '' }); } catch (e) { return; }
                (function next() {
                  if (off >= blob.size) { post({ t: 'dl', op: 'end', id: id }); return; }
                  var r = new FileReader();
                  r.onload = function () {
                    var s = String(r.result);
                    post({ t: 'dl', op: 'chunk', id: id, data: s.slice(s.indexOf(',') + 1) });
                    next();
                  };
                  r.onerror = function () { post({ t: 'dl', op: 'abort', id: id }); };
                  r.readAsDataURL(blob.slice(off, off + CHUNK));
                  off += CHUNK;
                })();
              }
              function take(a) {
                if (!a || !a.hasAttribute || !a.hasAttribute('download')) return false;
                var href = a.href || '';
                var name = a.getAttribute('download') || '';
                if (href.indexOf('blob:') === 0) {
                  var b = blobs.get(href);
                  if (!b) return false;
                  send(b, name);
                  return true;
                }
                if (href.indexOf('data:') === 0) {
                  fetch(href).then(function (r) { return r.blob(); }).then(function (b) { send(b, name); });
                  return true;
                }
                return false;
              }
              var click = HTMLAnchorElement.prototype.click;
              HTMLAnchorElement.prototype.click = function () { if (!take(this)) click.call(this); };
              document.addEventListener('click', function (e) {
                var a = e.target && e.target.closest ? e.target.closest('a[download]') : null;
                if (a && take(a)) e.preventDefault();
              }, true);
            })();
        """.trimIndent()
    }
}
