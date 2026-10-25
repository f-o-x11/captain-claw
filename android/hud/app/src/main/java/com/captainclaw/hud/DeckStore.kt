package com.captainclaw.hud

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * The Flight Decks this phone has opened, and the accounts used on each — what
 * the start screen lists. Plain data (addresses, names, emails) in
 * SharedPreferences; anything secret is in [Vault].
 */
object DeckStore {
    private const val PREFS = "decks"
    private const val KEY_DECKS = "decks"
    /** 0.1/0.2 kept a single deck here; it becomes the first saved one. */
    private const val LEGACY_PREFS = "deck"
    private const val LEGACY_KEY = "origin"

    data class Account(val email: String, val name: String, val role: String, val lastUsed: Long)

    data class Deck(
        val origin: String,
        val name: String,
        /** The deck's logo as Branding sets it ("" = the Captain mark). */
        val logo: String,
        val authEnabled: Boolean,
        val lastUsed: Long,
        val accounts: List<Account>,
    ) {
        /** host[:port], how a deck is told apart from another of the same name. */
        val host: String get() = origin.substringAfter("://")
    }

    /** One row of the start screen: a deck, with one of its accounts or none. */
    data class Entry(val deck: Deck, val account: Account?) {
        val lastUsed: Long get() = account?.lastUsed ?: deck.lastUsed
    }

    fun decks(context: Context): List<Deck> {
        migrate(context)
        val raw = prefs(context).getString(KEY_DECKS, null) ?: return emptyList()
        return try {
            val arr = JSONArray(raw)
            List(arr.length()) { readDeck(arr.getJSONObject(it)) }
        } catch (_: Exception) {
            emptyList()
        }
    }

    fun deck(context: Context, origin: String): Deck? = decks(context).firstOrNull { it.origin == origin }

    /** Every deck and account, the most recently used first. */
    fun entries(context: Context): List<Entry> =
        decks(context).flatMap { d ->
            if (d.accounts.isEmpty()) listOf(Entry(d, null)) else d.accounts.map { Entry(d, it) }
        }.sortedByDescending { it.lastUsed }

    /** Add a deck, or refresh what its /fd/auth/status says. */
    fun saveDeck(context: Context, origin: String, name: String, logo: String, authEnabled: Boolean) {
        edit(context) { list ->
            val old = list.firstOrNull { it.origin == origin }
            list.removeAll { it.origin == origin }
            list += Deck(origin, name, logo, authEnabled, old?.lastUsed ?: now(), old?.accounts.orEmpty())
        }
    }

    /** An account signed in on a deck (through the app or the deck's own page); marks it used. */
    fun saveAccount(context: Context, origin: String, email: String, name: String, role: String) {
        edit(context) { list ->
            val i = list.indexOfFirst { it.origin == origin }
            if (i < 0) return@edit
            val d = list[i]
            val accounts = d.accounts.filterNot { it.email.equals(email, ignoreCase = true) } +
                Account(email, name, role, now())
            list[i] = d.copy(accounts = accounts, lastUsed = now())
        }
    }

    /** The deck (and account) was just opened. */
    fun touch(context: Context, origin: String, email: String?) {
        edit(context) { list ->
            val i = list.indexOfFirst { it.origin == origin }
            if (i < 0) return@edit
            val d = list[i]
            list[i] = d.copy(
                lastUsed = now(),
                accounts = d.accounts.map { if (email != null && it.email.equals(email, ignoreCase = true)) it.copy(lastUsed = now()) else it },
            )
        }
    }

    fun removeAccount(context: Context, origin: String, email: String) {
        edit(context) { list ->
            val i = list.indexOfFirst { it.origin == origin }
            if (i >= 0) list[i] = list[i].copy(accounts = list[i].accounts.filterNot { it.email.equals(email, ignoreCase = true) })
        }
        Vault.remove(context, Vault.sessionId(origin, email))
        Vault.remove(context, Vault.passwordId(origin, email))
    }

    fun removeDeck(context: Context, origin: String) {
        edit(context) { list -> list.removeAll { it.origin == origin } }
        Vault.removeDeck(context, origin)
    }

    private fun edit(context: Context, change: (MutableList<Deck>) -> Unit) {
        val list = decks(context).toMutableList()
        change(list)
        val arr = JSONArray()
        list.forEach { arr.put(writeDeck(it)) }
        prefs(context).edit().putString(KEY_DECKS, arr.toString()).apply()
    }

    private fun migrate(context: Context) {
        val legacy = context.getSharedPreferences(LEGACY_PREFS, Context.MODE_PRIVATE)
        val origin = legacy.getString(LEGACY_KEY, null) ?: return
        legacy.edit().remove(LEGACY_KEY).apply()
        if (prefs(context).getString(KEY_DECKS, null) == null) {
            val deck = Deck(origin, "Flight Deck", "", authEnabled = true, lastUsed = now(), accounts = emptyList())
            prefs(context).edit().putString(KEY_DECKS, JSONArray().put(writeDeck(deck)).toString()).apply()
        }
    }

    private fun readDeck(o: JSONObject): Deck {
        val accounts = o.optJSONArray("accounts") ?: JSONArray()
        return Deck(
            origin = o.getString("origin"),
            name = o.optString("name"),
            logo = o.optString("logo"),
            authEnabled = o.optBoolean("auth", true),
            lastUsed = o.optLong("used"),
            accounts = List(accounts.length()) {
                val a = accounts.getJSONObject(it)
                Account(a.getString("email"), a.optString("name"), a.optString("role"), a.optLong("used"))
            },
        )
    }

    private fun writeDeck(d: Deck): JSONObject = JSONObject()
        .put("origin", d.origin)
        .put("name", d.name)
        .put("logo", d.logo)
        .put("auth", d.authEnabled)
        .put("used", d.lastUsed)
        .put(
            "accounts",
            JSONArray().apply {
                d.accounts.forEach { put(JSONObject().put("email", it.email).put("name", it.name).put("role", it.role).put("used", it.lastUsed)) }
            },
        )

    private fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun now() = System.currentTimeMillis()
}
