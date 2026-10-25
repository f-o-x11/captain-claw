package com.captainclaw.hud

import android.app.AlertDialog
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.KeyEvent
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.ProgressBar
import android.widget.Switch
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.SystemBarStyle
import androidx.activity.enableEdgeToEdge
import androidx.core.content.pm.ShortcutInfoCompat
import androidx.core.content.pm.ShortcutManagerCompat
import androidx.core.graphics.drawable.IconCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.isVisible
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * The start screen: the Flight Decks this phone has opened, and the accounts
 * used on each, drawn like the kiosk's agent list. Tapping one opens its kiosk
 * (MainActivity) signed in: with the session the app kept for that account,
 * else its saved password, else a sign-in form here. "Add a Flight Deck" takes
 * an address with or without http(s)://.
 *
 * Launcher shortcuts (long-press the icon) open the recently used ones.
 */
class DecksActivity : ComponentActivity() {
    private enum class Page { LIST, ADD, SIGN_IN }

    private var page = Page.LIST
    private var busy = false
    /** The deck the sign-in page is for. */
    private var signInDeck: DeckStore.Deck? = null

    private lateinit var back: ImageButton
    private lateinit var headerCaption: TextView
    private lateinit var pageList: View
    private lateinit var pageAdd: View
    private lateinit var pageSignIn: View
    private lateinit var listLabel: TextView
    private lateinit var listStatus: TextView
    private lateinit var list: LinearLayout
    private lateinit var listNote: View
    private lateinit var addButton: View
    private lateinit var addTitle: TextView
    private lateinit var address: EditText
    private lateinit var addStatus: TextView
    private lateinit var addContinue: View
    private lateinit var addContinueLabel: TextView
    private lateinit var signInAvatar: FrameLayout
    private lateinit var signInDeckName: TextView
    private lateinit var signInHost: TextView
    private lateinit var signInText: TextView
    private lateinit var email: EditText
    private lateinit var password: EditText
    private lateinit var passwordToggle: ImageButton
    private lateinit var remember: Switch
    private lateinit var signInStatus: TextView
    private lateinit var signIn: View
    private lateinit var signInLabel: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
        )
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_decks)
        ViewCompat.setOnApplyWindowInsetsListener(findViewById(R.id.decks_root)) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            v.setPadding(bars.left, bars.top, bars.right, maxOf(bars.bottom, ime.bottom))
            WindowInsetsCompat.CONSUMED
        }

        back = findViewById(R.id.back)
        headerCaption = findViewById(R.id.header_caption)
        pageList = findViewById(R.id.page_list)
        pageAdd = findViewById(R.id.page_add)
        pageSignIn = findViewById(R.id.page_sign_in)
        listLabel = findViewById(R.id.list_label)
        listStatus = findViewById(R.id.list_status)
        list = findViewById(R.id.list)
        listNote = findViewById(R.id.list_note)
        addButton = findViewById(R.id.add)
        addTitle = findViewById(R.id.add_title)
        address = findViewById(R.id.address)
        addStatus = findViewById(R.id.add_status)
        addContinue = findViewById(R.id.add_continue)
        addContinueLabel = findViewById(R.id.add_continue_label)
        signInAvatar = findViewById(R.id.sign_in_avatar)
        signInDeckName = findViewById(R.id.sign_in_deck)
        signInHost = findViewById(R.id.sign_in_host)
        signInText = findViewById(R.id.sign_in_text)
        email = findViewById(R.id.email)
        password = findViewById(R.id.password)
        passwordToggle = findViewById(R.id.password_toggle)
        remember = findViewById(R.id.remember)
        signInStatus = findViewById(R.id.sign_in_status)
        signIn = findViewById(R.id.sign_in)
        signInLabel = findViewById(R.id.sign_in_label)

        // A password field defaults to monospace: the kiosk's font, as in the email field.
        password.typeface = Typeface.DEFAULT
        findViewById<ImageView>(R.id.brand_logo).apply {
            setBackgroundResource(R.drawable.bg_avatar)
            clipToOutline = true
        }

        back.setOnClickListener { leavePage() }
        addButton.setOnClickListener { showAdd() }
        addContinue.setOnClickListener { addDeck() }
        address.onGo { addDeck() }
        signIn.setOnClickListener { signIn() }
        password.onGo { signIn() }
        passwordToggle.setOnClickListener { togglePassword() }
        findViewById<View>(R.id.remember_row).setOnClickListener { remember.toggle() }
        findViewById<View>(R.id.sign_in_web).setOnClickListener { signInOnPage() }
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() = leavePage()
        })

        if (DeckStore.decks(this).isEmpty()) showAdd() else showList()
        if (savedInstanceState == null) openFromShortcut(intent)
        refreshDecks()
    }

    /** What each deck says about itself now (Branding's name and logo, sign-in on or off). */
    private fun refreshDecks() {
        lifecycleScope.launch {
            var changed = false
            for (d in DeckStore.decks(this@DecksActivity)) {
                val r = withContext(Dispatchers.IO) { Deck.check(d.origin) } as? Deck.Check.Ok ?: continue
                if (r.origin != d.origin || (r.name == d.name && r.logo == d.logo && r.authEnabled == d.authEnabled)) continue
                DeckStore.saveDeck(this@DecksActivity, d.origin, r.name, r.logo, r.authEnabled)
                changed = true
            }
            if (changed && page == Page.LIST && !busy) renderList()
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        openFromShortcut(intent)
    }

    override fun onResume() {
        super.onResume()
        // Back from a kiosk: its session (the green dot) and the order changed.
        if (page == Page.LIST && !busy) renderList()
    }

    // ── Pages ──

    private fun show(p: Page) {
        page = p
        pageList.isVisible = p == Page.LIST
        pageAdd.isVisible = p == Page.ADD
        pageSignIn.isVisible = p == Page.SIGN_IN
        addButton.isVisible = p == Page.LIST
        back.isVisible = p != Page.LIST && DeckStore.decks(this).isNotEmpty()
        headerCaption.text = when (p) {
            Page.LIST -> getString(R.string.decks_caption)
            Page.ADD -> getString(R.string.add_title)
            Page.SIGN_IN -> signInDeck?.host.orEmpty()
        }
        if (p == Page.LIST) hideKeyboard()
    }

    private fun leavePage() {
        if (busy) return
        if (page != Page.LIST && DeckStore.decks(this).isNotEmpty()) showList() else finish()
    }

    private fun showList() {
        show(Page.LIST)
        renderList()
    }

    private fun renderList() {
        val decks = DeckStore.decks(this)
        val entries = DeckStore.entries(this)
        listLabel.text = getString(R.string.decks_label, decks.size)
        list.removeAllViews()
        entries.forEachIndexed { i, e -> list.addView(row(e, lastUsed = i == 0 && entries.size > 1)) }
        listNote.isVisible = entries.isNotEmpty()
    }

    private fun row(entry: DeckStore.Entry, lastUsed: Boolean): View {
        val deck = entry.deck
        val account = entry.account
        val v = LayoutInflater.from(this).inflate(R.layout.item_deck, list, false)
        if (lastUsed) v.setBackgroundResource(R.drawable.bg_card_current)
        bindAvatar(v.findViewById(R.id.avatar), deck)
        v.findViewById<TextView>(R.id.title).text = deck.name
        v.findViewById<View>(R.id.chip).isVisible = lastUsed
        v.findViewById<TextView>(R.id.detail).text = when {
            account == null && deck.authEnabled -> getString(R.string.signs_in_on_page)
            account == null -> getString(R.string.no_sign_in)
            account.role == "admin" -> getString(R.string.role_admin, account.email)
            else -> account.email
        }
        v.findViewById<TextView>(R.id.host).text = deck.host
        v.findViewById<View>(R.id.key).isVisible = account != null && Vault.has(this, Vault.passwordId(deck.origin, account.email))
        v.findViewById<View>(R.id.dot).isVisible = account != null && Vault.has(this, Vault.sessionId(deck.origin, account.email))
        v.setOnClickListener { enter(deck, account, v) }
        v.findViewById<View>(R.id.more).setOnClickListener { menu(it, deck, account) }
        return v
    }

    private fun showListStatus(text: String?) {
        listStatus.text = text.orEmpty()
        listStatus.isVisible = !text.isNullOrEmpty()
    }

    private fun showAdd() {
        val first = DeckStore.decks(this).isEmpty()
        addTitle.setText(if (first) R.string.add_first_title else R.string.add_title)
        address.setText("")
        setStatus(addStatus, null)
        show(Page.ADD)
        focus(address)
    }

    private fun showSignIn(deck: DeckStore.Deck, knownEmail: String?, message: String?) {
        signInDeck = deck
        bindAvatar(signInAvatar, deck)
        signInDeckName.text = deck.name
        signInHost.text = deck.host
        signInText.text = getString(R.string.sign_in_text, deck.name)
        email.setText(knownEmail.orEmpty())
        password.setText("")
        remember.isChecked = true
        setStatus(signInStatus, message, error = true)
        show(Page.SIGN_IN)
        focus(if (knownEmail.isNullOrEmpty()) email else password)
    }

    // ── Opening a deck ──

    private sealed interface Entered {
        data class Ok(val user: Session.User, val token: String) : Entered
        data class NeedsPassword(val message: String?) : Entered
        data class Unreachable(val message: String) : Entered
    }

    /** Tap on a card: open it signed in as that account, by the quickest way there is. */
    private fun enter(deck: DeckStore.Deck, account: DeckStore.Account?, row: View?) {
        if (busy) return
        showListStatus(null)
        if (!deck.authEnabled || account == null) {
            open(deck, null)
            return
        }
        setBusy(true, row)
        lifecycleScope.launch {
            val r = withContext(Dispatchers.IO) { resume(deck, account.email) }
            setBusy(false, row)
            when (r) {
                is Entered.Ok -> open(deck, r.user.email)
                is Entered.NeedsPassword -> showSignIn(deck, account.email, r.message)
                is Entered.Unreachable -> showListStatus(r.message)
            }
        }
    }

    /** The account's kept session, else its saved password. Blocking. */
    private fun resume(deck: DeckStore.Deck, email: String): Entered {
        val origin = deck.origin
        Vault.get(this, Vault.sessionId(origin, email))?.let { token ->
            when (val r = Session.resume(origin, token)) {
                is Session.Result.Ok -> {
                    keep(origin, r.user, r.token, password = null)
                    if (r.user.email.equals(email, ignoreCase = true)) return Entered.Ok(r.user, r.token)
                    // The session kept for it was someone else's (they signed in on the page): kept for them now.
                    Vault.remove(this, Vault.sessionId(origin, email))
                }
                is Session.Result.Refused -> Vault.remove(this, Vault.sessionId(origin, email))
                is Session.Result.Unreachable -> return Entered.Unreachable(r.message)
            }
        }
        val saved = Vault.get(this, Vault.passwordId(origin, email)) ?: return Entered.NeedsPassword(null)
        return when (val r = Session.login(origin, email, saved)) {
            is Session.Result.Ok -> {
                keep(origin, r.user, r.token, password = null)
                Entered.Ok(r.user, r.token)
            }
            is Session.Result.Refused -> {
                if (r.code == 401) Vault.remove(this, Vault.passwordId(origin, email))
                Entered.NeedsPassword(if (r.code == 401) getString(R.string.sign_in_saved_failed) else r.message)
            }
            is Session.Result.Unreachable -> Entered.Unreachable(r.message)
        }
    }

    /** A signed-in account: on the list, its session (and the password, when asked) in the vault. */
    private fun keep(origin: String, user: Session.User, token: String, password: String?) {
        DeckStore.saveAccount(this, origin, user.email, user.name, user.role)
        Vault.put(this, Vault.sessionId(origin, user.email), token)
        if (password != null) Vault.put(this, Vault.passwordId(origin, user.email), password)
    }

    private fun open(deck: DeckStore.Deck, accountEmail: String?) {
        DeckStore.touch(this, deck.origin, accountEmail)
        pushShortcut(deck, accountEmail)
        startActivity(
            Intent(this, MainActivity::class.java)
                .putExtra(MainActivity.EXTRA_ORIGIN, deck.origin)
                .putExtra(MainActivity.EXTRA_EMAIL, accountEmail),
        )
        // Back from the kiosk lands on the list.
        if (page != Page.LIST) show(Page.LIST)
    }

    // ── Add a deck ──

    private fun addDeck() {
        if (busy) return
        val input = address.text.toString()
        setBusy(true, null)
        addContinueLabel.setText(R.string.checking)
        setStatus(addStatus, null)
        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) { Deck.check(input) }
            setBusy(false, null)
            addContinueLabel.setText(R.string.add_continue)
            when (result) {
                is Deck.Check.Ok -> {
                    DeckStore.saveDeck(this@DecksActivity, result.origin, result.name, result.logo, result.authEnabled)
                    val deck = DeckStore.deck(this@DecksActivity, result.origin) ?: return@launch
                    if (deck.authEnabled) showSignIn(deck, null, null) else open(deck, null)
                }
                is Deck.Check.Failed -> setStatus(addStatus, result.message, error = true)
            }
        }
    }

    // ── Sign in ──

    private fun signIn() {
        val deck = signInDeck ?: return
        if (busy) return
        val who = email.text.toString().trim()
        val secret = password.text.toString()
        if (who.isEmpty() || secret.isEmpty()) {
            setStatus(signInStatus, getString(R.string.email_needed), error = true)
            return
        }
        val keepPassword = remember.isChecked
        setBusy(true, null)
        signInLabel.setText(R.string.signing_in)
        setStatus(signInStatus, null)
        lifecycleScope.launch {
            val r = withContext(Dispatchers.IO) { Session.login(deck.origin, who, secret) }
            setBusy(false, null)
            signInLabel.setText(R.string.sign_in)
            when (r) {
                is Session.Result.Ok -> {
                    keep(deck.origin, r.user, r.token, if (keepPassword) secret else null)
                    if (!keepPassword) Vault.remove(this@DecksActivity, Vault.passwordId(deck.origin, r.user.email))
                    password.setText("")
                    open(deck, r.user.email)
                }
                is Session.Result.Refused -> setStatus(signInStatus, r.message, error = true)
                is Session.Result.Unreachable -> setStatus(signInStatus, r.message, error = true)
            }
        }
    }

    /** The deck's own page signs in (an invitation, the waitlist…): with no session in the jar. */
    private fun signInOnPage() {
        val deck = signInDeck ?: return
        Session.clear(deck.origin)
        open(deck, null)
    }

    private fun togglePassword() {
        val shown = password.inputType and InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD != 0
        val at = password.selectionEnd
        password.inputType = InputType.TYPE_CLASS_TEXT or
            if (shown) InputType.TYPE_TEXT_VARIATION_PASSWORD else InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
        password.typeface = Typeface.DEFAULT
        password.setSelection(at.coerceIn(0, password.length()))
        passwordToggle.setImageResource(if (shown) R.drawable.ic_eye else R.drawable.ic_eye_off)
        passwordToggle.contentDescription = getString(if (shown) R.string.show_password else R.string.hide_password)
    }

    // ── A card's ⋯ ──

    private fun menu(anchor: View, deck: DeckStore.Deck, account: DeckStore.Account?) {
        if (busy) return
        val popup = PopupMenu(this, anchor, Gravity.END)
        val m = popup.menu
        if (deck.authEnabled) m.add(0, 1, 0, if (account == null) R.string.menu_sign_in else R.string.menu_other_account)
        if (account != null && Vault.has(this, Vault.passwordId(deck.origin, account.email))) m.add(0, 2, 0, R.string.menu_forget_password)
        if (account != null) m.add(0, 3, 0, R.string.menu_remove_account)
        m.add(0, 4, 0, getString(R.string.menu_remove_deck, deck.name))
        popup.setOnMenuItemClickListener { item ->
            when (item.itemId) {
                1 -> showSignIn(deck, null, null)
                2 -> {
                    Vault.remove(this, Vault.passwordId(deck.origin, account!!.email))
                    renderList()
                }
                3 -> confirm(getString(R.string.remove_account_title, account!!.email), getString(R.string.remove_account_text)) {
                    DeckStore.removeAccount(this, deck.origin, account.email)
                    ShortcutManagerCompat.removeDynamicShortcuts(this, listOf(shortcutId(deck.origin, account.email)))
                    renderList()
                }
                4 -> confirm(getString(R.string.remove_deck_title, deck.name), getString(R.string.remove_deck_text)) {
                    DeckStore.removeDeck(this, deck.origin)
                    Session.clear(deck.origin)
                    ShortcutManagerCompat.removeDynamicShortcuts(
                        this,
                        listOf(shortcutId(deck.origin, null)) + deck.accounts.map { shortcutId(deck.origin, it.email) },
                    )
                    if (DeckStore.decks(this).isEmpty()) showAdd() else renderList()
                }
            }
            true
        }
        popup.show()
    }

    private fun confirm(title: String, text: String, then: () -> Unit) {
        AlertDialog.Builder(this)
            .setTitle(title)
            .setMessage(text)
            .setPositiveButton(R.string.remove) { _, _ -> then() }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    // ── Launcher shortcuts: the recently used decks ──

    private fun shortcutId(origin: String, email: String?) = "$origin|${email.orEmpty().lowercase()}"

    private fun pushShortcut(deck: DeckStore.Deck, email: String?) {
        val intent = Intent(this, DecksActivity::class.java).setAction(Intent.ACTION_VIEW)
            .putExtra(MainActivity.EXTRA_ORIGIN, deck.origin)
        if (email != null) intent.putExtra(MainActivity.EXTRA_EMAIL, email)
        val shortcut = ShortcutInfoCompat.Builder(this, shortcutId(deck.origin, email))
            // Short: who (two accounts of one deck must differ); long: who on which deck.
            .setShortLabel(email?.substringBefore('@') ?: deck.name)
            .setLongLabel("${deck.name} — ${email ?: deck.host}")
            .setIcon(IconCompat.createWithResource(this, R.mipmap.ic_launcher))
            .setIntent(intent)
            .build()
        runCatching { ShortcutManagerCompat.pushDynamicShortcut(this, shortcut) }
    }

    private fun openFromShortcut(intent: Intent?) {
        val origin = intent?.getStringExtra(MainActivity.EXTRA_ORIGIN) ?: return
        val email = intent.getStringExtra(MainActivity.EXTRA_EMAIL)
        setIntent(Intent(this, DecksActivity::class.java))
        val deck = DeckStore.deck(this, origin) ?: return
        val account = deck.accounts.firstOrNull { it.email.equals(email, ignoreCase = true) }
        showList()
        enter(deck, account, null)
    }

    // ── Bits ──

    private fun bindAvatar(box: FrameLayout, deck: DeckStore.Deck) {
        box.removeAllViews()
        val img = ImageView(this).apply {
            setBackgroundResource(R.drawable.bg_avatar)
            clipToOutline = true
            scaleType = ImageView.ScaleType.CENTER_CROP
        }
        box.addView(img, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        if (deck.logo.isEmpty()) {
            img.setImageResource(R.drawable.ic_launcher_art)
            return
        }
        DeckLogos.cached(deck)?.let {
            img.setImageBitmap(it)
            return
        }
        val initials = TextView(this).apply {
            text = initials(deck.name)
            gravity = Gravity.CENTER
            setTextColor(getColor(R.color.text_soft))
            textSize = 12f
            typeface = Typeface.DEFAULT_BOLD
        }
        box.addView(initials, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        lifecycleScope.launch {
            val bitmap = withContext(Dispatchers.IO) { DeckLogos.load(this@DecksActivity, deck) } ?: return@launch
            img.setImageBitmap(bitmap)
            box.removeView(initials)
        }
    }

    /** As the kiosk's avatars: the first letters of the first two words. */
    private fun initials(name: String): String {
        val words = name.trim().split(Regex("\\s+")).filter { it.isNotEmpty() }
        return when {
            words.isEmpty() -> "FD"
            words.size == 1 -> words[0].take(2).uppercase()
            else -> (words[0].take(1) + words[1].take(1)).uppercase()
        }
    }

    private fun setBusy(on: Boolean, row: View?) {
        busy = on
        addContinue.isEnabled = !on
        signIn.isEnabled = !on
        row?.findViewById<View>(R.id.more)?.visibility = if (on) View.INVISIBLE else View.VISIBLE
        row?.findViewById<ProgressBar>(R.id.busy)?.isVisible = on
        row?.alpha = if (on) 0.85f else 1f
    }

    private fun setStatus(view: TextView, text: String?, error: Boolean = false) {
        view.text = text.orEmpty()
        view.setTextColor(getColor(if (error) R.color.error else R.color.text_dim))
        view.isVisible = !text.isNullOrEmpty()
    }

    private fun focus(field: EditText) {
        field.requestFocus()
        field.post { WindowCompat.getInsetsController(window, field).show(WindowInsetsCompat.Type.ime()) }
    }

    private fun hideKeyboard() {
        WindowCompat.getInsetsController(window, window.decorView).hide(WindowInsetsCompat.Type.ime())
    }

    private fun EditText.onGo(action: () -> Unit) {
        setOnEditorActionListener { _, id, event ->
            val enter = event?.keyCode == KeyEvent.KEYCODE_ENTER && event.action == KeyEvent.ACTION_DOWN
            if (id == EditorInfo.IME_ACTION_GO || enter) {
                action()
                true
            } else {
                false
            }
        }
    }
}
