import { create } from 'zustand'
import { getBotportShares, setBotportShare, type BotportShareAgent, type BotportShares } from '../services/botportLink'

// This deck's BotPort network as an agent card sees it: may you share agents
// on it, and is this one on it (because its owner shared it, or the admin's
// list covers it)? Loaded once, when the first card mounts, and after every
// change. Keyed by slug and by name (lower case): process cards know a slug,
// container cards their agent's name.

interface NetworkShareState {
  loaded: boolean
  canShare: boolean
  deckName: string
  agents: Record<string, BotportShareAgent>
  fetch: (force?: boolean) => Promise<void>
  /** Share an agent on the network or stop; rethrows the server's reason. */
  setShared: (slug: string, shared: boolean) => Promise<void>
}

let inflight: Promise<void> | null = null

function view(r: BotportShares): Pick<NetworkShareState, 'loaded' | 'canShare' | 'deckName' | 'agents'> {
  const agents: Record<string, BotportShareAgent> = {}
  for (const a of r.agents) {
    if (a.name) agents[a.name.toLowerCase()] = a
    if (a.slug) agents[a.slug.toLowerCase()] = a
  }
  return { loaded: true, canShare: r.can_share, deckName: r.deck_name, agents }
}

export const useNetworkShareStore = create<NetworkShareState>((set, get) => ({
  loaded: false,
  canShare: false,
  deckName: '',
  agents: {},
  fetch: (force = false) => {
    if (inflight) return inflight
    if (get().loaded && !force) return Promise.resolve()
    inflight = getBotportShares()
      .then((r) => set(view(r)))
      .catch(() => set({ loaded: true }))
      .finally(() => { inflight = null })
    return inflight
  },
  setShared: async (slug, shared) => {
    set(view(await setBotportShare(slug, shared)))
  },
}))
