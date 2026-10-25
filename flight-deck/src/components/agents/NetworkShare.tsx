import { useEffect } from 'react'
import { Globe } from 'lucide-react'
import { useNetworkShareStore } from '../../stores/networkShareStore'

// "On the network": an agent its owner (or the admin) shared on this deck's
// BotPort network, so agents on other Flight Decks can ask it for help. The
// card's menu item and badge read the same small store.

export interface NetworkShareAction {
  show: boolean
  disabled: boolean
  label: string
  onClick: () => void
}

/** The card's menu item for sharing `key` (slug or agent name) on the network. */
export function useNetworkShare(key: string, displayName: string): NetworkShareAction {
  const fetch = useNetworkShareStore((s) => s.fetch)
  useEffect(() => { void fetch() }, [fetch])
  const canShare = useNetworkShareStore((s) => s.canShare)
  const deckName = useNetworkShareStore((s) => s.deckName)
  const st = useNetworkShareStore((s) => s.agents[(key || '').toLowerCase()])
  const setShared = useNetworkShareStore((s) => s.setShared)

  if (!st || !st.shareable || (!canShare && !st.by_owner)) {
    return { show: false, disabled: true, label: '', onClick: () => {} }
  }
  if (st.by_admin) {
    return { show: true, disabled: true, label: 'On network (admin)', onClick: () => {} }
  }
  const sharing = !st.by_owner
  return {
    show: true,
    disabled: false,
    label: sharing ? 'Share on network…' : 'Stop sharing on network',
    onClick: () => {
      if (sharing && !confirm(
        `Share '${displayName}' on the BotPort network${deckName ? ` as ${displayName}@${deckName}` : ''}?\n\n`
        + 'Agents on other Flight Decks can then ask it for help. Their requests run in a lane of their own: '
        + 'they never see your chats, memory or private data, cannot send email and cannot use the tools '
        + 'your admin blocks. You can stop sharing at any time.',
      )) return
      setShared(st.slug || st.name, sharing).catch((e: unknown) => {
        alert(e instanceof Error ? e.message : String(e))
      })
    },
  }
}

/** "network" next to an agent's name while it is on the network. */
export function NetworkBadge({ agentKey }: { agentKey: string }) {
  const st = useNetworkShareStore((s) => s.agents[(agentKey || '').toLowerCase()])
  const deckName = useNetworkShareStore((s) => s.deckName)
  if (!st?.exposed) return null
  const who = st.by_owner && st.by_admin ? 'its owner and the admin' : st.by_owner ? 'its owner' : 'the admin'
  return (
    <span
      className="flex shrink-0 items-center gap-0.5 rounded border border-cyan-500/25 bg-cyan-500/15 px-1 py-0.5 text-[9px] font-medium text-cyan-700 dark:text-cyan-300"
      title={`On the BotPort network${deckName ? ` as ${st.name}@${deckName}` : ''} (shared by ${who}). Agents on other Flight Decks can ask it for help.`}
    >
      <Globe className="h-2.5 w-2.5" />network
    </span>
  )
}
