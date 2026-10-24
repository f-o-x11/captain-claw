import { useEffect, useRef, useState } from 'react'
import { Brain, Check, ChevronDown } from 'lucide-react'
import { useReasoningStore } from '../../stores/reasoningStore'
import {
  LEVEL_HINT, LEVEL_LABEL, clampLevel, effectiveLevel, sortLevels,
  type EffortPick, type ReasoningLevel, type ReasoningProfile,
} from '../../utils/reasoningEffort'

const SHORT_LABEL: Record<ReasoningLevel, string> = {
  off: 'Off', minimal: 'Min', low: 'Low', medium: 'Med', high: 'High', xhigh: 'X-High', max: 'Max', on: 'On',
}

// How hard the agent thinks before answering — the composer's reasoning-effort
// picker (simple layout). It offers the levels the agent's model has, plus
// Auto (the model's own default); the pick is kept per agent and travels with
// every message sent to it.
export function EffortMenu({ agentId, profile, disabled, compact }: {
  agentId: string
  profile: ReasoningProfile
  disabled?: boolean
  /** Phone: a shorter label. */
  compact?: boolean
}) {
  const stored = useReasoningStore((s) => s.byAgent[agentId]) ?? 'auto'
  const setEffort = useReasoningStore((s) => s.setEffort)
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // A pick made for an earlier model shows as what this one will run at.
  const pick: EffortPick = stored === 'auto' ? 'auto' : clampLevel(stored, profile)
  const running = effectiveLevel(pick, profile)
  const levels = sortLevels(profile.levels)
  const choose = (p: EffortPick) => { setEffort(agentId, p); setOpen(false) }
  const label = pick === 'auto' ? 'Auto' : (compact ? SHORT_LABEL : LEVEL_LABEL)[pick]

  return (
    // Phone: the menu spans the composer box (its positioned ancestor), as
    // the skills menu does — anchored to the button it would run off-screen.
    <div className={compact ? 'shrink-0' : 'relative shrink-0'} ref={ref}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        disabled={disabled}
        aria-expanded={open}
        aria-haspopup="listbox"
        title={`Reasoning effort: ${pick === 'auto' ? `Auto (${LEVEL_LABEL[running]})` : LEVEL_LABEL[pick]} — how hard the agent thinks before answering`}
        className={`flex h-9 items-center gap-1.5 rounded-lg border px-2.5 text-sm transition-colors disabled:opacity-40 ${
          open
            ? 'border-violet-500/40 bg-violet-500/10 text-zinc-100'
            : pick !== 'auto'
              ? 'border-violet-500/30 text-zinc-200 hover:bg-zinc-800'
              : 'border-zinc-700 text-zinc-300 hover:bg-zinc-800'
        }`}
      >
        <Brain className={`h-4 w-4 ${pick === 'auto' ? 'text-zinc-500' : 'text-violet-500'}`} />
        <span>{label}</span>
        <ChevronDown className={`h-3.5 w-3.5 text-zinc-500 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div
          role="listbox"
          aria-label="Reasoning effort"
          className={`absolute bottom-full z-40 mb-2 overflow-hidden rounded-2xl border border-zinc-700 bg-zinc-900 shadow-2xl ${
            compact ? 'inset-x-0' : 'left-0 w-72'
          }`}
        >
          <div className="border-b border-zinc-800 px-4 py-2.5">
            <div className="text-sm font-medium text-zinc-200">Reasoning effort</div>
            <div className="text-xs text-zinc-500">How hard the agent thinks before it answers</div>
          </div>
          <div className="max-h-[min(55vh,26rem)] overflow-y-auto p-1.5">
            <EffortRow
              active={pick === 'auto'}
              title="Auto"
              hint={`The model's default — ${LEVEL_LABEL[profile.default].toLowerCase()}`}
              onClick={() => choose('auto')}
            />
            {levels.map((lvl) => (
              <EffortRow
                key={lvl}
                active={pick === lvl}
                title={LEVEL_LABEL[lvl]}
                hint={LEVEL_HINT[lvl]}
                tag={lvl === profile.default ? 'default' : undefined}
                onClick={() => choose(lvl)}
              />
            ))}
          </div>
          {profile.note && (
            <div className="border-t border-zinc-800 px-4 py-2 text-xs text-zinc-500">{profile.note}</div>
          )}
        </div>
      )}
    </div>
  )
}

function EffortRow({ active, title, hint, tag, onClick }: {
  active: boolean
  title: string
  hint: string
  tag?: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      role="option"
      aria-selected={active}
      onClick={onClick}
      className={`flex w-full items-center gap-3 rounded-xl px-2.5 py-2 text-left transition-colors ${
        active ? 'bg-violet-500/10' : 'hover:bg-zinc-800'
      }`}
    >
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-2 text-[15px] font-medium text-zinc-100">
          {title}
          {tag && <span className="rounded bg-zinc-800 px-1.5 py-px text-[10px] font-normal uppercase tracking-wide text-zinc-400">{tag}</span>}
        </span>
        <span className="block truncate text-sm text-zinc-400">{hint}</span>
      </span>
      {active && <Check className="h-4 w-4 shrink-0 text-violet-500" />}
    </button>
  )
}
