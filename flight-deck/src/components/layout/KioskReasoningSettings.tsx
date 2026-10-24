import { useEffect, useState } from 'react'
import { AlertTriangle, Brain, Check, Loader2 } from 'lucide-react'
import { getAgentReasoning, setAgentReasoning, type AgentReasoning, type AgentReasoningSettings } from '../../services/docker'
import {
  LEVEL_LABEL, clampLevel, parseProfile, sortLevels,
  type ReasoningLevel, type ReasoningProfile,
} from '../../utils/reasoningEffort'

// ── Settings: reasoning effort by where a message comes from ──
//
// The web chat has its own picker in the chat box. Everything else — WhatsApp,
// Telegram, the app, glasses, a device, a scheduled task, another agent —
// takes these: one default, and a level per source where it should differ.
// The agent reads them afresh, so a change applies from its next message.

const AUTO = 'auto'
const INHERIT = ''

/** A stored value as the person reads it on this model. */
function levelText(value: string, profile: ReasoningProfile | null): string {
  if (value === AUTO || !value) return profile ? `Model default · ${LEVEL_LABEL[profile.default]}` : 'Model default'
  const lvl = value as ReasoningLevel
  if (!profile || profile.levels.includes(lvl)) return LEVEL_LABEL[lvl] ?? value
  return `${LEVEL_LABEL[lvl] ?? value} → ${LEVEL_LABEL[clampLevel(lvl, profile)]} on this model`
}

/** The level a source set to Default runs at. */
function defaultLevel(value: string, profile: ReasoningProfile): string {
  if (value === AUTO || !value) return LEVEL_LABEL[profile.default]
  return LEVEL_LABEL[clampLevel(value as ReasoningLevel, profile)]
}

export function KioskReasoningSettings({ kind, agentKey }: { kind: 'process' | 'docker'; agentKey: string }) {
  const [data, setData] = useState<AgentReasoning | null>(null)
  const [settings, setSettings] = useState<AgentReasoningSettings>({ channels: {} })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState('')
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    let live = true
    getAgentReasoning(kind, agentKey)
      .then((r) => { if (live) { setData(r); setSettings(r.settings || { channels: {} }) } })
      .catch((e) => { if (live) setError(`Couldn't load its reasoning settings: ${e instanceof Error ? e.message : String(e)}`) })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [kind, agentKey])

  const profile = parseProfile(data?.profile)
  const levels = profile ? sortLevels(profile.levels) : []

  const save = async (key: string, change: Parameters<typeof setAgentReasoning>[2], optimistic: AgentReasoningSettings) => {
    const was = settings
    setSettings(optimistic); setSaving(key); setError(''); setSaved(false)
    try {
      const res = await setAgentReasoning(kind, agentKey, change)
      setSettings(res.settings)
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (e) {
      setSettings(was)
      setError(`Couldn't save: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSaving('')
    }
  }

  const pickDefault = (value: string) => {
    const next = { ...settings }
    if (value === AUTO) delete next.default
    else next.default = value
    void save('default', value === AUTO ? { clear_default: true } : { default: value }, next)
  }

  const pickSource = (source: string, value: string) => {
    const channels = { ...settings.channels }
    if (value === INHERIT) delete channels[source]
    else channels[source] = value
    void save(source, { channels: { [source]: value === INHERIT ? null : value } }, { ...settings, channels })
  }

  const defaultValue = settings.default || AUTO

  return (
    <section>
      <h3 className="mb-1 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-400">
        <Brain className="h-3.5 w-3.5" />
        Reasoning effort
        {saving && <Loader2 className="h-3 w-3 animate-spin text-zinc-500" />}
        {saved && <span className="flex items-center gap-0.5 normal-case tracking-normal text-emerald-600 dark:text-emerald-400"><Check className="h-3 w-3" />saved</span>}
      </h3>
      <p className="mb-3 text-[11px] leading-relaxed text-zinc-500">
        How hard it thinks before answering messages that don't come from the web chat — the chat box has its own
        picker. Applies from its next message.
      </p>

      {loading ? (
        <div className="flex items-center py-4 text-xs text-zinc-500"><Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" /> Loading…</div>
      ) : !profile ? (
        <p className="rounded-lg border border-zinc-800 bg-zinc-900/60 px-3 py-2.5 text-xs text-zinc-400">
          {data?.model.model
            ? <>Its model ({data.model.provider} / {data.model.model}) has no reasoning levels to choose from.</>
            : <>Its model has no reasoning levels to choose from.</>}
        </p>
      ) : (
        <>
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-3">
            <LevelRow
              label="Default"
              hint="For every source below set to Default"
              value={defaultValue}
              onChange={pickDefault}
              disabled={!!saving}
              options={[
                { value: AUTO, text: levelText(AUTO, profile) },
                ...levels.map((l) => ({ value: l, text: LEVEL_LABEL[l] })),
                ...(defaultValue !== AUTO && !levels.includes(defaultValue as ReasoningLevel)
                  ? [{ value: defaultValue, text: levelText(defaultValue, profile) }] : []),
              ]}
              strong
            />
          </div>
          {profile.note && <p className="mt-1.5 text-[11px] text-zinc-500">{profile.note}</p>}

          <div className="mt-3 space-y-3">
            {(data?.catalog ?? []).map((group) => (
              <div key={group.title}>
                <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">{group.title}</div>
                <div className="divide-y divide-zinc-800/70 rounded-lg border border-zinc-800">
                  {group.sources.map((src) => {
                    const value = settings.channels[src.key] ?? INHERIT
                    return (
                      <div key={src.key} className="px-3 py-1.5">
                        <LevelRow
                          label={src.label}
                          value={value}
                          onChange={(v) => pickSource(src.key, v)}
                          disabled={!!saving}
                          options={[
                            { value: INHERIT, text: `Default · ${defaultLevel(defaultValue, profile)}` },
                            { value: AUTO, text: levelText(AUTO, profile) },
                            ...levels.map((l) => ({ value: l, text: LEVEL_LABEL[l] })),
                            ...(value && value !== AUTO && !levels.includes(value as ReasoningLevel)
                              ? [{ value, text: levelText(value, profile) }] : []),
                          ]}
                        />
                      </div>
                    )
                  })}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
      {error && (
        <div className="mt-2 flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-300">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {error}
        </div>
      )}
    </section>
  )
}

function LevelRow({ label, hint, value, options, onChange, disabled, strong = false }: {
  label: string
  hint?: string
  value: string
  options: { value: string; text: string }[]
  onChange: (value: string) => void
  disabled?: boolean
  strong?: boolean
}) {
  return (
    <label className="flex items-center gap-3">
      <span className="min-w-0 flex-1">
        <span className={`block leading-snug ${strong ? 'text-sm font-medium text-zinc-100' : 'text-xs text-zinc-200'}`}>{label}</span>
        {hint && <span className="block truncate text-[11px] text-zinc-500">{hint}</span>}
      </span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className={`w-40 shrink-0 rounded-lg border border-zinc-700 bg-zinc-950 px-2 text-zinc-200 focus:border-violet-500/60 focus:outline-none disabled:cursor-wait wide:w-52 ${
          strong ? 'py-2 text-sm' : 'py-1.5 text-xs'
        }`}
      >
        {options.map((o) => <option key={o.value || 'inherit'} value={o.value}>{o.text}</option>)}
      </select>
    </label>
  )
}
