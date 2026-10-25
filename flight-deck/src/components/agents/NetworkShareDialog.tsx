import { useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Globe, X, MessagesSquare, ShieldCheck, ToggleRight, Loader2, Network, Lightbulb } from 'lucide-react'
import { useNetworkShareStore } from '../../stores/networkShareStore'
import { getBotportStatus, isFullStatus, type BotportShareAgent } from '../../services/botportLink'

// "Share on network?" — what putting one of your agents on this deck's BotPort
// network means, before it happens. Opened by networkShareAction (the agent
// card's menu, the kiosk's "⋯") through the share store; mounted once, next to
// the app (main.tsx), so every layout gets the same dialog.

/** The other Flight Decks on the network, by name, as this user may see them. */
async function otherDecks(): Promise<string[] | null> {
  try {
    const st = await getBotportStatus()
    if (isFullStatus(st)) {
      return st.network.decks.filter((d) => d.status === 'connected').map((d) => d.label || d.name)
    }
    if (st.decks) return st.decks.map((d) => d.label || d.name)
    const names = new Set<string>()
    for (const r of st.remote_agents || []) {
      const at = r.lastIndexOf('@')
      if (at > 0) names.add(r.slice(at + 1))
    }
    return [...names]
  } catch {
    return null
  }
}

function Point({ icon: Icon, tone, title, children }: {
  icon: typeof Globe
  tone: 'cyan' | 'emerald' | 'violet' | 'amber'
  title: string
  children: ReactNode
}) {
  const tile = {
    cyan: 'bg-cyan-500/10 text-cyan-700 ring-cyan-500/25 dark:text-cyan-300',
    emerald: 'bg-emerald-500/10 text-emerald-700 ring-emerald-500/25 dark:text-emerald-300',
    violet: 'bg-violet-500/10 text-violet-700 ring-violet-500/25 dark:text-violet-300',
    amber: 'bg-amber-500/10 text-amber-700 ring-amber-500/25 dark:text-amber-300',
  }[tone]
  return (
    <div className="flex gap-3">
      <span className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ring-1 ${tile}`}>
        <Icon className="h-4 w-4" />
      </span>
      <div className="min-w-0">
        <div className="text-sm font-semibold text-zinc-100">{title}</div>
        <ul className="mt-1 space-y-1 text-[13px] leading-relaxed text-zinc-400">{children}</ul>
      </div>
    </div>
  )
}

export function NetworkShareDialog() {
  const pending = useNetworkShareStore((s) => s.confirm)
  if (!pending) return null
  // Keyed per request: each one starts with a fresh dialog state.
  return <ShareDialog key={`${pending.agent.kind}:${pending.agent.slug || pending.agent.name}`} pending={pending} />
}

function ShareDialog({ pending }: { pending: { agent: BotportShareAgent; displayName: string } }) {
  const deckName = useNetworkShareStore((s) => s.deckName)
  const close = useNetworkShareStore((s) => s.closeConfirm)
  const setShared = useNetworkShareStore((s) => s.setShared)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [decks, setDecks] = useState<string[] | null>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    let alive = true
    void otherDecks().then((d) => { if (alive) setDecks(d) })
    const back = document.activeElement as HTMLElement | null
    requestAnimationFrame(() => cancelRef.current?.focus())
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close() }
    window.addEventListener('keydown', onKey)
    return () => {
      alive = false
      window.removeEventListener('keydown', onKey)
      if (back && document.contains(back)) back.focus()
    }
  }, [close])

  const name = pending.displayName
  const address = deckName ? `${name}@${deckName}` : name

  const share = async () => {
    setBusy(true)
    setError('')
    try {
      await setShared(pending.agent, true)
      close()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setBusy(false)
    }
  }

  return createPortal(
    <div
      className="fixed inset-0 z-[70] flex items-end justify-center bg-black/60 backdrop-blur-[2px] wide:items-center wide:p-4"
      onClick={() => { if (!busy) close() }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="network-share-title"
        className="relative flex max-h-[92dvh] w-full flex-col overflow-hidden rounded-t-2xl border border-zinc-800 bg-zinc-900 shadow-2xl wide:max-w-lg wide:rounded-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header: the agent and the name other decks will use */}
        <div className="relative shrink-0 overflow-hidden border-b border-zinc-800 px-6 pb-5 pt-6">
          <div className="pointer-events-none absolute -right-16 -top-20 h-48 w-48 rounded-full bg-cyan-500/10 blur-3xl" />
          <div className="pointer-events-none absolute -left-10 -top-24 h-40 w-40 rounded-full bg-violet-500/10 blur-3xl" />
          <button
            onClick={close}
            disabled={busy}
            aria-label="Close"
            className="absolute right-3 top-3 rounded-lg p-1.5 text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-200"
          >
            <X className="h-4 w-4" />
          </button>
          <div className="relative flex items-center gap-4">
            <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-cyan-400 via-indigo-400 to-violet-500 text-white shadow-lg shadow-cyan-500/20">
              <Globe className="h-6 w-6" />
            </span>
            <div className="min-w-0">
              <h2 id="network-share-title" className="text-lg font-semibold leading-tight text-zinc-100">
                Share {name} on the network?
              </h2>
              <p className="mt-1 flex flex-wrap items-center gap-1.5 text-[13px] text-zinc-500">
                Other Flight Decks will know it as
                <code className="rounded-md bg-zinc-800 px-1.5 py-0.5 font-mono text-[12px] text-cyan-700 dark:text-cyan-300">{address}</code>
              </p>
            </div>
          </div>
        </div>

        {/* What it means */}
        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-6 py-5">
          <Point icon={MessagesSquare} tone="cyan" title="Agents on other Flight Decks can ask it for help">
            <li>They can send {name} questions and tasks, and get its answers back.</li>
            <li>
              Each request runs here, on this deck, with {name}'s own model and tools, in a conversation of
              its own that never mixes with yours.
            </li>
          </Point>
          <Point icon={ShieldCheck} tone="emerald" title="What stays out of their reach">
            <li>Your chats with {name} and your profile: their requests never see your conversations.</li>
            <li>
              Sending email, your Flows, scheduled tasks, the terminal, and any tool your admin blocks for
              requests from the network.
            </li>
          </Point>
          <Point icon={Lightbulb} tone="amber" title="Worth knowing">
            <li>
              To answer, {name} works with its own tools and data: its files, its datastore, and any inbox or
              account connected to it. Share it if you're comfortable with other decks getting answers drawn
              from those.
            </li>
            <li>Their requests use {name}'s model, so they count toward this deck's usage.</li>
          </Point>
          <Point icon={ToggleRight} tone="violet" title="You stay in control">
            <li>Stop sharing whenever you like, from the same menu. Your admin can stop it too.</li>
            <li>Removing the agent, or giving it to someone else, ends the sharing.</li>
          </Point>

          <div className="rounded-xl border border-zinc-800 bg-zinc-950/40 px-4 py-3 text-[13px]">
            <div className="flex items-start gap-2.5 text-zinc-400">
              <Network className="mt-0.5 h-4 w-4 shrink-0 text-zinc-500" />
              {decks === null ? (
                <span className="flex items-center gap-1.5"><Loader2 className="h-3.5 w-3.5 animate-spin" /> Looking at the network…</span>
              ) : decks.length ? (
                <span>
                  On this network now:{' '}
                  {decks.map((d, i) => (
                    <span key={d}>
                      {i > 0 && <span className="text-zinc-600"> · </span>}
                      <span className="font-medium text-zinc-200">{d}</span>
                    </span>
                  ))}
                  <span className="text-zinc-500"> — and any Flight Deck that joins later, if this deck accepts it.</span>
                </span>
              ) : (
                <span>No other Flight Deck is connected right now. Those that join later can ask it, if this deck accepts them.</span>
              )}
            </div>
          </div>

          {error && (
            <div role="alert" className="rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-[13px] text-red-700 dark:text-red-300">
              {error}
            </div>
          )}
        </div>

        {/* Decide */}
        <div className="flex shrink-0 flex-col-reverse gap-2 border-t border-zinc-800 bg-zinc-900/80 px-6 py-4 wide:flex-row wide:items-center wide:justify-end">
          <button
            ref={cancelRef}
            onClick={close}
            disabled={busy}
            className="rounded-xl px-4 py-2.5 text-sm font-medium text-zinc-300 transition-colors hover:bg-zinc-800 disabled:opacity-50"
          >
            Not now
          </button>
          <button
            onClick={share}
            disabled={busy}
            className="flex items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-cyan-600 to-violet-600 px-5 py-2.5 text-sm font-semibold text-white shadow-lg shadow-cyan-500/20 transition hover:brightness-110 disabled:opacity-60"
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Globe className="h-4 w-4" />}
            {busy ? 'Sharing…' : 'Share on network'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
