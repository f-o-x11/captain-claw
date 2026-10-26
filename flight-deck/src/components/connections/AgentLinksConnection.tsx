import { useCallback, useEffect, useId, useState, type FormEvent } from 'react'
import {
  Cable, Loader2, Check, AlertCircle, Trash2, Plus, Copy, ChevronDown, KeyRound, Pencil,
} from 'lucide-react'
import {
  changeLink, createLink, deleteLink, linkableAgents, linkHttpBase, linkSocketUrl, listLinks, newLinkKey,
  newWebhookSecret, LINK_INSTRUCTIONS_MAX, type AgentLink, type AgentLinkSecrets, type LinkableAgent,
  type LinkDirection,
} from '../../services/agentLinks'

// Systems: agent links (captain_claw/flight_deck/agent_link.py). Another
// system — a CRM, a shop, a monitoring tool — holds a WebSocket to one of the
// person's agents with a key of its own (or talks HTTP, with a webhook for
// what comes back: agent_link_http.py), sends it messages and files, and
// gets the answers back; the agent can message it on its own too. A system
// that won't take connections gets one from Flight Deck instead (an outbound
// link: its address and the key it issued). The list polls while this card
// is open, so a system connecting or dropping off shows up here.

const pill = 'inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border'
const POLL_MS = 10_000

interface Draft {
  name: string
  agent_slug: string
  instructions: string
  agent_may_send: boolean
  webhook_url: string
  direction: LinkDirection
  remote_url: string
  remote_key: string // "" while editing keeps the key kept
}

const EMPTY: Draft = {
  name: '', agent_slug: '', instructions: '', agent_may_send: true, webhook_url: '',
  direction: 'inbound', remote_url: '', remote_key: '',
}

// A secret shown once: the link's new key, its webhook's new signing secret.
interface Fresh extends AgentLinkSecrets {
  name: string
}

function hostOf(url: string): string {
  try { return new URL(url).host } catch { return url }
}

function when(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function LinkForm({ draft, onChange, agents, busy, submitLabel, onSubmit, onCancel, editing = false }: {
  draft: Draft
  onChange: (d: Draft) => void
  agents: LinkableAgent[]
  busy: boolean
  submitLabel: string
  onSubmit: () => void
  onCancel: () => void
  editing?: boolean
}) {
  const id = useId()
  const known = agents.some((a) => a.slug === draft.agent_slug)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!busy) onSubmit()
  }
  return (
    <form onSubmit={submit} className="rounded-md border border-zinc-800 bg-zinc-950/60 p-3 space-y-3">
      <fieldset className="space-y-1.5">
        <legend className="text-xs text-zinc-400">Who connects</legend>
        <div className="flex flex-wrap gap-x-4 gap-y-1.5">
          {([
            ['inbound', 'The system connects to Flight Deck'],
            ['outbound', 'Flight Deck connects to the system'],
          ] as const).map(([value, label]) => (
            <label key={value} className="flex items-center gap-1.5 text-xs text-zinc-300">
              <input
                type="radio"
                name={`${id}-direction`}
                value={value}
                checked={draft.direction === value}
                onChange={() => onChange({ ...draft, direction: value })}
                className="h-3.5 w-3.5 accent-violet-500"
              />
              {label}
            </label>
          ))}
        </div>
        {draft.direction === 'outbound' && (
          <span className="block text-[11px] text-zinc-500">
            For a system that won't take connections from agents (an orchestrator): Flight Deck dials its address with
            the key it gave you, keeps the connection open and dials again after a drop — no public address needed here.
          </span>
        )}
      </fieldset>
      {draft.direction === 'outbound' && (
        <div className="grid gap-3 sm:grid-cols-2">
          <label htmlFor={`${id}-remote-url`} className="block space-y-1">
            <span className="text-xs text-zinc-400">Its address</span>
            <input
              id={`${id}-remote-url`}
              value={draft.remote_url}
              onChange={(e) => onChange({ ...draft, remote_url: e.target.value })}
              placeholder="wss://orchestrator.example/agent"
              maxLength={500}
              inputMode="url"
              className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-200 focus:border-violet-500/50 focus:outline-none"
            />
          </label>
          <label htmlFor={`${id}-remote-key`} className="block space-y-1">
            <span className="text-xs text-zinc-400">The key it gave you</span>
            <input
              id={`${id}-remote-key`}
              type="password"
              autoComplete="off"
              value={draft.remote_key}
              onChange={(e) => onChange({ ...draft, remote_key: e.target.value })}
              placeholder={editing ? 'Leave empty to keep the key it has' : 'Sent as Authorization: Bearer …'}
              className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-200 focus:border-violet-500/50 focus:outline-none"
            />
          </label>
        </div>
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        <label htmlFor={`${id}-name`} className="block space-y-1">
          <span className="text-xs text-zinc-400">Name</span>
          <input
            id={`${id}-name`}
            value={draft.name}
            onChange={(e) => onChange({ ...draft, name: e.target.value })}
            placeholder="e.g. Acme CRM"
            maxLength={80}
            className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-200 focus:border-violet-500/50 focus:outline-none"
          />
        </label>
        <label htmlFor={`${id}-agent`} className="block space-y-1">
          <span className="text-xs text-zinc-400">Agent</span>
          <select
            id={`${id}-agent`}
            value={draft.agent_slug}
            onChange={(e) => onChange({ ...draft, agent_slug: e.target.value })}
            className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-200 focus:border-violet-500/50 focus:outline-none"
          >
            <option value="">Your first running agent</option>
            {agents.map((a) => (
              <option key={a.slug} value={a.slug}>
                {a.name}{a.status === 'running' ? '' : ' (stopped)'}
              </option>
            ))}
            {draft.agent_slug && !known && <option value={draft.agent_slug}>{draft.agent_slug}</option>}
          </select>
        </label>
      </div>
      <label htmlFor={`${id}-instructions`} className="block space-y-1">
        <span className="text-xs text-zinc-400">Your instructions for what it sends</span>
        <textarea
          id={`${id}-instructions`}
          value={draft.instructions}
          onChange={(e) => onChange({ ...draft, instructions: e.target.value })}
          rows={3}
          maxLength={LINK_INSTRUCTIONS_MAX}
          placeholder="e.g. For each new lead, write a short note and add it to the Leads table."
          className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-200 focus:border-violet-500/50 focus:outline-none"
        />
        <span className="flex flex-wrap justify-between gap-2 text-[11px] text-zinc-500">
          <span>The agent writes email for this link only when these instructions ask for it.</span>
          <span>{draft.instructions.length}/{LINK_INSTRUCTIONS_MAX}</span>
        </span>
      </label>
      <label htmlFor={`${id}-may-send`} className="flex items-start gap-2">
        <input
          id={`${id}-may-send`}
          type="checkbox"
          checked={draft.agent_may_send}
          onChange={(e) => onChange({ ...draft, agent_may_send: e.target.checked })}
          className="mt-0.5 h-3.5 w-3.5 accent-violet-500"
        />
        <span className="text-xs text-zinc-300">
          The agent can message it on its own
          <span className="block text-[11px] text-zinc-500">e.g. a scheduled job's result, sent with files, without the system asking first.</span>
        </span>
      </label>
      {draft.direction === 'inbound' && <label htmlFor={`${id}-webhook`} className="block space-y-1">
        <span className="text-xs text-zinc-400">Webhook (optional)</span>
        <input
          id={`${id}-webhook`}
          value={draft.webhook_url}
          onChange={(e) => onChange({ ...draft, webhook_url: e.target.value })}
          placeholder="https://your-system.example/captain-webhook"
          maxLength={500}
          inputMode="url"
          className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-200 focus:border-violet-500/50 focus:outline-none"
        />
        <span className="block text-[11px] text-zinc-500">
          For a system that can't keep a connection open: Flight Deck posts the answers to its HTTP messages and the
          agent's own messages here, signed with a secret you get once.
        </span>
      </label>}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="submit"
          disabled={busy}
          className="flex items-center gap-1 rounded-md border border-violet-500/40 bg-violet-500/10 px-3 py-2 text-xs font-medium text-violet-700 dark:text-violet-200 hover:bg-violet-500/20 disabled:opacity-50"
        >
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />} {submitLabel}
        </button>
        <button type="button" onClick={onCancel} className="rounded-md px-3 py-2 text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800">
          Cancel
        </button>
      </div>
    </form>
  )
}

export interface LinksStatus {
  links: number
  connected: number
}

// `active` — this card's tab is the one showing: only then does the list
// poll for systems connecting and dropping off. `onStatus` — how many links
// there are and how many systems are connected right now (the tab's dot,
// the console's row).
export default function AgentLinksConnection({ active = true, onStatus }: {
  active?: boolean
  onStatus?: (status: LinksStatus) => void
}) {
  const [collapsed, setCollapsed] = useState(false)
  const [links, setLinks] = useState<AgentLink[] | null>(null)
  const [agents, setAgents] = useState<LinkableAgent[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<string | null>(null) // "create", or "<action>:<link id>"
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [editing, setEditing] = useState<string | null>(null)
  const [editDraft, setEditDraft] = useState<Draft>(EMPTY)
  const [fresh, setFresh] = useState<Fresh | null>(null)
  const [copied, setCopied] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setLinks(await listLinks())
      setError('')
    } catch (e) {
      setError(message(e))
    }
  }, [])

  const connected = links?.filter((l) => l.connected).length ?? 0
  useEffect(() => {
    if (links) onStatus?.({ links: links.length, connected })
  }, [links, connected, onStatus])

  // The list once (the tab's dot), the agents when the card is first shown.
  useEffect(() => {
    listLinks().then(setLinks).catch((e) => setError(message(e)))
  }, [])
  useEffect(() => {
    if (active && !collapsed && agents.length === 0) linkableAgents().then(setAgents).catch(() => {})
  }, [active, collapsed, agents.length])
  useEffect(() => {
    if (!active || collapsed) return
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') refresh()
    }, POLL_MS)
    return () => window.clearInterval(timer)
  }, [active, collapsed, refresh])

  const run = async (what: string, job: () => Promise<void>) => {
    if (busy) return
    setBusy(what)
    try {
      await job()
      setError('')
    } catch (e) {
      setError(message(e))
    }
    setBusy(null)
    await refresh()
  }

  // What a form sends: an outbound link's address and key, else its webhook.
  const fieldsOf = (d: Draft) => {
    const { remote_url, remote_key, webhook_url, ...rest } = d
    return d.direction === 'outbound'
      ? { ...rest, name: d.name.trim(), remote_url: remote_url.trim(), ...(remote_key.trim() ? { remote_key: remote_key.trim() } : {}) }
      : { ...rest, name: d.name.trim(), webhook_url: webhook_url.trim() }
  }

  const create = () => run('create', async () => {
    const made = await createLink(fieldsOf(draft))
    setFresh({ name: made.name, key: made.key, webhook_secret: made.webhook_secret })
    setDraft(EMPTY)
    setAdding(false)
  })

  const save = (link: AgentLink) => run(`save:${link.id}`, async () => {
    const changed = await changeLink(link.id, fieldsOf(editDraft))
    if (changed.webhook_secret) setFresh({ name: changed.name, webhook_secret: changed.webhook_secret })
    setEditing(null)
  })

  const rotate = (link: AgentLink) => {
    if (!window.confirm(`Make a new key for "${link.name}"?\n\nThe old key stops working at once, and a system connected with it is disconnected until it uses the new one.`)) return
    run(`key:${link.id}`, async () => {
      const made = await newLinkKey(link.id)
      setFresh({ name: made.name, key: made.key })
    })
  }

  const renewSecret = (link: AgentLink) => {
    if (!window.confirm(`Make a new webhook signing secret for "${link.name}"?\n\nFrom now on its webhook posts are signed with the new one.`)) return
    run(`secret:${link.id}`, async () => {
      const made = await newWebhookSecret(link.id)
      setFresh({ name: made.name, webhook_secret: made.webhook_secret })
    })
  }

  const remove = (link: AgentLink) => {
    if (!window.confirm(`Remove "${link.name}"?\n\nIts key stops working, a connected system is disconnected, and messages still waiting are dropped. The agent keeps the link's session.`)) return
    run(`delete:${link.id}`, async () => {
      await deleteLink(link.id)
      if (editing === link.id) setEditing(null)
    })
  }

  const edit = (link: AgentLink) => {
    setEditing(link.id)
    setEditDraft({
      name: link.name, agent_slug: link.agent_slug, instructions: link.instructions,
      agent_may_send: link.agent_may_send, webhook_url: link.webhook_url,
      direction: link.direction, remote_url: link.remote_url, remote_key: '',
    })
  }

  const copy = (text: string, which: string) => {
    navigator.clipboard?.writeText(text).then(() => {
      setCopied(which)
      setTimeout(() => setCopied(null), 1500)
    }).catch(() => {})
  }

  const agentName = (slug: string) => agents.find((a) => a.slug === slug)?.name || slug

  const statusLine = (link: AgentLink): string => {
    const out = link.outbound
    if (link.connected) return link.direction === 'outbound' ? 'Connected — Flight Deck dialled it' : 'Connected now'
    if (out?.state === 'connecting') return 'Connecting…'
    if (out?.state === 'waiting') {
      return `Can't connect: ${out.error || 'no answer'}${out.retry_at ? ` — trying again at ${new Date(out.retry_at).toLocaleTimeString()}` : ''}`
    }
    if (link.direction === 'outbound') return 'Not connected'
    return link.last_used_at ? `Last connected ${when(link.last_used_at)}` : 'Never connected'
  }
  const stopped = (slug: string) => {
    const agent = agents.find((a) => a.slug === slug)
    return !!agent && agent.status !== 'running'
  }

  let statusPill
  if (!links && error) {
    statusPill = <span className={`${pill} bg-red-500/15 text-red-600 dark:text-red-500 border-red-500/30`}><AlertCircle className="h-3 w-3" /> Unavailable</span>
  } else if (!links) {
    statusPill = <span className={`${pill} bg-zinc-800 text-zinc-400 border-zinc-700`}><Loader2 className="h-3 w-3 animate-spin" /> Checking</span>
  } else if (connected > 0) {
    statusPill = <span className={`${pill} bg-emerald-500/15 text-emerald-600 dark:text-emerald-500 border-emerald-500/30`}><Check className="h-3 w-3" /> {connected} connected</span>
  } else if (links.length > 0) {
    statusPill = <span className={`${pill} bg-zinc-800 text-zinc-400 border-zinc-700`}>{links.length} link{links.length === 1 ? '' : 's'}, none connected</span>
  } else {
    statusPill = <span className={`${pill} bg-zinc-800 text-zinc-400 border-zinc-700`}>No links yet</span>
  }

  const url = linkSocketUrl()
  const http = linkHttpBase()
  const example = '{"type":"message","id":"lead-42","text":"New lead: Ana Horvat, Acme"}'

  const secretRow = (label: string, value: string, which: string) => (
    <div className="space-y-1">
      <p className="text-[11px] text-zinc-500">{label}</p>
      <div className="flex items-center gap-2">
        <code className="block min-w-0 flex-1 text-xs text-zinc-200 font-mono bg-zinc-950 rounded p-2 break-all">{value}</code>
        <button type="button" onClick={() => copy(value, which)} className="shrink-0 rounded p-1.5 text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800" title={`Copy ${label.toLowerCase()}`} aria-label={`Copy ${label.toLowerCase()}`}>
          {copied === which ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
        </button>
      </div>
    </div>
  )

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 overflow-hidden">
      <button
        type="button"
        onClick={() => setCollapsed((v) => !v)}
        className={'w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-zinc-900/80 transition-colors ' + (collapsed ? '' : 'border-b border-zinc-800')}
        aria-expanded={!collapsed}
      >
        <div className="h-10 w-10 shrink-0 rounded-md bg-gradient-to-br from-violet-500/20 to-emerald-500/20 border border-zinc-800 flex items-center justify-center">
          <Cable className="h-5 w-5 text-zinc-200" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-zinc-100">Systems</h3>
            {statusPill}
          </div>
          <div className="text-xs text-zinc-500 mt-0.5">Other systems that send your agents messages and files</div>
        </div>
        <ChevronDown className={'h-4 w-4 shrink-0 text-zinc-500 transition-transform ' + (collapsed ? '-rotate-90' : '')} />
      </button>

      <div hidden={collapsed} className="px-5 py-4 space-y-4">
        <p className="text-xs leading-relaxed text-zinc-400">
          Connect a system — a CRM, a shop, a monitoring tool — to one of your agents. It sends the agent
          messages and files and gets the answers back, and any files the agent makes for it. Each link has its own
          key and its own session on the agent (“🔗 name”), so it stays out of your chats. What a system sends
          is its content, not your words: the agent goes by your instructions for the link.
        </p>

        {error && (
          <div className="flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-300">
            <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
            <span className="min-w-0 flex-1 break-words">{error}</span>
          </div>
        )}

        <div className="rounded-md border border-zinc-800 bg-zinc-950/60 p-3 space-y-2">
          <p className="text-xs font-medium text-zinc-300">How a system connects</p>
          <div className="flex items-center gap-2">
            <code className="block min-w-0 flex-1 text-xs text-zinc-300 font-mono bg-zinc-900 rounded p-2 break-all">{url}</code>
            <button type="button" onClick={() => copy(url, 'url')} className="shrink-0 rounded p-1.5 text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800" title="Copy address" aria-label="Copy address">
              {copied === 'url' ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
            </button>
          </div>
          <p className="text-[11px] leading-relaxed text-zinc-500">
            It opens a WebSocket there with its key in the <code className="text-zinc-400">Authorization: Bearer</code> header
            (or <code className="text-zinc-400">?key=</code> where it can't set one), then sends messages like
          </p>
          <code className="block text-[11px] text-zinc-300 font-mono bg-zinc-900 rounded p-2 break-all">{example}</code>
          <p className="text-[11px] leading-relaxed text-zinc-500">
            Every answer comes back with <code className="text-zinc-400">"re"</code> set to the message's id. Files go in a
            message's <code className="text-zinc-400">files</code> as base64. Messages the agent sends on its own come as{' '}
            <code className="text-zinc-400">"push"</code>.
          </p>
          <p className="text-[11px] leading-relaxed text-zinc-500">
            Without a connection, over HTTP with the same key: <code className="text-zinc-400">POST {http}/link/messages</code>{' '}
            (add <code className="text-zinc-400">?wait=60</code> to get the answer in the reply), and{' '}
            <code className="text-zinc-400">GET {http}/link/events?after=…</code> for answers and the agent's messages —
            or give the link a webhook and they're posted to it.
          </p>
          <p className="text-[11px] leading-relaxed text-zinc-500">
            A system that won't take connections from agents (an orchestrator) gets one from Flight Deck instead: choose
            "Flight Deck connects to the system" and give its address and the key it issued. The conversation is the same;
            Flight Deck says <code className="text-zinc-400">hello</code> first.
          </p>
        </div>

        {fresh && (fresh.key || fresh.webhook_secret) && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/[0.06] p-3 space-y-2" role="status">
            <p className="text-xs text-amber-600 dark:text-amber-500/90 flex items-center gap-1">
              <KeyRound className="h-3.5 w-3.5 shrink-0" /> For “{fresh.name}” — copy {fresh.key && fresh.webhook_secret ? 'these' : 'it'} now, it won't be shown again.
            </p>
            {fresh.key && secretRow('Key', fresh.key, 'key')}
            {fresh.webhook_secret && secretRow('Webhook signing secret', fresh.webhook_secret, 'secret')}
            <button type="button" onClick={() => setFresh(null)} className="text-[11px] text-zinc-500 hover:text-zinc-300">Done</button>
          </div>
        )}

        {adding ? (
          <LinkForm
            draft={draft}
            onChange={setDraft}
            agents={agents}
            busy={busy === 'create'}
            submitLabel="Create link"
            onSubmit={create}
            onCancel={() => { setAdding(false); setDraft(EMPTY) }}
          />
        ) : (
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="flex items-center gap-1 rounded-md border border-violet-500/40 bg-violet-500/10 px-3 py-2 text-xs font-medium text-violet-700 dark:text-violet-200 hover:bg-violet-500/20"
          >
            <Plus className="h-3.5 w-3.5" /> New link
          </button>
        )}

        <div className="space-y-2">
          {!links && !error && (
            <div className="flex items-center gap-2 text-xs text-zinc-500"><Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…</div>
          )}
          {links && links.length === 0 && <p className="text-xs text-zinc-600">No systems linked yet.</p>}
          {links?.map((link) => editing === link.id ? (
            <LinkForm
              key={link.id}
              draft={editDraft}
              onChange={setEditDraft}
              agents={agents}
              busy={busy === `save:${link.id}`}
              submitLabel="Save"
              onSubmit={() => save(link)}
              onCancel={() => setEditing(null)}
              editing
            />
          ) : (
            <div key={link.id} className="rounded-md border border-zinc-800 bg-zinc-950/40 px-3 py-2.5">
              <div className="flex items-start gap-2.5">
                <span
                  className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${link.connected ? 'bg-emerald-500' : 'bg-zinc-600'}`}
                  title={link.connected ? 'Connected now' : 'Not connected'}
                />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium text-zinc-200">{link.name || '(unnamed)'}</div>
                  <div className={`text-[11px] ${link.connected ? 'text-emerald-600 dark:text-emerald-500'
                    : link.outbound?.state === 'waiting' ? 'text-amber-600 dark:text-amber-500' : 'text-zinc-500'}`}>
                    {statusLine(link)}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-0.5">
                  <button type="button" onClick={() => edit(link)} disabled={!!busy} className="rounded p-1.5 text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800 disabled:opacity-50" title="Edit" aria-label={`Edit ${link.name}`}>
                    <Pencil className="h-3.5 w-3.5" />
                  </button>
                  {link.direction === 'inbound' && (
                    <button type="button" onClick={() => rotate(link)} disabled={!!busy} className="rounded p-1.5 text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800 disabled:opacity-50" title="New key" aria-label={`New key for ${link.name}`}>
                      {busy === `key:${link.id}` ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <KeyRound className="h-3.5 w-3.5" />}
                    </button>
                  )}
                  <button type="button" onClick={() => remove(link)} disabled={!!busy} className="rounded p-1.5 text-zinc-500 hover:text-red-400 hover:bg-zinc-800 disabled:opacity-50" title="Remove" aria-label={`Remove ${link.name}`}>
                    {busy === `delete:${link.id}` ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
                  </button>
                </div>
              </div>
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 pl-[18px] text-[11px] text-zinc-500">
                <span>
                  Agent: <span className="text-zinc-400">{link.agent_slug ? agentName(link.agent_slug) : 'your first running agent'}</span>
                  {link.agent_slug && stopped(link.agent_slug) && (
                    <span className="text-amber-600 dark:text-amber-500"> · stopped — its messages fail until it runs</span>
                  )}
                </span>
                {link.direction === 'outbound'
                  ? <span>Flight Deck connects to <span className="text-zinc-400">{hostOf(link.remote_url)}</span></span>
                  : <span>Key …{link.key_hint}</span>}
                <span>{link.agent_may_send ? 'The agent can message it' : 'Answers only'}</span>
                {link.webhook_url && (
                  <span>
                    Webhook: <span className="text-zinc-400">{hostOf(link.webhook_url)}</span>{' '}
                    <button type="button" onClick={() => renewSecret(link)} disabled={!!busy} className="underline decoration-dotted hover:text-zinc-300 disabled:opacity-50">
                      new signing secret
                    </button>
                  </span>
                )}
              </div>
              {link.instructions ? (
                <p className="mt-1 pl-[18px] text-[11px] text-zinc-400 line-clamp-2 break-words">{link.instructions}</p>
              ) : (
                <p className="mt-1 pl-[18px] text-[11px] text-zinc-600">No instructions — the agent works on what each message asks, and writes no email.</p>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
