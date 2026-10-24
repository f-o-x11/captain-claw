import { create } from 'zustand'
import { queueSave, registerHydrator } from '../services/settingsSync'
import { useAuthStore } from './authStore'
import type { EffortPick } from '../utils/reasoningEffort'

// The reasoning effort picked in the chat box, per agent ("auto" is not
// stored: no entry means the model's own default). Synced with the user's
// deck settings, so the pick follows them to another device.

const STORAGE_KEY = 'fd:reasoning-effort'

function load(): Record<string, EffortPick> {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}') || {} } catch { return {} }
}
function save(byAgent: Record<string, EffortPick>) {
  const val = JSON.stringify(byAgent)
  try { localStorage.setItem(STORAGE_KEY, val) } catch { /* private mode */ }
  if (useAuthStore.getState().authEnabled) queueSave(STORAGE_KEY, val)
}

interface ReasoningStore {
  byAgent: Record<string, EffortPick>
  setEffort: (agentId: string, pick: EffortPick) => void
}

export const useReasoningStore = create<ReasoningStore>((set, get) => ({
  byAgent: load(),

  setEffort: (agentId, pick) => {
    if (!agentId) return
    const byAgent = { ...get().byAgent }
    if (pick === 'auto') delete byAgent[agentId]
    else byAgent[agentId] = pick
    save(byAgent)
    set({ byAgent })
  },
}))

export function effortFor(agentId: string): EffortPick {
  return useReasoningStore.getState().byAgent[agentId] ?? 'auto'
}

// Absent key → reset, so a previous user's picks don't carry into the next
// account (see pinnedStore).
registerHydrator((settings) => {
  const raw = settings[STORAGE_KEY]
  if (!raw) {
    useReasoningStore.setState({ byAgent: {} })
    return
  }
  try { useReasoningStore.setState({ byAgent: JSON.parse(raw) || {} }) } catch { /* ignore */ }
})
