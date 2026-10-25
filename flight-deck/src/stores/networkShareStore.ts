import { create } from 'zustand'
import { getBotportShares, setBotportShare, type BotportShareAgent, type BotportShares } from '../services/botportLink'

// This deck's BotPort network as an agent card sees it: may you share agents
// on it, and is this one on it (because its owner shared it, or the admin's
// list covers it)? Loaded when the first card mounts, again when a card's
// agent is missing (it was created since), on window focus and after every
// change. Keyed by kind so a docker and a process agent of one name stay
// apart: `process:<slug>` (process cards know the slug) and `docker#<name>`
// (container cards know the agent's name).

export const processShareKey = (slug: string) => `process:${(slug || '').toLowerCase()}`
export const dockerShareKey = (name: string) => `docker#${(name || '').toLowerCase()}`

interface NetworkShareState {
  loaded: boolean
  canShare: boolean
  admin: boolean
  deckName: string
  agents: Record<string, BotportShareAgent>
  fetch: (force?: boolean) => Promise<void>
  /** Refetch (at most every few seconds) when `key` is not known yet. */
  ensure: (key: string) => void
  /** Share an agent on the network or stop; rethrows the server's reason. */
  setShared: (agent: BotportShareAgent, shared: boolean) => Promise<void>
}

const MIN_REFETCH_MS = 5000
let inflight: Promise<void> | null = null
let lastFetch = 0

function view(r: BotportShares): Pick<NetworkShareState, 'loaded' | 'canShare' | 'admin' | 'deckName' | 'agents'> {
  const agents: Record<string, BotportShareAgent> = {}
  for (const a of r.agents) {
    if (a.kind === 'docker') agents[dockerShareKey(a.name)] = a
    else agents[processShareKey(a.slug || a.name)] = a
  }
  return { loaded: true, canShare: r.can_share, admin: r.admin, deckName: r.deck_name, agents }
}

export const useNetworkShareStore = create<NetworkShareState>((set, get) => ({
  loaded: false,
  canShare: false,
  admin: false,
  deckName: '',
  agents: {},
  fetch: (force = false) => {
    if (inflight) return inflight
    if (get().loaded && !force) return Promise.resolve()
    lastFetch = Date.now()
    inflight = getBotportShares()
      .then((r) => set(view(r)))
      .catch(() => { /* not loaded: the next card or focus tries again */ })
      .finally(() => { inflight = null })
    return inflight
  },
  ensure: (key) => {
    if (!key || get().agents[key] || Date.now() - lastFetch < MIN_REFETCH_MS) return
    void get().fetch(true)
  },
  setShared: async (agent, shared) => {
    set(view(await setBotportShare(agent.slug || agent.name, shared, agent.kind)))
    lastFetch = Date.now()
  },
}))

if (typeof window !== 'undefined') {
  window.addEventListener('focus', () => {
    if (useNetworkShareStore.getState().loaded && Date.now() - lastFetch > MIN_REFETCH_MS) {
      void useNetworkShareStore.getState().fetch(true)
    }
  })
}
