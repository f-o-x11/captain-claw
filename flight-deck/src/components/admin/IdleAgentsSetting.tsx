import { Switch } from './Switch'

// Idle agents go to sleep (Flight Deck's agent controller, agent_idle.py): a
// switch, and how long an agent may sit idle first. The Admin page and the
// console's Settings share it.

export const IDLE_AGENTS_TITLE = 'Put idle agents to sleep'
export const IDLE_AGENTS_HINT =
  'An agent that does nothing for this long is stopped to free the server’s memory. It wakes by itself when its chat is opened, a message reaches it (WhatsApp, Telegram, a device, another agent), and 5 minutes before a scheduled task or a job of its own cron. Agents with a Telegram, Slack or Discord bot of their own stay on, as do the workers of a run and Iskra beings.'

const DEFAULT_MINUTES = 30
const CHOICES = [15, 30, 60, 120, 240, 480, 1440]

function minutesLabel(m: number): string {
  if (m < 60) return `${m} minutes`
  if (m % 60 === 0) return m === 60 ? '1 hour' : `${m / 60} hours`
  return `${Math.floor(m / 60)} h ${m % 60} min`
}

export function IdleAgentsSetting({ minutes, busy, onSave }: {
  minutes: number
  busy: boolean
  onSave: (minutes: number) => void
}) {
  const on = minutes > 0
  // A time set outside the list (the API takes 10 minutes to a week) stays pickable.
  const choices = on && !CHOICES.includes(minutes) ? [...CHOICES, minutes].sort((a, b) => a - b) : CHOICES
  return (
    <div className="flex items-center gap-3">
      {on && (
        <select
          value={minutes}
          disabled={busy}
          onChange={(e) => onSave(Number(e.target.value))}
          aria-label="Idle time before an agent sleeps"
          className="rounded-lg border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-violet-500/60 focus:outline-none disabled:opacity-60"
        >
          {choices.map((m) => <option key={m} value={m}>after {minutesLabel(m)}</option>)}
        </select>
      )}
      <Switch
        on={on}
        disabled={busy}
        label={IDLE_AGENTS_TITLE}
        onToggle={() => onSave(on ? 0 : DEFAULT_MINUTES)}
      />
    </div>
  )
}
