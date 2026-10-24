import { Globe, Network, Activity, ArrowDownLeft, ArrowUpRight, Inbox, Cable } from 'lucide-react'
import type { BotportStatus, BotportDeck, BotportActivity } from '../../services/botportLink'
import { Card, StatTile, Pill, Dot, EmptyState } from './parts'
import { fmtDuration } from './format'

// BotPort → what the link sees: the other decks on the hub with the agents
// they share, and what has gone over the link since Flight Deck started.

const ver = (v: string) => (/^\d/.test(v) ? `v${v}` : v)

const sectionCls = 'mb-2 text-[11px] font-semibold uppercase tracking-wider text-zinc-500'

// ── Network ──

function DeckRow({ deck }: { deck: BotportDeck }) {
  const on = deck.status === 'connected'
  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-950/40 p-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="flex min-w-0 items-center gap-2">
          <Dot tone={on ? 'on' : 'off'} />
          <span className="truncate text-sm font-medium text-zinc-100">{deck.name}</span>
        </span>
        {deck.version && <span className="text-[11px] text-zinc-500">{ver(deck.version)}</span>}
        <span className="ml-auto text-[11px] tabular-nums text-zinc-500">
          {on ? `${deck.active_concerns}/${deck.max_concurrent} busy` : 'disconnected'}
        </span>
      </div>
      {deck.agents.length > 0 ? (
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {deck.agents.map((a) => {
            const running = (a.status || 'running') === 'running'
            const tip = [a.description, a.expertise_tags?.length ? a.expertise_tags.join(', ') : '', running ? '' : a.status]
              .filter(Boolean).join('\n')
            return (
              <span
                key={a.slug || a.name}
                title={tip || undefined}
                className={`max-w-full truncate rounded-lg border border-zinc-800 bg-zinc-900 px-2 py-0.5 font-mono text-xs ${
                  running ? 'text-zinc-200' : 'text-zinc-500 line-through decoration-zinc-600'
                }`}
              >
                {a.name}@{deck.name}
              </span>
            )
          })}
        </div>
      ) : (
        <div className="mt-1.5 text-xs text-zinc-500">{on ? 'Shares no agents.' : 'Offline.'}</div>
      )}
    </div>
  )
}

export function NetworkCard({ status }: { status: BotportStatus }) {
  const decks = status.network?.decks ?? []
  const legacy = status.network?.agents ?? []
  const connected = status.state === 'connected'
  return (
    <Card
      icon={Globe}
      title="Network"
      hint="The other Flight Decks on the hub and the agents each one shares. Agents here reach them as Agent@deck."
    >
      {decks.length === 0 && legacy.length === 0 ? (
        <EmptyState icon={Network} title={connected ? 'No other decks on the hub yet' : 'Not on a hub'}>
          {connected
            ? 'When another Flight Deck connects to this hub, it shows here with the agents it shares.'
            : 'Connect to a BotPort hub to see the other decks and their agents.'}
        </EmptyState>
      ) : (
        <div className="space-y-4">
          {!connected && <p className="text-xs text-zinc-500">As last seen — the link is not connected now.</p>}
          {decks.length > 0 && (
            <div className="space-y-2">
              {legacy.length > 0 && <div className={sectionCls}>Flight Decks</div>}
              {decks.map((d) => <DeckRow key={d.name} deck={d} />)}
            </div>
          )}
          {legacy.length > 0 && (
            <div>
              <div className={sectionCls}>Agents linked to the hub directly (legacy)</div>
              <div className="flex flex-wrap gap-1.5">
                {legacy.map((a) => (
                  <span
                    key={a.name}
                    title={a.personas.map((p) => (p.description ? `${p.name}: ${p.description}` : p.name)).join('\n') || undefined}
                    className="inline-flex max-w-full items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900 px-2 py-0.5 text-xs text-zinc-300"
                  >
                    <span className="truncate">{a.name}</span>
                    <span className="shrink-0 tabular-nums text-zinc-500">{a.active_concerns}/{a.max_concurrent}</span>
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </Card>
  )
}

// ── Activity ──

function when(at: number): { short: string; full: string } {
  const d = new Date(at * 1000)
  const today = d.toDateString() === new Date().toDateString()
  return {
    short: today ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : d.toLocaleDateString(),
    full: d.toLocaleString(),
  }
}

function DirIcon({ dir }: { dir: 'in' | 'out' }) {
  return dir === 'in' ? (
    <span title="From another deck" className="shrink-0 text-violet-600 dark:text-violet-400"><ArrowDownLeft className="h-3.5 w-3.5" /></span>
  ) : (
    <span title="To another deck" className="shrink-0 text-cyan-600 dark:text-cyan-400"><ArrowUpRight className="h-3.5 w-3.5" /></span>
  )
}

function RecentRow({ r }: { r: BotportActivity }) {
  const t = when(r.at)
  return (
    <li className="flex items-start gap-3 py-2">
      <span className="w-20 shrink-0 whitespace-nowrap pt-px text-[11px] tabular-nums text-zinc-500" title={t.full}>{t.short}</span>
      <span className="pt-0.5"><DirIcon dir={r.dir} /></span>
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          {/* in: the asking deck → the agent here; out: the remote agent asked */}
          <span className="min-w-0 truncate text-sm text-zinc-200">
            {r.peer || '?'}
            {r.dir === 'in' && r.agent && <><span className="text-zinc-500"> → </span>{r.agent}</>}
          </span>
          {r.ok === true ? <Pill tone="emerald">ok</Pill> : r.ok === false ? <Pill tone="red">failed</Pill> : <Pill>ended</Pill>}
        </span>
        {r.detail && <span className="mt-0.5 block truncate text-xs text-zinc-500" title={r.detail}>{r.detail}</span>}
      </span>
    </li>
  )
}

export function ActivityCard({ status }: { status: BotportStatus }) {
  const c = status.counters ?? {}
  const n = (k: string) => c[k] ?? 0
  const inbound = status.inbound ?? []
  const outbound = status.outbound ?? []
  const recent = status.recent ?? []
  const row = 'flex items-start gap-2 rounded-xl border border-zinc-800 bg-zinc-950/40 px-3 py-2'
  return (
    <Card icon={Activity} title="Activity" hint="Work over the link since Flight Deck started.">
      <div className="space-y-6">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile
            icon={ArrowDownLeft} tone="violet" label="Done for others" value={n('inbound_ok')}
            sub={`${n('inbound_failed')} failed · ${n('inbound_refused')} refused`}
          />
          <StatTile
            icon={ArrowUpRight} tone="cyan" label="Asked of others" value={n('outbound_ok')}
            sub={`${n('outbound')} sent · ${n('outbound_failed')} failed`}
          />
          <StatTile icon={Inbox} tone="amber" label="Results queued" value={status.queued_results ?? 0} sub="for the hub" />
          <StatTile icon={Cable} tone="emerald" label="Connects" value={n('connects')} sub={`${n('disconnects')} drops`} />
        </div>

        <div className="grid grid-cols-1 gap-5 wide:grid-cols-2">
          <div>
            <div className={sectionCls}>Running here for other decks</div>
            {inbound.length === 0 ? <p className="text-xs text-zinc-500">Nothing right now.</p> : (
              <div className="space-y-1.5">
                {inbound.map((i) => (
                  <div key={i.concern_id} className={row}>
                    <span className="pt-0.5"><DirIcon dir="in" /></span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm text-zinc-200">
                        {i.agent || '?'} <span className="text-zinc-500">for {i.from || '?'}</span>
                      </span>
                      {i.last_event && <span className="block truncate text-xs text-zinc-500" title={i.last_event}>{i.last_event}</span>}
                    </span>
                    <span className="shrink-0 text-[11px] tabular-nums text-zinc-500">{fmtDuration(i.seconds)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
          <div>
            <div className={sectionCls}>Waiting on other decks</div>
            {outbound.length === 0 ? <p className="text-xs text-zinc-500">Nothing right now.</p> : (
              <div className="space-y-1.5">
                {outbound.map((o) => (
                  <div key={o.concern_id} className={row}>
                    <span className="pt-0.5"><DirIcon dir="out" /></span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-mono text-sm text-zinc-200">{o.target || '?'}</span>
                      {o.state && <span className="block truncate text-xs text-zinc-500">{o.state}</span>}
                    </span>
                    <span className="shrink-0 text-[11px] tabular-nums text-zinc-500">{fmtDuration(o.seconds)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <div>
          <div className={sectionCls}>Recent</div>
          {recent.length === 0 ? <p className="text-xs text-zinc-500">Nothing has gone over the link yet.</p> : (
            <ul className="max-h-96 divide-y divide-zinc-800/80 overflow-y-auto">
              {recent.map((r, i) => <RecentRow key={`${r.at}-${i}`} r={r} />)}
            </ul>
          )}
        </div>
      </div>
    </Card>
  )
}
