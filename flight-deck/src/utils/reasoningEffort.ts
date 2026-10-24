// The chat box's reasoning-effort picker: one scale for every model's
// thinking controls (the agent translates it — captain_claw/llm/reasoning.py).
// The agent tells us, per model, which levels it offers and which one it runs
// at when we send nothing; "auto" means send nothing.

export type ReasoningLevel = 'off' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max' | 'on'
export type EffortPick = ReasoningLevel | 'auto'

export interface ReasoningProfile {
  family: string
  levels: ReasoningLevel[]
  /** The level the model runs at when the request says nothing. */
  default: ReasoningLevel
  note?: string
}

const ORDER: ReasoningLevel[] = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
const RANK: Record<ReasoningLevel, number> = {
  off: 0, minimal: 1, low: 2, medium: 3, high: 4, xhigh: 5, max: 6, on: 3,
}

export const LEVEL_LABEL: Record<ReasoningLevel, string> = {
  off: 'Off',
  minimal: 'Minimal',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'Extra high',
  max: 'Max',
  on: 'On',
}

export const LEVEL_HINT: Record<ReasoningLevel, string> = {
  off: 'Answers straight away, no thinking',
  minimal: 'A quick thought before answering',
  low: 'Light thinking — faster and cheaper',
  medium: 'Balanced thinking',
  high: 'Thinks it through',
  xhigh: 'Deep thinking for hard tasks',
  max: 'Longest thinking — slowest, costs most',
  on: 'Thinks before answering',
}

function isLevel(v: unknown): v is ReasoningLevel {
  return typeof v === 'string' && v in RANK
}

/** The `reasoning` block of the agent's session info, or null (no picker). */
export function parseProfile(raw: unknown): ReasoningProfile | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  const levels = Array.isArray(r.levels) ? r.levels.filter(isLevel) : []
  if (levels.length === 0) return null
  return {
    family: String(r.family ?? ''),
    levels,
    default: isLevel(r.default) ? r.default : levels[0],
    note: r.note ? String(r.note) : undefined,
  }
}

/** *pick* if the model offers it, else the nearest level it does (a tie goes
 *  to the cheaper one) — the same rule the agent applies. */
export function clampLevel(pick: ReasoningLevel, profile: ReasoningProfile): ReasoningLevel {
  const { levels } = profile
  if (levels.includes(pick)) return pick
  if (levels.includes('on')) return pick === 'off' && levels.includes('off') ? 'off' : 'on'
  if (pick === 'on') {
    if (profile.default !== 'off' && levels.includes(profile.default)) return profile.default
    const on = levels.filter((l) => l !== 'off')
    return on[Math.floor(on.length / 2)] ?? levels[0]
  }
  return [...levels].sort((a, b) =>
    Math.abs(RANK[a] - RANK[pick]) - Math.abs(RANK[b] - RANK[pick]) || RANK[a] - RANK[b])[0]
}

/** What the agent will actually run at for *pick*. */
export function effectiveLevel(pick: EffortPick, profile: ReasoningProfile): ReasoningLevel {
  return pick === 'auto' ? profile.default : clampLevel(pick, profile)
}

export function sortLevels(levels: ReasoningLevel[]): ReasoningLevel[] {
  return [...levels].sort((a, b) => (a === 'on' ? 3.5 : ORDER.indexOf(a)) - (b === 'on' ? 3.5 : ORDER.indexOf(b)))
}
