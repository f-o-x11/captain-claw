package com.captainclaw.hud

import android.webkit.CookieManager
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URI
import java.net.URL
import java.net.UnknownHostException
import javax.net.ssl.SSLException

/**
 * Signing in from the start screen. A deck's session is its httpOnly
 * `fd_refresh` cookie (path /fd/auth, 7 days, replaced on every refresh), so
 * the app signs in — or resumes a session it kept — with the same calls the
 * kiosk makes, and puts the cookie it gets into the WebView's jar: the kiosk
 * then opens signed in. Blocking calls.
 */
object Session {
    const val COOKIE = "fd_refresh"
    private const val TIMEOUT_MS = 15_000

    data class User(val email: String, val name: String, val role: String)

    sealed interface Result {
        /** Signed in; [token] is the session now in the cookie jar. */
        data class Ok(val user: User, val token: String) : Result
        /** The deck said no: wrong password, a waitlist, an expired session… */
        data class Refused(val message: String, val code: Int) : Result
        /** The deck couldn't be reached. */
        data class Unreachable(val message: String) : Result
    }

    /** POST /fd/auth/login. */
    fun login(origin: String, email: String, password: String): Result =
        call(origin, "/fd/auth/login", JSONObject().put("email", email.trim()).put("password", password).toString(), cookie = null)

    /** POST /fd/auth/refresh with a kept session; the deck answers with its account and a new session. */
    fun resume(origin: String, token: String): Result = call(origin, "/fd/auth/refresh", "", cookie = "$COOKIE=$token")

    /** The session in the WebView's jar for this deck, if any. */
    fun current(origin: String): String? =
        CookieManager.getInstance().getCookie("$origin/fd/auth/refresh")
            ?.split(';')?.map { it.trim() }
            ?.firstOrNull { it.startsWith("$COOKIE=") }
            ?.substringAfter('=')?.takeIf { it.isNotEmpty() && it != "\"\"" }

    /** Drop the deck's session from the jar (the kiosk then shows its sign-in page). */
    fun clear(origin: String) {
        CookieManager.getInstance().apply {
            setCookie(origin, "$COOKIE=; Max-Age=0; Path=/fd/auth")
            flush()
        }
    }

    private fun call(origin: String, path: String, body: String, cookie: String?): Result {
        val conn = try {
            URL(origin + path).openConnection() as HttpURLConnection
        } catch (_: Exception) {
            return Result.Unreachable("That doesn't look like an address.")
        }
        try {
            conn.connectTimeout = TIMEOUT_MS
            conn.readTimeout = TIMEOUT_MS
            conn.requestMethod = "POST"
            conn.instanceFollowRedirects = false
            conn.doOutput = true
            conn.setRequestProperty("Accept", "application/json")
            conn.setRequestProperty("Content-Type", "application/json")
            cookie?.let { conn.setRequestProperty("Cookie", it) }
            conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            val code = conn.responseCode
            val text = (if (code >= 400) conn.errorStream else conn.inputStream)?.use { s ->
                s.bufferedReader().readText().take(64_000)
            }.orEmpty()
            val json = runCatching { JSONObject(text) }.getOrNull()
            if (code != 200) {
                val detail = json?.optString("detail").orEmpty()
                return Result.Refused(
                    when {
                        code == 401 && path.endsWith("/login") -> "Wrong email or password."
                        code == 401 -> "The saved session has ended."
                        code == 429 -> detail.ifEmpty { "Too many attempts — wait a minute and try again." }
                        detail.isNotEmpty() -> detail
                        else -> "Flight Deck answered HTTP $code."
                    },
                    code,
                )
            }
            val u = json?.optJSONObject("user") ?: return Result.Refused("Flight Deck answered, but not like Flight Deck.", code)
            val token = applyCookies(origin, conn.headerFields.entries.filter { it.key.equals("Set-Cookie", ignoreCase = true) }.flatMap { it.value })
                ?: return Result.Refused("Flight Deck signed you in but sent no session.", code)
            return Result.Ok(User(u.optString("email"), u.optString("display_name"), u.optString("role")), token)
        } catch (e: UnknownHostException) {
            return Result.Unreachable("Can't find ${URI(origin).host} — check the address and your network.")
        } catch (e: SSLException) {
            return Result.Unreachable("$origin has no HTTPS certificate this phone trusts.")
        } catch (e: SocketTimeoutException) {
            return Result.Unreachable("$origin didn't answer in time.")
        } catch (e: IOException) {
            return Result.Unreachable("Can't reach $origin (${e.message ?: e.javaClass.simpleName}).")
        } finally {
            conn.disconnect()
        }
    }

    /** Put the deck's Set-Cookie headers into the WebView's jar; the new session token, if one came. */
    private fun applyCookies(origin: String, headers: List<String>): String? {
        val jar = CookieManager.getInstance()
        var token: String? = null
        for (h in headers) {
            jar.setCookie(origin, h)
            val pair = h.substringBefore(';').trim()
            if (pair.startsWith("$COOKIE=")) token = pair.substringAfter('=').takeIf { it.isNotEmpty() && it != "\"\"" }
        }
        jar.flush()
        return token
    }
}
