import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { Network, Radio, RefreshCw, Stethoscope, Loader2 } from 'lucide-react'
import { useAuthStore } from '../../stores/authStore'
import { selectConsoleAdmin } from '../../stores/consoleStore'
import {
  getBotportStatus, reconnectBotport, probeBotport, isFullStatus,
  type BotportStatus, type BotportStatusBasic, type BotportLinkState, type BotportProbe,
} from '../../services/botportLink'
import { Page, PageHeader, Card, Pill, Notice, Button, type Tone } from './parts'
import { NetworkSettings, ProbeLine } from './NetworkSettings'
import { NetworkCard, ActivityCard } from './NetworkActivity'
import { unixAgo } from './format'

// BotPort: this deck's link to a BotPort hub, which joins Flight Decks into
// one network — an agent here consults an agent on another deck (Agent →
// this deck → hub → that deck → agent), and other decks use the agents an
// admin exposes here. The deck holds the one connection; agents no longer
// link to the hub themselves (that per-agent link is legacy).

const POLL_MS = 5000

const STATE: Record<BotportLinkState, { tone: Tone; label: string }> = {
  connected: { tone: 'emerald', label: 'Connected' },
  connecting: { tone: 'amber', label: 'Connecting…' },
  backoff: { tone: 'amber', label: 'Retrying' },
  error: { tone: 'red', label: 'Error' },
  disabled: { tone: 'zinc', label: 'Off' },
  stopped: { tone: 'zinc', label: 'Stopped' },
}

function Fact({ label, children, title }: { label: string; children: ReactNode; title?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] font-medium text-zinc-500">{label}</dt>
      <dd className="mt-0.5 break-words text-sm text-zinc-200" title={title}>{children}</dd>
    </div>
  )
}

function StatusCard({ status, admin, onRefresh }: {
  status: BotportStatusBasic | BotportStatus
  admin: boolean
  onRefresh: () => Promise<void>
}) {
  const [busy, setBusy] = useState<'' | 'reconnect' | 'probe'>('')
  const [probe, setProbe] = useState<BotportProbe | null>(null)
  const [error, setError] = useState('')
  const full = isFullStatus(status) ? status : null
  const st = STATE[status.state] ?? { tone: 'zinc' as Tone, label: status.state }
  const label = status.state === 'backoff' && full?.retry_in != null ? `Retrying in ${full.retry_in}s` : st.label

  const reconnect = async () => {
    setBusy('reconnect')
    setError('')
    try {
      await reconnectBotport()
      await onRefresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy('')
    }
  }

  const test = async () => {
    setBusy('probe')
    setProbe(null)
    try {
      setProbe(await probeBotport())
    } catch (e) {
      setProbe({ ok: false, error: e instanceof Error ? e.message : String(e) })
    } finally {
      setBusy('')
    }
  }

  const since = full?.connected_since && status.state === 'connected' ? full.connected_since : null
  const showError = !!full?.last_error && (status.state === 'error' || status.state === 'backoff')

  return (
    <Card
      icon={Radio}
      title="Status"
      action={admin && full ? (
        <>
          <Button icon={Stethoscope} busy={busy === 'probe'} disabled={!full.url || busy !== ''} onClick={test} title="Check the saved hub address answers (does not connect)">
            Test hub
          </Button>
          <Button icon={RefreshCw} busy={busy === 'reconnect'} disabled={!status.enabled || busy !== ''} onClick={reconnect}>
            Reconnect
          </Button>
        </>
      ) : undefined}
    >
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <Pill tone={st.tone}>{label}</Pill>
          {!status.enabled && (
            <span className="text-xs text-zinc-500">
              {admin ? 'The link is off — turn it on under Connection below.' : 'This deck is not connected to a BotPort hub.'}
            </span>
          )}
        </div>

        {full && showError && (
          <Notice tone={status.state === 'error' ? 'red' : 'amber'}>
            <span className="break-words">{full.last_error}</span>
            {full.last_error_at && <span className="text-xs opacity-75"> · {unixAgo(full.last_error_at)}</span>}
          </Notice>
        )}

        <dl className="grid grid-cols-1 gap-x-6 gap-y-3 wide:grid-cols-2">
          <Fact label="This deck on the network">{status.deck_name || '—'}</Fact>
          {full && (
            <>
              <Fact label="Hub"><span className="break-all font-mono text-xs">{full.url || '—'}</span></Fact>
              <Fact label="Hub version">{full.hub_version || '—'}</Fact>
              <Fact label="Connected" title={since ? new Date(since * 1000).toLocaleString() : undefined}>
                {since ? unixAgo(since) : '—'}
              </Fact>
            </>
          )}
        </dl>

        {full && full.problems.length > 0 && (
          <Notice tone="amber">
            <div className="font-medium">The link can't start until this is fixed:</div>
            <ul className="mt-1 list-disc space-y-0.5 pl-5">
              {full.problems.map((p) => <li key={p} className="break-words">{p}</li>)}
            </ul>
          </Notice>
        )}

        {/* Non-admins: the remote agents their agents may consult. */}
        {!full && status.remote_agents && (
          <div>
            <div className="mb-1.5 text-[11px] font-medium text-zinc-500">Agents on other decks you can consult</div>
            {status.remote_agents.length === 0 ? <p className="text-xs text-zinc-500">None right now.</p> : (
              <div className="flex flex-wrap gap-1.5">
                {status.remote_agents.map((a) => (
                  <span key={a} className="max-w-full truncate rounded-lg border border-zinc-800 bg-zinc-900 px-2 py-0.5 font-mono text-xs text-zinc-200">{a}</span>
                ))}
              </div>
            )}
          </div>
        )}

        {probe && <ProbeLine probe={probe} />}
        {error && <Notice tone="red">{error}</Notice>}
      </div>
    </Card>
  )
}

export function NetworkConsole() {
  const admin = useAuthStore(selectConsoleAdmin)
  const signInOff = useAuthStore((s) => s.authEnabled === false)
  const [status, setStatus] = useState<BotportStatusBasic | BotportStatus | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => getBotportStatus().then(
    (s) => { setStatus(s); setError('') },
    (e) => setError(e instanceof Error ? e.message : String(e)),
  ), [])

  // The link changes on its own (reconnects, remote work): keep it fresh while
  // the page is on screen; a hidden tab doesn't poll, and catches up on return.
  useEffect(() => {
    load()
    const t = window.setInterval(() => { if (!document.hidden) load() }, POLL_MS)
    const onVisible = () => { if (!document.hidden) load() }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.clearInterval(t)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [load])

  const full = status && isFullStatus(status) ? status : null

  return (
    <Page>
      <PageHeader
        icon={Network}
        title="BotPort"
        description="Connect this deck to a BotPort hub so its agents can consult agents on other Flight Decks — and choose which of its own agents other decks may use. A remote request always runs in a separate lane of the agent (never its owner's main chat), can never write email, and can't reach back into the network."
      />

      {signInOff && (
        <Notice>
          Sign-in is off on this deck: BotPort settings can only be changed from this machine (or with FD_BOTPORT_* environment variables).
        </Notice>
      )}
      {!admin && <Notice>Only an admin can change this deck's BotPort link.</Notice>}
      {error && <Notice tone="red">{error}</Notice>}

      {status ? <StatusCard status={status} admin={admin} onRefresh={load} /> : !error && (
        <Card icon={Radio} title="Status">
          <div className="flex items-center gap-2 text-sm text-zinc-500"><Loader2 className="h-4 w-4 animate-spin" /> Checking the link…</div>
        </Card>
      )}

      {admin && <NetworkSettings onSaved={load} />}
      {full && <NetworkCard status={full} />}
      {full && <ActivityCard status={full} />}
    </Page>
  )
}
