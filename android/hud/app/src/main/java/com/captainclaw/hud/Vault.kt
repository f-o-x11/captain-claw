package com.captainclaw.hud

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Secrets the app keeps for its saved accounts — a deck session (the
 * fd_refresh token) and, when the user asks, the password — encrypted with an
 * AES-GCM key that lives in the Android Keystore and never leaves it. The
 * ciphertext sits in SharedPreferences, which backups skip (allowBackup=false).
 *
 * A key the system dropped (a lock-screen reset on some phones) makes every
 * secret unreadable: they are forgotten and the user signs in again.
 */
object Vault {
    private const val PREFS = "vault"
    private const val KEY_ALIAS = "captain-claw-vault"
    private const val GCM_TAG_BITS = 128

    fun sessionId(origin: String, email: String) = "session|$origin|${email.lowercase()}"
    fun passwordId(origin: String, email: String) = "password|$origin|${email.lowercase()}"

    fun put(context: Context, id: String, secret: String) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val sealed = cipher.iv + cipher.doFinal(secret.toByteArray(Charsets.UTF_8))
        prefs(context).edit().putString(id, Base64.encodeToString(sealed, Base64.NO_WRAP)).apply()
    }

    fun get(context: Context, id: String): String? {
        val stored = prefs(context).getString(id, null) ?: return null
        return try {
            val sealed = Base64.decode(stored, Base64.NO_WRAP)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(GCM_TAG_BITS, sealed, 0, IV_BYTES))
            String(cipher.doFinal(sealed, IV_BYTES, sealed.size - IV_BYTES), Charsets.UTF_8)
        } catch (_: Exception) {
            remove(context, id)
            null
        }
    }

    fun has(context: Context, id: String): Boolean = prefs(context).contains(id)

    fun remove(context: Context, id: String) {
        prefs(context).edit().remove(id).apply()
    }

    /** Every secret of a deck (it was removed from the app). */
    fun removeDeck(context: Context, origin: String) {
        val p = prefs(context)
        val gone = p.all.keys.filter { it.split('|').getOrNull(1) == origin }
        p.edit().apply { gone.forEach(::remove) }.apply()
    }

    private fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return generator.generateKey()
    }

    /** GCM's IV, which Cipher picks for AES/GCM in the Keystore: 12 bytes. */
    private const val IV_BYTES = 12
}
