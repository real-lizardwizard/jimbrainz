import { useEffect, useMemo, useState } from 'preact/hooks'

import {
  getServerSettings, saveServerSettings,
  type ServerSetting, type ServerSettings, type SettingUpdate,
} from '../api/settings'
import type { FormatPreference } from '../api/types'
import { LoadingPanel } from './Loading'
import {
  useDownloadDefaults, usePreferences,
  type DownloadDefaults, type Preferences,
} from '../state/persisted'

/**
 * The settings tab.
 *
 * EVERYTHING HERE IS A DRAFT UNTIL YOU PRESS SAVE.
 *
 * The two halves still have different natures - preferences live in this browser, server
 * settings live in the database and are laid over the environment - but they are edited
 * through one model and committed by one button. That is deliberate. Two halves with two
 * different save semantics on one screen is how you end up unsure whether the thing you just
 * changed took, which was the actual complaint about the previous version: preferences saved
 * instantly and silently, so there was no evidence anything had happened.
 *
 * SERVER SETTINGS ARE EDITABLE, with two exceptions that are shown and explained rather than
 * hidden - DB_PATH is the database the overrides live in, and PUID/PGID are consumed by the
 * entrypoint before Python starts. Saving one stores an OVERRIDE that wins over the
 * environment, applies live without a restart, and is announced as an override with a revert
 * control. See the long note at the top of src/routes/settings.py for why the override has to
 * win rather than the environment.
 */

const FORMAT_PREFERENCES: { id: FormatPreference; name: string; note: string }[] = [
  { id: 'any', name: 'Any format', note: 'Rank on everything else; format is not considered' },
  { id: 'prefer_lossless', name: 'Prefer lossless', note: 'FLAC scores higher, MP3 still eligible' },
  { id: 'lossless_only', name: 'Lossless only', note: 'Lossy candidates are excluded outright' },
]

/* ============================================================================
 * Small shared pieces
 * ==========================================================================*/

function Section({
  title,
  note,
  children,
}: {
  title: string
  note?: preact.ComponentChildren
  children: preact.ComponentChildren
}) {
  return (
    <section class="settings-section">
      <h3 class="settings-section-title">{title}</h3>
      {note ? <p class="settings-section-note">{note}</p> : null}
      <div class="settings-section-body">{children}</div>
    </section>
  )
}

/** A labelled row. The label is the hit target, so the whole row is clickable. */
function Row({
  label,
  hint,
  control,
  htmlFor,
}: {
  label: string
  hint?: string
  control: preact.ComponentChildren
  htmlFor?: string
}) {
  return (
    <div class="settings-row">
      <div class="settings-row-text">
        <label class="settings-row-label" for={htmlFor}>
          {label}
        </label>
        {hint ? <span class="settings-row-hint">{hint}</span> : null}
      </div>
      <div class="settings-row-control">{control}</div>
    </div>
  )
}

/**
 * A plain checkbox. It was a sliding switch; James asked for checkboxes "to keep with the
 * nostalgic theme", and the switch was the one control on the page that said phone rather than
 * desktop. The row puts it BEFORE its label (see .settings-row:has(.settings-check)), where a
 * desktop checkbox lives, rather than off at the far edge where the switch sat.
 */
function Checkbox({
  id,
  checked,
  onChange,
}: {
  id: string
  checked: boolean
  onChange: (next: boolean) => void
}) {
  return (
    <input
      id={id}
      type="checkbox"
      class="settings-check"
      checked={checked}
      onChange={(e) => onChange((e.currentTarget as HTMLInputElement).checked)}
    />
  )
}

/* ============================================================================
 * Server configuration - now editable
 * ==========================================================================*/

function SettingRow({
  setting,
  modes,
  draft,
  onEdit,
  onRevert,
}: {
  setting: ServerSetting
  modes: Record<string, string>
  /** The pending edit: a string, `null` for "revert", or undefined when untouched. */
  draft: string | null | undefined
  onEdit: (key: string, value: string) => void
  onRevert: (key: string) => void
}) {
  const edited = draft !== undefined
  const reverting = draft === null

  /*
   * A secret is never sent to the browser, so there is no current value to put in the field.
   * The placeholder carries whether one exists, and typing replaces it wholesale - which is
   * the only thing you can do with a value you cannot read.
   */
  const value = typeof draft === 'string' ? draft : setting.secret ? '' : (setting.value ?? '')

  const control = !setting.editable ? (
    <span class="settings-env-locked" title={setting.locked_reason ?? undefined}>Locked</span>
  ) : setting.key === 'ORGANIZE_MODE' ? (
    <select
      class="settings-env-input"
      value={value}
      onChange={(e) => onEdit(setting.key, (e.currentTarget as HTMLSelectElement).value)}
    >
      {Object.keys(modes).map((mode) => (
        <option key={mode} value={mode}>{mode}</option>
      ))}
    </select>
  ) : (
    <input
      class="settings-env-input"
      type={setting.secret ? 'password' : 'text'}
      value={value}
      spellcheck={false}
      autocomplete="off"
      placeholder={setting.secret ? (setting.value ? '•••••••• (set)' : 'not set') : 'not set'}
      onInput={(e) => onEdit(setting.key, (e.currentTarget as HTMLInputElement).value)}
    />
  )

  return (
    <div
      class={`settings-env settings-env-${setting.status}${edited ? ' is-edited' : ''}`}
    >
      <div class="settings-env-head">
        <code class="settings-env-key">{setting.key}</code>

        {setting.overridden && !edited ? (
          <span class="settings-env-source is-override" title="Set here, overriding the environment">
            Set here
          </span>
        ) : setting.source ? (
          <span class="settings-env-source" title="Which file supplied this value">
            From {setting.source}
          </span>
        ) : null}

        {edited ? <span class="settings-env-badge is-edited">Unsaved</span> : null}
        {setting.status === 'error' && !edited ? (
          <span class="settings-env-badge">Needs attention</span>
        ) : null}
      </div>

      <div class="settings-env-control">{control}</div>

      <div class="settings-env-effect">{setting.effect}</div>

      {/*
        A locked setting explains itself. A disabled control with no reason attached reads as
        a bug rather than as a decision, and this one has a good reason.
      */}
      {!setting.editable && setting.locked_reason ? (
        <div class="settings-env-effect">{setting.locked_reason}</div>
      ) : null}

      {/*
        An override that cannot be undone becomes state nobody remembers setting. Reverting
        deletes the stored row rather than writing the environment's value back, so the
        setting follows the compose file again from then on.
      */}
      {setting.overridden && !reverting ? (
        <button type="button" class="settings-env-revert" onClick={() => onRevert(setting.key)}>
          Revert to the environment{setting.env_value ? ` (${setting.env_value})` : ''}
        </button>
      ) : null}

      {reverting ? <div class="settings-env-effect">Will revert to the environment on save.</div> : null}

      {setting.detail && !edited ? <div class="settings-env-detail">{setting.detail}</div> : null}
    </div>
  )
}

/* ============================================================================
 * The tab
 * ==========================================================================*/

export function SettingsView({ active }: { active: boolean }) {
  const [defaults, commitDefaults] = useDownloadDefaults()
  const [prefs, commitPrefs, resetPrefs] = usePreferences()

  const [server, setServer] = useState<ServerSettings | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  /*
   * The drafts. Held separately from the committed values rather than as a copy of them, so
   * "has anything changed" is a comparison rather than a flag somebody has to remember to
   * set - a dirty flag maintained by hand is a dirty flag that eventually lies.
   */
  const [draftPrefs, setDraftPrefs] = useState<Partial<Preferences>>({})
  const [draftDefaults, setDraftDefaults] = useState<Partial<DownloadDefaults>>({})
  const [draftEnv, setDraftEnv] = useState<Record<string, string | null>>({})

  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [justSaved, setJustSaved] = useState(false)

  //? What the controls should show: the draft where there is one, the committed value where
  //? there isn't. One expression, so a control can never render a value the draft disagrees
  //? with.
  const shownPrefs = { ...prefs, ...draftPrefs }
  const shownDefaults = { ...defaults, ...draftDefaults }

  const dirtyCount =
    Object.keys(draftPrefs).length + Object.keys(draftDefaults).length + Object.keys(draftEnv).length

  const settingsByKey = useMemo(() => {
    const map = new Map<string, ServerSetting>()
    for (const group of server?.groups ?? []) {
      for (const setting of group.settings) map.set(setting.key, setting)
    }
    return map
  }, [server])

  /*
   * Fetched when the tab is opened, not on mount: this walks the filesystem to check every
   * configured path, and the settings tab is the least-visited of the three. Refetched on
   * each open so a volume you just fixed shows as fixed without a reload.
   */
  useEffect(() => {
    if (!active) return

    let cancelled = false
    setLoading(true)

    getServerSettings()
      .then((data) => {
        if (cancelled) return
        setServer(data)
        setError(null)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not read settings')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [active])

  /** Record a preference edit, dropping it from the draft if it matches the committed value. */
  function editPref<K extends keyof Preferences>(key: K, value: Preferences[K]) {
    setJustSaved(false)
    setDraftPrefs((current) => {
      const next = { ...current, [key]: value }
      //? Setting something back to what it already was is not a change. Without this, undoing
      //? an edit by hand leaves the save button lit with nothing to save.
      if (prefs[key] === value) delete next[key]
      return next
    })
  }

  function editDefault<K extends keyof DownloadDefaults>(key: K, value: DownloadDefaults[K]) {
    setJustSaved(false)
    setDraftDefaults((current) => {
      const next = { ...current, [key]: value }
      if (defaults[key] === value) delete next[key]
      return next
    })
  }

  function editEnv(key: string, value: string) {
    setJustSaved(false)
    setDraftEnv((current) => {
      const next = { ...current, [key]: value }
      const committed = settingsByKey.get(key)
      //? Secrets have no readable current value, so any typing is a change by definition.
      if (committed && !committed.secret && (committed.value ?? '') === value) delete next[key]
      return next
    })
  }

  function revertEnv(key: string) {
    setJustSaved(false)
    setDraftEnv((current) => ({ ...current, [key]: null }))
  }

  function discard() {
    setDraftPrefs({})
    setDraftDefaults({})
    setDraftEnv({})
    setSaveError(null)
  }

  async function save() {
    setSaving(true)
    setSaveError(null)

    try {
      /*
       * Server settings first, and only commit the local half if they succeed. The server can
       * refuse the batch (an invalid URL, an unwritable database); committing preferences
       * anyway would leave the screen showing a save that half happened.
       */
      const updates: SettingUpdate[] = Object.entries(draftEnv).map(([key, value]) => ({ key, value }))

      if (updates.length) {
        const fresh = await saveServerSettings(updates)
        setServer(fresh)
      }

      if (Object.keys(draftPrefs).length) commitPrefs(draftPrefs)
      if (Object.keys(draftDefaults).length) commitDefaults(draftDefaults)

      setDraftPrefs({})
      setDraftDefaults({})
      setDraftEnv({})
      setJustSaved(true)
    } catch (e: unknown) {
      setSaveError(e instanceof Error ? e.message : 'Could not save')
    } finally {
      setSaving(false)
    }
  }

  if (!active) return null

  return (
    <div id="settings-content">
      <div class="settings-scroll scrollable">
        <Section
          title="Downloads"
          note="Applied when jimbrainz ranks Soulseek candidates and when you grab one."
        >
          <div class="settings-radio-group" role="radiogroup" aria-label="Format preference">
            {FORMAT_PREFERENCES.map((format) => (
              <label
                key={format.id}
                class={`settings-radio${
                  shownDefaults.formatPreference === format.id ? ' active' : ''
                }`}
              >
                <input
                  type="radio"
                  name="format-preference"
                  value={format.id}
                  checked={shownDefaults.formatPreference === format.id}
                  onChange={() => editDefault('formatPreference', format.id)}
                />
                <span class="settings-radio-name">{format.name}</span>
                <span class="settings-radio-note">{format.note}</span>
              </label>
            ))}
          </div>

          <Row
            label="Auto-grab best match"
            hint="Queue the top-ranked candidate immediately instead of opening the panel to choose"
            htmlFor="pref-auto-grab"
            control={
              <Checkbox
                id="pref-auto-grab"
                checked={shownDefaults.autoGrab}
                onChange={(v) => editDefault('autoGrab', v)}
              />
            }
          />

          <Row
            label="Confirm before cancelling"
            hint="Ask first when cancelling a transfer that's already running"
            htmlFor="pref-confirm-cancel"
            control={
              <Checkbox
                id="pref-confirm-cancel"
                checked={shownPrefs.confirmCancel}
                onChange={(v) => editPref('confirmCancel', v)}
              />
            }
          />
        </Section>

        <Section
          title="Search"
          note="Starting values for a new search. Both can still be changed per-search without changing the default."
        >
          <Row
            label="Results per search"
            hint="How many release groups MusicBrainz is asked for. It caps a page at 100."
            htmlFor="pref-search-limit"
            control={
              <div class="settings-number">
                <input
                  id="pref-search-limit"
                  type="number"
                  min={1}
                  max={100}
                  value={shownPrefs.searchLimit}
                  onInput={(e) =>
                    editPref('searchLimit', Number((e.currentTarget as HTMLInputElement).value))
                  }
                />
              </div>
            }
          />

          <Row
            label="Studio albums only, by default"
            hint="Excludes live albums, compilations, interviews and demos from the query — which is what makes it worth doing, since MusicBrainz spends the limit on whatever matches"
            htmlFor="pref-studio-only"
            control={
              <Checkbox
                id="pref-studio-only"
                checked={shownPrefs.searchStudioOnly}
                onChange={(v) => editPref('searchStudioOnly', v)}
              />
            }
          />
        </Section>

        <Section
          title="Soulseek candidates"
          note="Where the filters in the candidates panel start. Changing them there is temporary; changing them here is the default."
        >
          <Row
            label="Minimum score"
            hint="Hide candidates ranking below this. Zero shows everything."
            htmlFor="pref-min-score"
            control={
              <div class="settings-slider">
                <input
                  id="pref-min-score"
                  type="range"
                  min={0}
                  max={100}
                  step={5}
                  value={shownPrefs.candidateMinScore}
                  onInput={(e) =>
                    editPref(
                      'candidateMinScore',
                      Number((e.currentTarget as HTMLInputElement).value),
                    )
                  }
                />
                <span class="settings-slider-value">{shownPrefs.candidateMinScore}</span>
              </div>
            }
          />

          <Row
            label="Free slot only"
            hint="Only peers who can start sending now, rather than queueing you"
            htmlFor="pref-free-slot"
            control={
              <Checkbox
                id="pref-free-slot"
                checked={shownPrefs.candidateFreeSlotOnly}
                onChange={(v) => editPref('candidateFreeSlotOnly', v)}
              />
            }
          />

          <Row
            label="Complete albums only"
            hint="Candidates holding at least as many tracks as the release you picked"
            htmlFor="pref-complete-only"
            control={
              <Checkbox
                id="pref-complete-only"
                checked={shownPrefs.candidateCompleteOnly}
                onChange={(v) => editPref('candidateCompleteOnly', v)}
              />
            }
          />
        </Section>

        <Section title="Interface">
          <Row
            label="Open the log on start"
            hint="Useful while you're still confirming a new install is behaving"
            htmlFor="pref-log-open"
            control={
              <Checkbox
                id="pref-log-open"
                checked={shownPrefs.logOpenOnStart}
                onChange={(v) => editPref('logOpenOnStart', v)}
              />
            }
          />

          <Row
            label="Reset preferences"
            hint="Puts everything above back to its default. Applies immediately, and does not touch server configuration."
            control={
              <button
                type="button"
                class="settings-reset-button"
                onClick={() => {
                  resetPrefs()
                  setDraftPrefs({})
                }}
              >
                Reset
              </button>
            }
          />
        </Section>

        {/* ===== server configuration ===== */}

        <div class="settings-divider">
          <h3 class="settings-section-title">Server configuration</h3>
          <p class="settings-section-note">
            These come from your compose file or <code>.env</code>. Changing one here stores an
            override that wins over the environment and applies without a restart — the row
            says so, and can be reverted. Two of them can't be changed here at all, and say
            why.
          </p>
        </div>

        {loading && !server ? <LoadingPanel label="Reading configuration" /> : null}

        {error ? (
          <div class="settings-error">
            <h4 class="text red">Could not read the server configuration</h4>
            <p class="text default-muted">{error}</p>
          </div>
        ) : null}

        {server ? (
          <>
            <div class={`settings-verdict${server.organizing.enabled ? ' ok' : ''}`} role="status">
              <span class="settings-verdict-title">
                {server.organizing.enabled
                  ? 'Organizing is active — finished downloads will be tagged and filed.'
                  : 'Organizing will not file anything right now.'}
              </span>

              {server.organizing.blockers.length ? (
                <ul class="settings-verdict-list">
                  {server.organizing.blockers.map((blocker) => (
                    <li key={blocker}>{blocker}</li>
                  ))}
                </ul>
              ) : null}
            </div>

            {server.groups.map((group) => (
              <Section key={group.id} title={group.label} note={group.note}>
                <div class="settings-env-list">
                  {group.settings.map((setting) => (
                    <SettingRow
                      key={setting.key}
                      setting={setting}
                      modes={server.organize_modes}
                      draft={draftEnv[setting.key]}
                      onEdit={editEnv}
                      onRevert={revertEnv}
                    />
                  ))}
                </div>
              </Section>
            ))}

            <Section
              title="Organize modes"
              note="What ORGANIZE_MODE can be set to, least to most destructive."
            >
              <div class="settings-env-list">
                {Object.entries(server.organize_modes).map(([mode, description]) => (
                  <div key={mode} class="settings-mode">
                    <code class="settings-mode-name">{mode}</code>
                    <span class="settings-mode-note">{description}</span>
                  </div>
                ))}
              </div>
            </Section>

            <div class="settings-version">
              jimbrainz <code>v{server.version}</code>
            </div>
          </>
        ) : null}
      </div>

      {/*
        The save bar, docked outside the scroller so it cannot scroll away from you while
        there are unsaved changes. It appears only when there is something to save: a save
        button that is permanently present and permanently disabled is furniture, and gives no
        signal at the moment it actually matters.
      */}
      {(dirtyCount > 0 || saveError || justSaved) && (
        <div class={`settings-savebar${saveError ? ' has-error' : ''}`} role="status">
          <span class="settings-savebar-text">
            {saveError
              ? saveError
              : justSaved
                ? 'Saved.'
                : `${dirtyCount} unsaved change${dirtyCount === 1 ? '' : 's'}`}
          </span>

          {dirtyCount > 0 && (
            <>
              <button
                type="button"
                class="settings-savebar-discard"
                onClick={discard}
                disabled={saving}
              >
                Discard
              </button>
              <button
                type="button"
                class="settings-savebar-save"
                onClick={save}
                disabled={saving}
              >
                {saving ? 'Saving…' : 'Save changes'}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  )
}
