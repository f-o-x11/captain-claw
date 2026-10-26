import { useEffect, useState } from 'react'
import { Bell, X, CheckCheck, Trash2, AlertTriangle, Info, CheckCircle2, AlertCircle, Loader2 } from 'lucide-react'
import { useNotificationStore, type Notification, type NotificationType } from '../../stores/notificationStore'
import { fetchWaitingProposalIds, resolveProposal } from '../../services/notifications'
import { useAuthStore, selectKioskLocked } from '../../stores/authStore'
import { useChatStore } from '../../stores/chatStore'
import { useSharedAgentStore } from '../../stores/sharedAgentStore'
import { useUIStore } from '../../stores/uiStore'
import {
  SHARED_SECTION_ID, sharedNotificationAction, type SharedNotificationAction,
} from '../../utils/sharedAgent'

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const s = Math.floor(diff / 1000)
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

const typeIcon: Record<NotificationType, typeof Info> = {
  info: Info,
  success: CheckCircle2,
  warning: AlertTriangle,
  error: AlertCircle,
}
const typeColor: Record<NotificationType, string> = {
  info: 'text-blue-400',
  success: 'text-emerald-400',
  warning: 'text-amber-400',
  error: 'text-red-400',
}

// `align` picks which edge of the bell the dropdown hangs from: 'right' (the
// default — the bell sits at the right end of the top bar) or 'left' (the
// bell sits in a left-hand column, so the panel must open towards the page).
export function NotificationBell({ align = 'right' }: { align?: 'left' | 'right' } = {}) {
  const { unreadCount } = useNotificationStore()
  const [open, setOpen] = useState(false)

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className={`relative rounded p-1.5 transition-colors ${
          open ? 'bg-zinc-800 text-zinc-200' : 'text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300'
        }`}
        title="Notifications"
      >
        <Bell className="h-4 w-4" />
        {unreadCount > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-red-500 text-[9px] font-bold text-white">
            {unreadCount > 9 ? '9+' : unreadCount}
          </span>
        )}
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className={`fixed left-2 right-2 top-14 z-50 sm:absolute sm:top-full sm:mt-1 sm:w-[360px] rounded-xl border border-zinc-800 bg-zinc-950 shadow-2xl ${align === 'left' ? 'sm:right-auto sm:left-0' : 'sm:left-auto sm:right-0'}`}>
            <NotificationDropdown onClose={() => setOpen(false)} />
          </div>
        </>
      )}
    </div>
  )
}

// The Agent Desktop may still be mounting after setView('desktop'): look for
// the "Shared with me" section for a moment before giving up.
function revealSharedSection(tries = 20): void {
  const el = document.getElementById(SHARED_SECTION_ID)
  if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); return }
  if (tries > 0) setTimeout(() => revealSharedSection(tries - 1), 50)
}

function NotificationDropdown({ onClose }: { onClose: () => void }) {
  const { notifications, markRead, markAllRead, remove, clear } = useNotificationStore()
  const [filter, setFilter] = useState<NotificationType | 'all'>('all')
  // A notice about an agent someone shared with you leads to it. The list is
  // only polled where agents are shown, so refresh it when the bell opens.
  const sharedAgents = useSharedAgentStore((s) => s.agents)
  const kioskLocked = useAuthStore(selectKioskLocked)
  const layoutMode = useUIStore((s) => s.layoutMode)
  const layout = kioskLocked || layoutMode === 'simple' ? 'simple' : 'full'
  const aboutAgents = notifications.some((n) => n.refType === 'agent')
  useEffect(() => {
    if (aboutAgents) void useSharedAgentStore.getState().fetch()
  }, [aboutAgents])
  // Autonomous Work proposals: which still wait for an answer (those get
  // Approve / Reject here — the kiosk has no other place to answer them).
  const aboutProposals = notifications.some((n) => n.refType === 'autonomy_action')
  const [waiting, setWaiting] = useState<Set<string> | null>(null)
  const loadWaiting = () => {
    fetchWaitingProposalIds().then(setWaiting).catch(() => setWaiting(null))
  }
  useEffect(() => {
    if (aboutProposals) loadWaiting()
  }, [aboutProposals])

  const activate = (action: SharedNotificationAction) => {
    if (action.kind === 'none') return
    onClose()
    if (action.kind === 'chat') {
      useChatStore.getState().openSharedChat(action.agentRef, action.name, action.ownerName)
    } else if (layout === 'full') {
      useUIStore.getState().setView('desktop')
      revealSharedSection()
    } else {
      useUIStore.getState().setSimpleLeftOpen(true)
    }
  }

  const filtered = filter === 'all' ? notifications : notifications.filter((n) => n.type === filter)

  return (
    <div className="flex max-h-[480px] flex-col">
      <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
        <span className="text-xs font-semibold">Notifications</span>
        <div className="flex items-center gap-1">
          <button onClick={markAllRead} className="rounded px-1.5 py-0.5 text-[10px] text-zinc-500 hover:text-zinc-300" title="Mark all read">
            <CheckCheck className="h-3 w-3" />
          </button>
          {notifications.length > 0 && (
            <button onClick={clear} className="rounded px-1.5 py-0.5 text-[10px] text-zinc-500 hover:text-red-400" title="Clear all">
              <Trash2 className="h-3 w-3" />
            </button>
          )}
        </div>
      </div>

      {/* Type filter */}
      <div className="flex gap-0.5 border-b border-zinc-800 px-3 py-1.5">
        {(['all', 'info', 'success', 'warning', 'error'] as const).map((t) => (
          <button
            key={t}
            onClick={() => setFilter(t)}
            className={`rounded px-1.5 py-0.5 text-[10px] font-medium capitalize ${
              filter === t ? 'bg-violet-600/20 text-violet-400' : 'text-zinc-500 hover:text-zinc-300'
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto">
        {filtered.length === 0 ? (
          <div className="px-3 py-6 text-center text-xs text-zinc-600">No notifications</div>
        ) : (
          filtered.map((n) => (
            <NotificationItem
              key={n.id}
              notification={n}
              action={sharedNotificationAction(n.refType, n.refId, sharedAgents, layout)}
              proposal={n.refType === 'autonomy_action' && n.refId && waiting
                ? (waiting.has(n.refId) ? 'waiting' : 'answered') : undefined}
              onAnswered={() => { markRead(n.id); loadWaiting() }}
              onRead={() => markRead(n.id)}
              onActivate={activate}
              onRemove={() => remove(n.id)}
            />
          ))
        )}
      </div>
    </div>
  )
}

// A proposal's answer, from the bell: Approve sends it to your agent, Reject
// drops it (and teaches Autonomous Work not to suggest its like).
function ProposalButtons({ id, onAnswered }: { id: string; onAnswered: () => void }) {
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const answer = async (approve: boolean) => {
    setBusy(approve ? 'approve' : 'reject'); setError(null)
    try {
      await resolveProposal(id, approve)
      onAnswered()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }
  const btn = 'flex items-center gap-1 rounded px-2 py-0.5 text-[10px] font-medium disabled:opacity-50'
  return (
    <div className="mt-1" onClick={(e) => e.stopPropagation()}>
      <div className="flex items-center gap-1.5">
        <button disabled={busy !== null} onClick={() => answer(true)}
          className={`${btn} bg-emerald-600 text-white hover:bg-emerald-500`}>
          {busy === 'approve' && <Loader2 className="h-3 w-3 animate-spin" />} Approve
        </button>
        <button disabled={busy !== null} onClick={() => answer(false)}
          className={`${btn} bg-zinc-800 text-zinc-300 hover:bg-zinc-700`}>
          {busy === 'reject' && <Loader2 className="h-3 w-3 animate-spin" />} Reject
        </button>
      </div>
      {error && <p className="mt-0.5 text-[10px] text-red-600 dark:text-red-400">{error}</p>}
    </div>
  )
}

function NotificationItem({ notification: n, action, proposal, onAnswered, onRead, onActivate, onRemove }: {
  notification: Notification
  action: SharedNotificationAction
  proposal?: 'waiting' | 'answered'
  onAnswered: () => void
  onRead: () => void
  onActivate: (action: SharedNotificationAction) => void
  onRemove: () => void
}) {
  const Icon = typeIcon[n.type]
  const color = typeColor[n.type]
  // Autonomous Work's text (a nudge, a proposal's why) keeps its lines; a
  // long one opens up on a click.
  const autonomy = n.refType === 'autonomy_action' || n.refType === 'autonomy_nudge'
  const [expanded, setExpanded] = useState(false)

  return (
    <div
      className={`group flex items-start gap-2 border-b border-zinc-800/40 px-3 py-2 hover:bg-zinc-900/30 cursor-pointer ${
        !n.read ? 'bg-zinc-900/20' : ''
      }`}
      onClick={() => { onRead(); onActivate(action); if (autonomy) setExpanded((v) => !v) }}
      title={action.kind === 'chat' ? `Chat with ${action.name}` : undefined}
    >
      <Icon className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${color}`} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="text-xs font-medium text-zinc-200">{n.title}</span>
          {!n.read && <span className="h-1.5 w-1.5 rounded-full bg-violet-500" />}
        </div>
        <p className={`text-[10px] text-zinc-500 mt-0.5 ${autonomy ? `whitespace-pre-line ${expanded ? '' : 'line-clamp-6'}` : ''}`}>
          {n.message}
        </p>
        {proposal === 'waiting' && n.refId && <ProposalButtons id={n.refId} onAnswered={onAnswered} />}
        {proposal === 'answered' && <p className="mt-0.5 text-[10px] text-zinc-600">No longer waiting.</p>}
        {action.kind !== 'none' && (
          <p className="mt-0.5 text-[10px] text-sky-700 dark:text-sky-400">{action.hint}</p>
        )}
        <div className="flex items-center gap-1.5 mt-0.5 text-[10px] text-zinc-600">
          {n.agentName && <span>{n.agentName}</span>}
          <span>{relativeTime(n.createdAt)}</span>
        </div>
      </div>
      <button
        onClick={(e) => { e.stopPropagation(); onRemove() }}
        className="rounded p-0.5 text-zinc-600 opacity-0 group-hover:opacity-100 hover:text-red-400 transition-opacity"
      >
        <X className="h-3 w-3" />
      </button>
    </div>
  )
}
