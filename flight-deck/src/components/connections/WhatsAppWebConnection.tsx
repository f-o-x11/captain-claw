import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { Smartphone, Loader2, ChevronDown, ChevronRight, Check, AlertCircle, RefreshCw } from 'lucide-react'
import {
  getMyWhatsAppWeb,
  linkWhatsAppWeb,
  whatsAppWebPairCode,
  reconnectWhatsAppWeb,
  cancelWhatsAppWeb,
  unlinkWhatsAppWeb,
  getWhatsAppWebAdmin,
  setWhatsAppWebAllowed,
  setWhatsAppWebRetention,
  getWhatsAppWebRecent,
  type MyWhatsAppWeb,
  type WhatsAppWebMessage,
  type WhatsAppWebAdmin,
  type WhatsAppWebState,
} from '../../services/whatsappWeb'

// WhatsApp Web (captain_claw/flight_deck/whatsapp_web/): the user's own
// WhatsApp account linked as a device — scan a QR, or type a pairing code on
// the phone — held by Flight Deck's WhatsApp Web service. Off until an admin
// allows it on the deck; every new link needs the user's consent, because this
// is an unofficial client. The history the phone sends after linking and every
// message since are kept (encrypted) for the user's agents, who read them with
// the whatsapp_web tool. Nothing is ever sent, marked read or shown as "online"
// in the user's name.

type Tone = 'on' | 'wait' | 'warn' | 'off'

const STATE: Record<WhatsAppWebState, { text: string; tone: Tone }> = {
  off: { text: 'Not linked', tone: 'off' },
  pairing: { text: 'Waiting for your phone', tone: 'wait' },
  connecting: { text: 'Connecting', tone: 'wait' },
  connected: { text: 'Linked', tone: 'on' },
  disconnected: { text: 'Reconnecting', tone: 'wait' },
  expired: { text: 'Code expired', tone: 'off' },
  failed: { text: 'Couldn’t link', tone: 'warn' },
  logged_out: { text: 'Unlinked on the phone', tone: 'warn' },
  stream_replaced: { text: 'Taken over elsewhere', tone: 'warn' },
  temporary_ban: { text: 'Temporarily banned', tone: 'warn' },
  outdated: { text: 'Needs an update', tone: 'warn' },
}

const PILL: Record<Tone, string> = {
  on: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-500 border-emerald-500/30',
  wait: 'bg-sky-500/15 text-sky-600 dark:text-sky-400 border-sky-500/30',
  warn: 'bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30',
  off: 'bg-zinc-800 text-zinc-400 border-zinc-700',
}

const BOX = 'rounded-md border border-zinc-800 bg-zinc-950/50 px-3 py-2.5 text-xs text-zinc-300'
const BUTTON = 'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs disabled:opacity-50'
const PRIMARY = `${BUTTON} bg-violet-600 text-white hover:bg-violet-500`
const SECONDARY = `${BUTTON} bg-zinc-800 text-zinc-200 hover:bg-zinc-700`

// States the user can start a fresh link from (after consenting again).
const STARTABLE: WhatsAppWebState[] = ['off', 'expired', 'failed', 'logged_out']
// States where the card asks the service every couple of seconds: the QR
// rotates and the state moves on by itself.
const LIVE: WhatsAppWebState[] = ['pairing', 'connecting', 'disconnected']

function when(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function day(ts: number | null | undefined): string {
  if (!ts) return ''
  try { return new Date(ts * 1000).toLocaleDateString() } catch { return '' }
}

function stamp(ts: number): string {
  try { return new Date(ts * 1000).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' }) } catch { return '' }
}

const KIND_LABEL: Record<string, string> = {
  image: 'image', video: 'video', audio: 'audio', voice: 'voice note', document: 'document', sticker: 'sticker',
  video_note: 'video note', location: 'location', contact: 'contact', contacts: 'contacts', poll: 'poll', event: 'event',
}

function AdminSwitch({ onChange }: { onChange: () => void }) {
  const [admin, setAdmin] = useState<WhatsAppWebAdmin | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => { getWhatsAppWebAdmin().then(setAdmin).catch((e) => setError(String(e?.message ?? e))) }, [])

  if (!admin) return error ? <div className="text-xs text-red-600 dark:text-red-400">{error}</div> : null
  const toggle = async () => {
    if (!admin.allowed && !confirm(
      'Allow WhatsApp Web on this deck? People can then link their own WhatsApp accounts. ' +
      'It is an unofficial client: it goes against WhatsApp\'s terms and accounts do get banned.')) return
    if (admin.allowed && !confirm('Turn WhatsApp Web off? Every link on this deck is paused (kept on disk) until it\'s on again.')) return
    setBusy(true)
    setError('')
    try {
      setAdmin(await setWhatsAppWebAllowed(!admin.allowed))
      onChange()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  const links = admin.service ? Object.values(admin.service.links).reduce((a, b) => a + b, 0) : 0
  return (
    <div className={`${BOX} space-y-1.5`}>
      <div className="flex items-center gap-2">
        <div className="font-medium text-zinc-100">For everyone on this deck</div>
        <button onClick={toggle} disabled={busy || admin.kill_switch} className={`ml-auto ${admin.allowed ? SECONDARY : PRIMARY}`}>
          {busy && <Loader2 className="h-3 w-3 animate-spin" />}
          {admin.allowed ? 'Turn off' : 'Allow WhatsApp Web'}
        </button>
      </div>
      <div className="text-zinc-400">
        {admin.kill_switch
          ? 'Switched off for this deck by FD_WHATSAPP_WEB=off.'
          : !admin.installed
            ? <>Not installed here: <span className="font-mono text-zinc-200">pip install &apos;captain-claw[whatsapp-web]&apos;</span>, then restart Flight Deck.</>
            : admin.allowed
              ? `Allowed. ${admin.service ? `Service running (neonize ${admin.service.neonize || '?'}), ${links} link${links === 1 ? '' : 's'}.` : 'The service starts when someone links.'}`
              : 'Off. Nobody on this deck can link WhatsApp until you allow it.'}
      </div>
      {admin.service && !admin.service.available && (
        <div className="flex items-start gap-1.5 text-amber-600 dark:text-amber-400">
          <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-px" />
          {admin.service.reason}
        </div>
      )}
      {error && <div className="text-red-600 dark:text-red-400">{error}</div>}
    </div>
  )
}

export default function WhatsAppWebConnection() {
  const [view, setView] = useState<MyWhatsAppWeb | null>(null)
  const [collapsed, setCollapsed] = useState(false)
  const [consent, setConsent] = useState(false)
  const [phone, setPhone] = useState('')
  const [byPhone, setByPhone] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [recent, setRecent] = useState<WhatsAppWebMessage[] | null>(null)

  const load = useCallback(async () => {
    try {
      setView(await getMyWhatsAppWeb())
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => { load() }, [load])

  const state: WhatsAppWebState = view?.link.state ?? 'off'
  const live = LIVE.includes(state)
  useEffect(() => {
    if (!live) return
    const t = window.setInterval(() => { getMyWhatsAppWeb().then(setView).catch(() => {}) }, 2000)
    return () => window.clearInterval(t)
  }, [live])

  const run = async (key: string, fn: () => Promise<MyWhatsAppWeb>) => {
    setBusy(key)
    setError('')
    setNotice('')
    try {
      const v = await fn()
      setView(v)
      if (key === 'unlink') {
        setConsent(false)
        setByPhone(false)
        setNotice(v.removed_from_phone
          ? 'Disconnected. “Captain Claw” is gone from your phone’s Linked devices.'
          : 'Disconnected here. Also remove “Captain Claw” on your phone: WhatsApp → Settings → Linked devices.')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const link = view?.link
  const label = STATE[state] ?? STATE.off
  const spin = (key: string) => busy === key && <Loader2 className="h-3 w-3 animate-spin" />
  const disconnect = (
    <button
      onClick={() => {
        if (confirm('Disconnect WhatsApp? The device is removed from your phone’s Linked devices and everything Flight Deck kept for it is deleted.')) {
          run('unlink', unlinkWhatsAppWeb)
        }
      }}
      disabled={!!busy}
      className={`${SECONDARY} hover:text-red-400`}
    >
      {spin('unlink')} Disconnect
    </button>
  )

  let body: ReactNode = null
  if (view && !view.available) {
    body = (
      <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2.5 text-xs text-zinc-300">
        {view.reason}
      </div>
    )
  } else if (view && link && STARTABLE.includes(state)) {
    body = (
      <>
        {state !== 'off' && link.detail && (
          <div className="flex items-start gap-1.5 text-xs text-amber-600 dark:text-amber-400">
            <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-px" />
            {link.detail}
          </div>
        )}
        <div className={`${BOX} space-y-2.5`}>
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={consent}
              onChange={(e) => setConsent(e.target.checked)}
              className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-violet-600"
            />
            <span>
              I understand that this links my WhatsApp to Flight Deck as a device, the way WhatsApp Web
              does, through an <span className="text-zinc-100">unofficial client</span>: that goes against
              WhatsApp’s terms, and WhatsApp bans some accounts that do it. Flight Deck keeps a copy of my
              chats — mine and other people’s messages — encrypted on this deck, my agents read them, and
              what they read goes to the AI model they use. Disconnecting removes the device and deletes
              everything kept.
            </span>
          </label>
          <div className="flex flex-wrap gap-2">
            <button onClick={() => run('link', linkWhatsAppWeb)} disabled={!!busy || !consent} className={PRIMARY}>
              {spin('link')} {state === 'off' ? 'Link WhatsApp' : 'Link again'}
            </button>
            {state !== 'off' && (
              <button onClick={() => run('cancel', cancelWhatsAppWeb)} disabled={!!busy} className={SECONDARY}>
                {spin('cancel')} Dismiss
              </button>
            )}
          </div>
        </div>
      </>
    )
  } else if (view && link && state === 'pairing') {
    body = (
      <div className={`${BOX} space-y-3`}>
        {!byPhone ? (
          <div className="flex flex-col sm:flex-row gap-4">
            <div className="h-56 w-56 shrink-0 rounded-md bg-white p-2 flex items-center justify-center">
              {link.qr_svg
                ? <img src={link.qr_svg} alt="WhatsApp pairing QR code" className="h-full w-full [image-rendering:pixelated]" />
                : <Loader2 className="h-6 w-6 animate-spin text-zinc-500" />}
            </div>
            <ol className="list-decimal pl-4 space-y-1 text-zinc-400">
              <li>Open WhatsApp on your phone.</li>
              <li>Go to <span className="text-zinc-200">Settings → Linked devices → Link a device</span>.</li>
              <li>Point the phone at this code. It changes every 20 seconds or so; keep this page open.</li>
            </ol>
          </div>
        ) : (
          <div className="space-y-2">
            <form
              onSubmit={(e) => { e.preventDefault(); run('code', () => whatsAppWebPairCode(phone)) }}
              className="flex flex-wrap items-center gap-2"
            >
              <input
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                placeholder="+385 97 123 4567"
                inputMode="tel"
                autoComplete="tel"
                className="w-48 rounded-md border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs text-zinc-200 focus:border-violet-500/50 focus:outline-none"
              />
              <button type="submit" disabled={!!busy || !phone.trim()} className={PRIMARY}>
                {spin('code')} Get code
              </button>
            </form>
            {link.pair_code && (
              <>
                <div className="font-mono text-2xl tracking-[0.2em] text-zinc-100">{link.pair_code}</div>
                <div className="text-zinc-400">
                  On your phone: <span className="text-zinc-200">Settings → Linked devices → Link a device →
                  Link with phone number instead</span>, then type this code.
                </div>
              </>
            )}
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <button onClick={() => setByPhone((v) => !v)} className="text-violet-500 hover:underline">
            {byPhone ? 'Scan a QR code instead' : 'Link with a phone number instead'}
          </button>
          {link.pairing_expires_at && (
            <span className="text-zinc-500">Expires {when(link.pairing_expires_at)}</span>
          )}
          <button onClick={() => run('cancel', cancelWhatsAppWeb)} disabled={!!busy} className={`ml-auto ${SECONDARY}`}>
            {spin('cancel')} Cancel
          </button>
        </div>
      </div>
    )
  } else if (view && link) {
    const sync = link.sync
    body = (
      <>
        <div className={`${BOX} space-y-1.5`}>
          <div className="flex items-center gap-2">
            <span className="text-base font-medium text-zinc-100">{link.number || link.jid || 'Your account'}</span>
            {state === 'connecting' && <Loader2 className="h-3.5 w-3.5 animate-spin text-zinc-500" />}
          </div>
          {link.linked_at && <div className="text-zinc-400">Linked {when(link.linked_at)}.</div>}
          {link.detail && (
            <div className="flex items-start gap-1.5 text-amber-600 dark:text-amber-400">
              <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-px" />
              {link.detail}
            </div>
          )}
          {sync && sync.chunks > 0 && (
            <div className="text-zinc-400">
              History from your phone: {sync.messages.toLocaleString()} messages in {sync.chunks} part
              {sync.chunks === 1 ? '' : 's'}{sync.progress ? ` (${sync.progress}%)` : ''}.
            </div>
          )}
          {link.stats && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-zinc-400">
              <span>
                {link.stats.messages.toLocaleString()} message{link.stats.messages === 1 ? '' : 's'} kept in{' '}
                {link.stats.chats} chat{link.stats.chats === 1 ? '' : 's'}
                {link.stats.oldest ? `, since ${day(link.stats.oldest)}` : ''}
              </span>
              <label className="flex items-center gap-1.5">
                Keep
                <select
                  value={link.retention_days ?? ''}
                  disabled={!!busy}
                  onChange={(e) => {
                    const days = e.target.value ? Number(e.target.value) : null
                    if (days && !confirm(`Keep messages for ${days === 365 ? 'a year' : `${days} days`}? Older ones are deleted now.`)) return
                    run('retention', () => setWhatsAppWebRetention(days))
                  }}
                  className="rounded-md border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs text-zinc-200 focus:border-violet-500/50 focus:outline-none disabled:opacity-50"
                >
                  <option value="">everything</option>
                  {(link.retention_choices ?? [7, 30, 90, 365]).map((d) => (
                    <option key={d} value={d}>{d === 365 ? '1 year' : `${d} days`}</option>
                  ))}
                </select>
              </label>
            </div>
          )}
        </div>
        {link.stats && link.stats.messages > 0 && (
          <div>
            <button
              onClick={async () => {
                if (recent) { setRecent(null); return }
                try { setRecent((await getWhatsAppWebRecent()).messages) } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
              }}
              className="flex items-center gap-1 text-xs text-violet-500 hover:underline"
            >
              <ChevronRight className={`h-3 w-3 transition-transform ${recent ? 'rotate-90' : ''}`} />
              What your agents see
            </button>
            {recent && (
              <div className="mt-2 max-h-72 overflow-y-auto rounded-md border border-zinc-800 bg-zinc-950 divide-y divide-zinc-800/60">
                {recent.map((m) => (
                  <div key={`${m.chat_jid}:${m.id}`} className="px-3 py-1.5 text-xs">
                    <div className="text-zinc-500">{stamp(m.ts)} · {m.chat} · {m.from_me ? 'you' : m.sender}</div>
                    <div className="text-zinc-300 whitespace-pre-wrap break-words">
                      {m.deleted ? <span className="italic text-zinc-500">deleted</span> : (
                        <>
                          {m.kind !== 'text' && <span className="text-zinc-500">[{KIND_LABEL[m.kind] ?? m.kind}{m.media?.file_name ? `: ${m.media.file_name}` : ''}] </span>}
                          {m.text}
                          {m.edited && <span className="text-zinc-500"> (edited)</span>}
                          {m.reactions.length > 0 && <span className="text-zinc-500"> · {m.reactions.join(', ')}</span>}
                        </>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          {(state === 'stream_replaced' || (!link.running && link.has_session && state !== 'outdated')) && (
            <button onClick={() => run('reconnect', reconnectWhatsAppWeb)} disabled={!!busy} className={PRIMARY}>
              {busy === 'reconnect' ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />} Reconnect
            </button>
          )}
          {disconnect}
        </div>
      </>
    )
  }

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 overflow-hidden">
      <button
        type="button"
        onClick={() => setCollapsed((v) => !v)}
        className={
          'w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-zinc-900/80 transition-colors ' +
          (collapsed ? '' : 'border-b border-zinc-800')
        }
        aria-expanded={!collapsed}
      >
        <div className="h-10 w-10 rounded-md bg-gradient-to-br from-emerald-500/20 to-green-500/20 border border-zinc-800 flex items-center justify-center">
          <Smartphone className="h-5 w-5 text-zinc-200" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-zinc-100">WhatsApp Web — your own account</h3>
            {view?.available && (
              <span className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border ${PILL[label.tone]}`}>
                {label.tone === 'on' && <Check className="h-3 w-3" />}
                {label.text}
              </span>
            )}
          </div>
          <div className="text-xs text-zinc-500 mt-0.5">
            Link your WhatsApp as a device, the way WhatsApp Web does, so your agents can read it like your email.
          </div>
        </div>
        {!view && !error && <Loader2 className="h-4 w-4 animate-spin text-zinc-500" />}
        <ChevronDown className={'h-4 w-4 text-zinc-500 transition-transform ' + (collapsed ? '-rotate-90' : '')} />
      </button>

      {!collapsed && (
        <div className="px-5 py-4 space-y-3">
          {view?.can_admin && <AdminSwitch onChange={load} />}
          {body}
          {notice && <div className="text-xs text-emerald-600 dark:text-emerald-500">{notice}</div>}
          {error && (
            <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-300">
              {error}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
