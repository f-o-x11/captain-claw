package com.captainclaw.hud

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Base64
import android.util.LruCache
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/**
 * A deck's logo (Admin → Settings → Branding) for its card on the start
 * screen: a data: URL, a path on the deck or an address. Kept on disk, so the
 * list draws without the network; a deck with no logo shows the Captain mark.
 */
object DeckLogos {
    private const val MAX_BYTES = 2_000_000
    private const val SIZE_PX = 160
    private val memory = LruCache<String, Bitmap>(24)

    fun cached(deck: DeckStore.Deck): Bitmap? = memory.get(key(deck))

    /** The logo, or null when there is none or it can't be had. Blocking. */
    fun load(context: Context, deck: DeckStore.Deck): Bitmap? {
        if (deck.logo.isEmpty()) return null
        val key = key(deck)
        memory.get(key)?.let { return it }
        val file = File(File(context.cacheDir, "logos"), "$key.img")
        val bytes = if (file.isFile) file.readBytes() else fetch(deck)?.also {
            file.parentFile?.mkdirs()
            file.writeBytes(it)
        }
        val bitmap = bytes?.let(::decode) ?: return null
        memory.put(key, bitmap)
        return bitmap
    }

    private fun fetch(deck: DeckStore.Deck): ByteArray? {
        val logo = deck.logo
        if (logo.startsWith("data:")) {
            val comma = logo.indexOf(',')
            if (comma < 0 || !logo.substring(0, comma).endsWith(";base64")) return null
            return runCatching { Base64.decode(logo.substring(comma + 1), Base64.DEFAULT) }.getOrNull()
        }
        val url = when {
            logo.startsWith("/") -> deck.origin + logo
            logo.startsWith("https://") || logo.startsWith("http://") -> logo
            else -> return null
        }
        return try {
            val conn = URL(url).openConnection() as HttpURLConnection
            try {
                conn.connectTimeout = 8_000
                conn.readTimeout = 8_000
                if (conn.responseCode != 200) null
                else conn.inputStream.use { s -> s.readBytes().takeIf { it.size <= MAX_BYTES } }
            } finally {
                conn.disconnect()
            }
        } catch (_: Exception) {
            null
        }
    }

    private fun decode(bytes: ByteArray): Bitmap? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        if (bounds.outWidth <= 0) return null
        var sample = 1
        while (bounds.outWidth / (sample * 2) >= SIZE_PX && bounds.outHeight / (sample * 2) >= SIZE_PX) sample *= 2
        return BitmapFactory.decodeByteArray(bytes, 0, bytes.size, BitmapFactory.Options().apply { inSampleSize = sample })
    }

    private fun key(deck: DeckStore.Deck): String =
        MessageDigest.getInstance("SHA-1").digest("${deck.origin}\n${deck.logo}".toByteArray())
            .joinToString("") { "%02x".format(it) }
}
