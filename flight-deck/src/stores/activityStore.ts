import { create } from 'zustand'

/** The kiosk's Activity dialog (the tool-call register): whether it is open,
 *  and the agent it opens filtered to — a process agent by its slug, a
 *  container by its id ('' = every agent). */
interface ActivityStore {
  open: boolean
  agentSlug: string
  agentRef: string
  openActivity: (agent?: { slug?: string; ref?: string }) => void
  close: () => void
}

export const useActivityStore = create<ActivityStore>((set) => ({
  open: false,
  agentSlug: '',
  agentRef: '',
  openActivity: (agent) => set({ open: true, agentSlug: agent?.slug ?? '', agentRef: agent?.ref ?? '' }),
  close: () => set({ open: false, agentSlug: '', agentRef: '' }),
}))
