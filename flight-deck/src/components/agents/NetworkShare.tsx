import { useEffect } from 'react'
import { Globe } from 'lucide-react'
import { useNetworkShareStore } from '../../stores/networkShareStore'
import type { BotportShareAgent } from '../../services/botportLink'

// "On the network": an agent its owner (or the admin) shared on this deck's
// BotPort network, so agents on other Flight Decks can ask it for help. The
// card's menu item, the kiosk's "⋯" menu and the badge read the same small
// store. `shareKey` is processShareKey(slug) or dockerShareKey(agentName).

export interface NetworkShareAction {
  show: boolean
  disabled: boolean
  label: string
  onClick: () => void
}

const HIDDEN: NetworkShareAction = { show: false, disabled: true, label: '', onClick: () => {} }

/** What sharing on the network offers for one agent: "Share on network…"
 *  (its owner, while the admin allows it), "Stop sharing on network" (its
 *  owner, or an admin), "On network (admin)" (the admin's list covers it), or
 *  nothing. Shared by the agent cards and the kiosk's "⋯" menu. */
export function networkShareAction(st: BotportShareAgent | undefined, ctx: {
  canShare: boolean
  admin: boolean
  deckName: string
  displayName: string
  setShared: (agent: BotportShareAgent, shared: boolean) => Promise<void>
  onError: (message: string) => void
}): NetworkShareAction & { hint: string } {
  const none = { ...HIDDEN, hint: '' }
  if (!st || !st.shareable) return none
  const fail = (e: unknown) => ctx.onError(e instanceof Error ? e.message : String(e))
  if (st.by_owner && (st.mine || ctx.admin)) {
    return {
      show: true, disabled: false, label: 'Stop sharing on network',
      hint: 'Agents on other Flight Decks can ask it for help — take it off',
      onClick: () => { ctx.setShared(st, false).catch(fail) },
    }
  }
  if (st.by_admin) {
    return { show: true, disabled: true, label: 'On network (admin)', hint: 'Your admin shares it on the network', onClick: () => {} }
  }
  if (!st.mine || !ctx.canShare) return none
  const as = ctx.deckName ? ` as ${ctx.displayName}@${ctx.deckName}` : ''
  return {
    show: true, disabled: false, label: 'Share on network…',
    hint: `Let agents on other Flight Decks ask it for help${as}`,
    onClick: () => {
      if (!confirm(
        `Share '${ctx.displayName}' on the BotPort network${as}?\n\n`
        + 'Agents on other Flight Decks can then ask it for help. Their requests run in a lane of their own: '
        + 'they never see your chats, memory or private data, cannot send email and cannot use the tools '
        + 'your admin blocks. You can stop sharing at any time.',
      )) return
      ctx.setShared(st, true).catch(fail)
    },
  }
}

/** The agent card's menu item for sharing its agent on the network. */
export function useNetworkShare(shareKey: string, displayName: string): NetworkShareAction {
  const fetch = useNetworkShareStore((s) => s.fetch)
  const ensure = useNetworkShareStore((s) => s.ensure)
  useEffect(() => { void fetch().then(() => ensure(shareKey)) }, [fetch, ensure, shareKey])
  const canShare = useNetworkShareStore((s) => s.canShare)
  const admin = useNetworkShareStore((s) => s.admin)
  const deckName = useNetworkShareStore((s) => s.deckName)
  const st = useNetworkShareStore((s) => s.agents[shareKey])
  const setShared = useNetworkShareStore((s) => s.setShared)
  return networkShareAction(st, { canShare, admin, deckName, displayName, setShared, onError: (m) => alert(m) })
}

/** "network" next to an agent's name while it is on the network. */
export function NetworkBadge({ shareKey }: { shareKey: string }) {
  const st = useNetworkShareStore((s) => s.agents[shareKey])
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
