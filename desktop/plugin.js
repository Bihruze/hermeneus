// Hermeneus desktop half: live subtitle pane and status bar item.
//
// Hermes Desktop loads this file uncompiled as plain ESM, so there is no JSX and
// no build step. The loader allows three import specifiers: @hermes/plugin-sdk,
// react and react/jsx-runtime. Everything lives in this one file because a
// relative import cannot resolve against the blob URL the loader evaluates from.
//
// The pure subtitle reducer (createSubtitleState, applyEvent, applyResync and
// the helpers below it) is exported by name so tests/desktop can run it under
// node --test. Desktop reads only the default export.

import {
  atom,
  Button,
  GlyphSpinner,
  host,
  icons,
  ListRow,
  PANES_AREA,
  Popover,
  PopoverContent,
  PopoverTrigger,
  SegmentedControl,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  STATUSBAR_AREAS,
  StatusDot,
  Tip,
  usePluginI18n,
  useTheme,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { memo, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hermeneus'
// The pane's contribution id as Desktop's pane tree knows it (`<plugin id>:<item id>`).
const PANE_ID = `${ID}:pane`

// ---------------------------------------------------------------------------
// Pure subtitle state
// ---------------------------------------------------------------------------

/** Lines kept in memory. Older lines are dropped from the top. */
export const LINE_LIMIT = 200

/** Speaker colours, assigned in order of first appearance. These are existing
 *  Hermes theme tokens (apps/desktop/src/styles.css, :root and :root.dark), so
 *  every theme recolours them. --ui-red is left out because failed lines use it. */
export const SPEAKER_TOKENS = Object.freeze([
  '--ui-blue',
  '--ui-green',
  '--ui-orange',
  '--ui-purple',
  '--ui-cyan',
  '--ui-yellow'
])

const LINE_STATES = new Set(['partial', 'committed', 'translated', 'failed'])
const RUNNING_STATES = new Set(['starting', 'live', 'reconnecting', 'stopping'])

export function createSubtitleState(limit = LINE_LIMIT) {
  return {
    limit,
    // Highest event seq applied so far; null before the first one.
    seq: null,
    // Lines in order of first appearance, at most `limit`.
    lines: [],
    // Ids dropped by the bound, so a late update cannot bring a line back at the bottom.
    evicted: [],
    // speaker_id -> speaker number (1, 2, ...) in order of first appearance. Scribe ids are
    // s<N>:speaker_<k> and N grows every Scribe session (docs/events.md), so the raw id
    // carries no stable number of its own. The number also picks the colour.
    speakers: {},
    speakerCount: 0,
    // Last status object from hello, status events, GET /status or a control call.
    status: null,
    // Most recent line that reached the translated state; drives aria-live and the status bar.
    lastTranslated: null
  }
}

function seqOf(event) {
  return Number.isInteger(event.seq) ? event.seq : null
}

function str(value) {
  return typeof value === 'string' ? value : ''
}

function optStr(value) {
  return typeof value === 'string' && value !== '' ? value : null
}

function normalizeLine(line) {
  return {
    line_id: line.line_id,
    state: line.state,
    original: str(line.original),
    translation: str(line.translation),
    speaker_id: optStr(line.speaker_id),
    source_language: optStr(line.source_language),
    target_language: optStr(line.target_language),
    segment_index: Number.isInteger(line.segment_index) ? line.segment_index : null,
    error: str(line.error)
  }
}

function withSpeaker(state, speakerId) {
  if (speakerId === null || Object.prototype.hasOwnProperty.call(state.speakers, speakerId)) {
    return state
  }

  return {
    ...state,
    speakers: { ...state.speakers, [speakerId]: state.speakerCount + 1 },
    speakerCount: state.speakerCount + 1
  }
}

function applyLine(state, raw) {
  if (!raw || typeof raw !== 'object' || typeof raw.line_id !== 'string' || !LINE_STATES.has(raw.state)) {
    return state
  }

  const line = normalizeLine(raw)
  const index = state.lines.findIndex(item => item.line_id === line.line_id)

  if (index === -1 && state.evicted.includes(line.line_id)) {
    return state
  }

  const previous = index === -1 ? null : state.lines[index]
  let lines

  if (index === -1) {
    lines = [...state.lines, line]
  } else {
    lines = state.lines.slice()
    lines[index] = line
  }

  let evicted = state.evicted

  if (lines.length > state.limit) {
    const dropped = lines.slice(0, lines.length - state.limit).map(item => item.line_id)
    lines = lines.slice(lines.length - state.limit)
    evicted = [...evicted, ...dropped].slice(-state.limit)
  }

  const newlyTranslated =
    line.state === 'translated' &&
    line.translation !== '' &&
    (previous === null || previous.state !== 'translated' || previous.translation !== line.translation)

  let lastTranslated = state.lastTranslated

  if (newlyTranslated) {
    lastTranslated = line
  } else if (lastTranslated && lastTranslated.line_id === line.line_id) {
    lastTranslated = line.state === 'translated' ? line : null
  }

  return { ...withSpeaker(state, line.speaker_id), lines, evicted, lastTranslated }
}

function applyRelabel(state, from, to) {
  if (typeof from !== 'string' || typeof to !== 'string' || from === '' || to === '' || from === to) {
    return state
  }

  const lines = state.lines.map(line => (line.speaker_id === from ? { ...line, speaker_id: to } : line))
  let speakers = state.speakers

  // The surviving speaker keeps its own number and colour; a new id inherits the merged one's.
  if (!Object.prototype.hasOwnProperty.call(speakers, to) && Object.prototype.hasOwnProperty.call(speakers, from)) {
    speakers = { ...speakers, [to]: speakers[from] }
  }

  const last = state.lastTranslated
  const lastTranslated = last && last.speaker_id === from ? { ...last, speaker_id: to } : last

  return { ...state, lines, speakers, lastTranslated }
}

function applyStatus(state, event) {
  const { type: _type, seq: _seq, ...status } = event

  if (typeof status.state !== 'string') {
    return state
  }

  // A new run starts a new SubtitleBus and a new Scribe client, so line and speaker ids
  // from the previous run can repeat. Start the list and the speaker numbering again.
  if (status.state === 'starting' && !RUNNING_STATES.has(state.status?.state)) {
    return { ...createSubtitleState(state.limit), seq: state.seq, status }
  }

  return { ...state, status }
}

function clearLines(state) {
  return {
    ...createSubtitleState(state.limit),
    speakers: state.speakers,
    speakerCount: state.speakerCount,
    status: state.status
  }
}

/** Apply one event from the /events socket or from a /lines resync page.
 *  Events whose seq is not above the last applied seq are duplicates and are
 *  ignored. Events without a seq (hello, a status read from GET /status) are
 *  applied without moving the seq. */
export function applyEvent(state, event) {
  if (!event || typeof event !== 'object') {
    return state
  }

  const seq = seqOf(event)

  // hello carries the server's latest seq, but it is not an event in the sequence: moving
  // our seq to it would skip what happened while the socket was down. It sets the status
  // only; the controller resyncs after it.
  if (event.type === 'hello') {
    const session = event.session

    if (session && typeof session === 'object' && typeof session.state === 'string') {
      const { seq: _seq, ...status } = session

      return { ...state, status }
    }

    return state
  }

  // reset: the stream is about to resend every stored line. Its seq may be below ours
  // after a gateway restart, so it is applied whatever our seq is.
  if (event.type === 'reset') {
    return { ...clearLines(state), seq }
  }

  if (seq !== null && state.seq !== null && seq <= state.seq) {
    return state
  }

  let next = state

  switch (event.type) {
    case 'line':
      next = applyLine(state, event.line)

      break
    case 'relabel':
      next = applyRelabel(state, event.from, event.to)

      break
    case 'status':
      next = applyStatus(state, event)

      break
    default:
      // An event type this pane does not know still advances the seq.
      break
  }

  return seq === null ? next : { ...next, seq }
}

/** Apply a GET /lines?after=<seq> page: { seq, reset, events }.
 *  With reset true the server's buffer no longer covers our seq (or the server
 *  restarted); the page is then a full snapshot and replaces the line list.
 *  Speaker colours survive a reset so a speaker keeps its colour. */
export function applyResync(state, page) {
  if (!page || typeof page !== 'object' || !Array.isArray(page.events)) {
    return state
  }

  let next = state

  if (page.reset === true) {
    next = clearLines(state)
  }

  for (const event of page.events) {
    next = applyEvent(next, event)
  }

  if (Number.isInteger(page.seq) && (next.seq === null || page.seq > next.seq)) {
    next = { ...next, seq: page.seq }
  }

  return next
}

/** CSS colour for a speaker, or null when the line has no speaker. */
export function speakerColor(state, speakerId) {
  if (speakerId === null || !Object.prototype.hasOwnProperty.call(state.speakers, speakerId)) {
    return null
  }

  return speakerInk(SPEAKER_TOKENS[(state.speakers[speakerId] - 1) % SPEAKER_TOKENS.length])
}

/** Share of the speaker token in the pane's speaker colour; the rest is --ui-text-primary.
 *  The raw tokens miss WCAG AA (4.5:1) on the pane in the default theme: light purple 2.65:1,
 *  yellow 3.06:1, orange 3.16:1, green 4.16:1, cyan 4.30:1; dark blue 3.33:1 (measured,
 *  tests/desktop/contrast.test.mjs). Mixing toward the primary text colour keeps each hue and
 *  follows every theme; at 60% the weakest is 4.75:1. */
export const SPEAKER_INK_PERCENT = 60

/** The pane's colour for a speaker token: readable on the pane background, still that token's hue. */
export function speakerInk(token) {
  return `color-mix(in srgb, var(${token}) ${SPEAKER_INK_PERCENT}%, var(--ui-text-primary))`
}

/** The speaker's number in order of first appearance (1-based), or null for no speaker. */
export function speakerNumber(state, speakerId) {
  if (speakerId === null || !Object.prototype.hasOwnProperty.call(state.speakers, speakerId)) {
    return null
  }

  return state.speakers[speakerId]
}

/** The language the backend detected (status.detected_language), else the source
 *  language of the newest line that carries one. */
export function detectedLanguage(state) {
  if (typeof state.status?.detected_language === 'string' && state.status.detected_language) {
    return state.status.detected_language
  }

  for (let i = state.lines.length - 1; i >= 0; i -= 1) {
    if (state.lines[i].source_language) {
      return state.lines[i].source_language
    }
  }

  return null
}

export function truncate(text, max) {
  const clean = str(text).replace(/\s+/g, ' ').trim()

  return clean.length <= max ? clean : `${clean.slice(0, Math.max(0, max - 1)).trimEnd()}…`
}

// ---------------------------------------------------------------------------
// Pure view decisions (tested under node --test)
// ---------------------------------------------------------------------------

/** What the subtitle area shows. `body` is one designed state, or 'lines' when
 *  there are lines to show. `notice` is a row shown beside the lines:
 *  'offline' (backend unreachable), 'reconnecting' (session reconnecting to
 *  the speech service), 'failed' (session failed), 'stopped' (session over,
 *  lines kept), or null. */
export function subtitleView(state, link) {
  const session = state.status?.state ?? null
  const hasLines = state.lines.length > 0

  let body = 'lines'

  if (!hasLines) {
    if (link.state === 'offline') {
      body = 'offline'
    } else if (session === null) {
      body = 'connecting'
    } else if (session === 'failed') {
      body = 'failed'
    } else if (session === 'starting') {
      body = 'starting'
    } else if (session === 'live' || session === 'stopping') {
      body = 'listening'
    } else if (session === 'reconnecting') {
      body = 'reconnecting'
    } else {
      body = 'idle'
    }
  }

  let notice = null

  if (hasLines) {
    if (link.state === 'offline') {
      notice = 'offline'
    } else if (session === 'reconnecting') {
      notice = 'reconnecting'
    } else if (session === 'failed') {
      notice = 'failed'
    } else if (session === 'idle') {
      notice = 'stopped'
    }
  }

  return { body, notice }
}

/** True when line `index` starts a new speaker turn, so its row shows the speaker name.
 *  Following lines of the same speaker leave the name column empty. */
export function startsTurn(lines, index) {
  if (index <= 0) {
    return true
  }

  return lines[index - 1].speaker_id !== lines[index].speaker_id
}

/** Lines below the one the reader paused at. `anchorId` is the newest line id when
 *  following stopped; an id that has been evicted counts every line as new. */
export function unseenCount(lines, anchorId) {
  if (anchorId === null || anchorId === undefined) {
    return 0
  }

  const index = lines.findIndex(line => line.line_id === anchorId)

  return index === -1 ? lines.length : lines.length - 1 - index
}

/** Pixels from the bottom that still count as following the newest line; the core Logs
 *  pane uses the same stick-to-bottom slack (apps/desktop/src/app/contrib/panes.tsx:37). */
export const FOLLOW_SLACK_PX = 48

/** Whether a scroll position counts as following the newest line. */
export function atLiveEdge({ scrollHeight, scrollTop, clientHeight }, slack = FOLLOW_SLACK_PX) {
  return scrollHeight - scrollTop - clientHeight <= slack
}

/** Status bar text: the plugin name, then the state, then the newest translation while a
 *  session runs. Idle shows the name alone so the item is still recognisable. */
export function chipParts(state) {
  const session = state.status?.state ?? null
  const running = RUNNING_STATES.has(session)
  const last = running && state.lastTranslated ? state.lastTranslated.translation : ''

  return { session, showState: session !== null && session !== 'idle' && !(session === 'live' && last), last }
}

// ---------------------------------------------------------------------------
// Pure first-run setup and one-click start (GET /setup, docs/events.md)
// ---------------------------------------------------------------------------

/** Each setup step's state: done, todo, running, error or unknown. Null before GET /setup answered. */
export function setupSteps(setup) {
  if (!setup || typeof setup !== 'object') {
    return null
  }

  const keySet = setup.key?.set
  const audio = str(setup.audio?.state)
  const model = setup.model && typeof setup.model === 'object' ? setup.model : {}

  return {
    key: keySet === true ? 'done' : keySet === false ? 'todo' : 'unknown',
    audio:
      audio === 'ok' || audio === 'not_needed'
        ? 'done'
        : audio === 'running'
          ? 'running'
          : audio === 'denied' || audio === 'failed'
            ? 'error'
            : 'todo',
    // The main Hermes model translates when live_translate is not pinned; only a missing model is a todo.
    model: model.route === 'auxiliary' || model.ready !== false ? 'done' : 'todo',
    language: optStr(setup.language?.code) ? 'done' : 'todo'
  }
}

/** Whether GET /setup has said setup is complete. A Desktop on another computer gets
 *  `{remote: true}` (setup happens on the Mac itself), which counts as nothing to guide here. */
export function setupKnownComplete(setup) {
  return Boolean(setup && typeof setup === 'object' && (setup.complete === true || setup.remote === true))
}

/** Whether the pane shows the guided setup and the chip opens the pane: until GET /setup is known
 *  to report it complete. Before the first answer (or while it fails) the setup shows its loading
 *  or error state with Try again, never the normal controls. */
export function setupNeeded(setup) {
  return !setupKnownComplete(setup)
}

/** Whether a tick re-reads GET /setup: every tick while there is no answer yet or the audio check
 *  runs, every third tick while setup is open, and every fifteenth (30 s) otherwise, so a backend
 *  that lost its libraries or its saved check is noticed even without a reconnect. */
export function setupTickDue(setup, tick) {
  if (!setup || typeof setup !== 'object' || setup.audio?.state === 'running') {
    return true
  }

  return tick % (setupNeeded(setup) ? 3 : 15) === 0
}

/** The source the pane preselects when none was chosen: the whole-system tap, else the only source. */
export function defaultSourceKey(sources) {
  const list = Array.isArray(sources) ? sources.filter(s => s && typeof s.source_id === 'string') : []
  const system = list.find(s => (s.backend === 'process-tap' || s.backend === 'process-loopback') && s.source_id === 'system')

  if (system) {
    return sourceKey(system)
  }

  return list.length === 1 ? sourceKey(list[0]) : ''
}

/** The POST /start body for the last settings (the pane's Start and one click on the status bar item).
 *  An empty source lets the backend pick (system audio with a process tap); an empty language uses the
 *  setup's default (Hermes display language, else the system language). */
export function startBody(prefs, defaultLanguage = '') {
  const p = prefs && typeof prefs === 'object' ? prefs : {}
  const body = { target_language: str(p.targetLanguage) || str(defaultLanguage) }
  const key = str(p.source)
  const split = key.indexOf(':')

  if (split > 0) {
    body.source = { backend: key.slice(0, split), source_id: key.slice(split + 1) }
  }

  if (p.speak === true) {
    body.speak = true
    body.mode = p.mode === 'voice' ? 'voice' : 'both'

    if (str(p.outputDevice)) {
      body.output_device = p.outputDevice
    }

    if (str(p.voiceId)) {
      body.voice_id = p.voiceId
    }
  } else {
    body.speak = false
    body.mode = 'subtitles'
  }

  return body
}

/** What one click on the status bar item does: stop a running session, else start with the last settings. */
export function chipClick(status) {
  const state = status?.state

  if (state === 'stopping' || state === 'starting') {
    return 'wait'
  }

  return RUNNING_STATES.has(state) ? 'stop' : 'start'
}

/** The system languages the Desktop app reports, best first (Electron gives the macOS order). */
export function clientLocales(nav = globalThis.navigator) {
  const list = Array.isArray(nav?.languages) ? nav.languages : []
  const all = [...list, nav?.language].filter(tag => typeof tag === 'string' && tag !== '')

  return [...new Set(all)].slice(0, 5)
}

/** GET /setup path with the system languages as repeated locale params. */
export function setupPath(locales) {
  const query = (Array.isArray(locales) ? locales : []).map(tag => `locale=${encodeURIComponent(tag)}`).join('&')

  return query ? `/setup?${query}` : '/setup'
}

// ---------------------------------------------------------------------------
// Controller: socket, resync, polling fallback, control calls
// ---------------------------------------------------------------------------

/** Poll tick. The socket is the primary feed; the tick only resyncs when no
 *  frame has arrived for the quiet window below (socket down, or a no-op
 *  socket on an OAuth remote, see desktop-plugin-sdk.md "Calling it from the plugin"). */
const POLL_TICK_MS = 2000
const QUIET_WHILE_RUNNING_MS = 4000
const QUIET_WHILE_IDLE_MS = 10000
const PENDING_LIMIT = LINE_LIMIT * 4
export const FONT_SCALES = Object.freeze([0.85, 1, 1.15, 1.3, 1.5, 1.75, 2])

const DEFAULT_PREFS = Object.freeze({
  fontScale: 1,
  mode: 'subtitles',
  outputDevice: '',
  source: '',
  speak: false,
  targetLanguage: '',
  voiceId: ''
})

// Desktop's REST bridge rejects with "<status>: <body>" (apps/desktop/electron/api-transport.ts:185-186).
// The Hermeneus routes answer errors as {"ok": false, "detail", "error"} (docs/events.md), so
// show the detail when the body is that JSON.
const REST_ERROR_BODY = /(\d{3}): (\{[\s\S]*\})\s*$/

export function errorText(error) {
  const text = error instanceof Error ? error.message : String(error)
  const match = REST_ERROR_BODY.exec(text)

  if (match) {
    try {
      const body = JSON.parse(match[2])
      const detail = typeof body.detail === 'string' ? body.detail : typeof body.error === 'string' ? body.error : ''

      if (detail) {
        return `${detail} (${match[1]})`
      }
    } catch {
      // Not JSON after all: show the text as it came.
    }
  }

  return text
}

/** GET /devices path for a saved source key (backend:source_id), or the plain path. */
export function devicesPath(key) {
  const split = typeof key === 'string' ? key.indexOf(':') : -1

  if (split <= 0) {
    return '/devices'
  }

  const backend = encodeURIComponent(key.slice(0, split))
  const sourceId = encodeURIComponent(key.slice(split + 1))

  return `/devices?backend=${backend}&source_id=${sourceId}`
}

export function sourceKey(source) {
  return source && typeof source === 'object' ? `${str(source.backend)}:${str(source.source_id)}` : ''
}

// ---------------------------------------------------------------------------
// Pure control model: what the controls row shows and what a choice changes
// ---------------------------------------------------------------------------

export const MODES = Object.freeze(['subtitles', 'both', 'voice'])

/** The mode the Mode select shows: what the session runs while running, the saved choice while idle. */
export function shownMode(status, prefs, running) {
  if (running) {
    const mode = str(status?.mode)

    return MODES.includes(mode) ? mode : status?.speak === true ? 'both' : 'subtitles'
  }

  return prefs.speak ? (prefs.mode === 'voice' ? 'voice' : 'both') : 'subtitles'
}

/** What picking `mode` saves (prefs patch) and, while running, asks the session for (POST /settings body).
 *  A mode with voice needs an output device: the chosen one, else the first safe one the backend listed. */
export function modeChoice(mode, { outputId = '', outputs = [], voiceId = '' } = {}) {
  if (mode === 'subtitles') {
    return { patch: { speak: false }, change: { mode: 'subtitles' } }
  }

  const listed = outputs.filter(o => o && typeof o.device_id === 'string' && o.device_id !== '')
  const output = listed.some(o => o.device_id === outputId) ? outputId : listed[0]?.device_id ?? ''
  const patch = { speak: true, mode, ...(output ? { outputDevice: output } : {}) }
  const change = { mode, ...(output ? { output_device: output } : {}), ...(voiceId ? { voice_id: voiceId } : {}) }

  return { patch, change }
}

/** Label of the voice popover trigger: the chosen voice's name, else the default voice's, else a fallback. */
export function voiceLabel(voices, voiceId, fallback) {
  const list = Array.isArray(voices) ? voices : []
  const chosen = list.find(v => v && v.voice_id === voiceId) ?? (voiceId ? null : list.find(v => v && v.is_default))

  return chosen ? str(chosen.name) || chosen.voice_id : voiceId || fallback
}

/** Overlay look settings the pane offers (plugin.yaml overlay_* keys, hermeneus_engine/overlay_settings.py). */
export const OVERLAY_TEXT_SIZES = Object.freeze([20, 26, 34, 44])
export const OVERLAY_OPACITIES = Object.freeze([0, 40, 80, 100])
/** overlay_style choices (hermeneus_engine/overlay_settings.py STYLES); banner is the default. */
export const OVERLAY_STYLES = Object.freeze(['banner', 'theme'])

/** Theme tokens the overlays draw with (hermeneus_engine/overlay_tokens.py keeps the same table). */
export const OVERLAY_COLOR_TOKENS = Object.freeze({
  surface: '--ui-bg-elevated',
  text: '--ui-text-primary',
  text_secondary: '--ui-text-secondary',
  text_tertiary: '--ui-text-tertiary',
  stroke: '--ui-stroke-secondary',
  // The pane colours a failed line's caption with --ui-red; the overlays mark a failed line with it too.
  failed: '--ui-red'
})

/** The look popover's choices: the offered values, plus the current one when it was set elsewhere
 *  (the CLI or the Settings form take any size from 14 to 64 and any opacity from 0 to 100). */
export function lookOptions(values, current) {
  const list = [...values]

  if (typeof current === 'number' && Number.isFinite(current) && !list.includes(current)) {
    list.push(current)
  }

  return list.sort((a, b) => a - b)
}

/** The overlay buttons' tooltip: what a click does now, and while idle whether the overlay is armed. */
export function overlayButtonLabel(t, running, overlay) {
  if (running) {
    return overlay.visible ? t('hideOverlay') : t('showOverlay')
  }

  return `${t('overlay')}: ${overlay.armed ? t('overlayArmed') : t('overlayNotArmed')}`
}

/** The body POST /overlay/theme takes, from values resolved on the pane's own element. */
export function overlayThemePayload({ colors, font, mode, name, radius, speakers }) {
  return { mode: mode === 'dark' ? 'dark' : 'light', name: str(name), colors: { ...colors, speakers }, radius_px: radius, font: str(font) }
}

export function createController(ctx, clock = () => Date.now(), deps = { request: (...args) => host.request(...args) }) {
  const store = atom(createSubtitleState())
  const link = atom({ state: 'connecting', error: '' })
  const action = atom({ busy: false, error: '' })
  // The native subtitle overlay (hermeneus_engine/overlay), shown and hidden through POST /overlay.
  const overlay = atom({ visible: false, armed: false, settings: null, busy: false, error: '' })
  // First-run setup (GET /setup): which steps are done, the default language, the audio check.
  // loadError: GET /setup failed; error: the last setup action failed (kept until the next action).
  const setup = atom({ data: null, loadError: '', error: '', busy: '' })
  let sentTheme = ''
  const stored = ctx.storage.get('prefs', {})
  const prefs = atom({ ...DEFAULT_PREFS, ...(stored && typeof stored === 'object' ? stored : {}) })

  let resyncing = false
  let resyncAgain = false
  let pending = []
  let lastFrameAt = 0
  let lastResyncAt = 0
  let setupTicks = 0
  let setupReads = 0

  // While a resync is in flight, socket frames wait in `pending` and are
  // replayed after the page, so a live frame can never move the seq past
  // events the page is about to deliver.
  async function resync() {
    if (resyncing) {
      resyncAgain = true

      return
    }

    resyncing = true

    try {
      do {
        resyncAgain = false
        lastResyncAt = clock()

        const after = store.get().seq
        const page = await ctx.rest(after === null ? '/lines' : `/lines?after=${after}`)
        const status = await ctx.rest('/status')
        let next = applyResync(store.get(), page)

        if (status && typeof status === 'object') {
          next = applyEvent(next, { ...status, type: 'status', seq: undefined })
        }

        for (const frame of pending.splice(0)) {
          next = applyEvent(next, frame)
        }

        store.set(next)
      } while (resyncAgain)

      const wasOffline = link.get().state === 'offline'

      link.set({ state: 'online', error: '' })

      // The backend is back (a gateway restart, for example): it may now load the plugin's
      // libraries or have lost the saved check, so the setup is read again.
      if (wasOffline) {
        void refreshSetup()
      }
    } catch (error) {
      // The frames held back are recovered by the next resync from the server buffer.
      pending = []
      link.set({ state: 'offline', error: errorText(error) })
    } finally {
      resyncing = false
    }
  }

  function onFrame(frame) {
    if (!frame || typeof frame !== 'object') {
      return
    }

    lastFrameAt = clock()

    if (resyncing) {
      if (frame.type === 'hello') {
        resyncAgain = true
      }

      pending.push(frame)

      if (pending.length > PENDING_LIMIT) {
        pending = []
        resyncAgain = true
      }

      return
    }

    store.set(applyEvent(store.get(), frame))

    // hello opens every (re)connected socket: catch up on what was missed while it was down,
    // and read the setup again (a restarted backend can answer differently).
    if (frame.type === 'hello') {
      void resync()
      void refreshSetup()
    } else if (link.get().state !== 'online') {
      link.set({ state: 'online', error: '' })
    }
  }

  function tick() {
    const running = RUNNING_STATES.has(store.get().status?.state)
    const quiet = clock() - Math.max(lastFrameAt, lastResyncAt)

    if (quiet >= (running ? QUIET_WHILE_RUNNING_MS : QUIET_WHILE_IDLE_MS)) {
      void resync()
    }

    // The overlay closes by itself when the session stops or from its own menu.
    if (running || overlay.get().visible) {
      void refreshOverlay()
    }

    // The audio check runs in the backend; its answer is read back here. While setup is open,
    // a key added in Settings > Tools & Keys shows up within a few seconds.
    setupTicks += 1

    if (setupTickDue(setup.get().data, setupTicks)) {
      void refreshSetup()
    }
  }

  // Only the newest read is applied, so a slow answer never overwrites a newer one.
  async function refreshSetup() {
    const read = ++setupReads

    try {
      const data = await ctx.rest(setupPath(clientLocales()))

      if (read === setupReads) {
        setup.set({ ...setup.get(), data: data && typeof data === 'object' ? data : null, loadError: '' })
      }
    } catch (error) {
      if (read === setupReads) {
        setup.set({ ...setup.get(), loadError: errorText(error) })
      }
    }
  }

  /** One setup action at a time; its failure stays on screen with the step until the next try. */
  async function setupAction(name, work) {
    if (setup.get().busy) {
      return
    }

    setup.set({ ...setup.get(), busy: name, error: '' })

    try {
      await work()
      setup.set({ ...setup.get(), busy: '' })
    } catch (error) {
      setup.set({ ...setup.get(), busy: '', error: errorText(error) })
    }

    await refreshSetup()
  }

  /** Start and stop the real capture once in the backend (the macOS prompt shows the first time). */
  function checkAudio() {
    return setupAction('audio', async () => {
      const audio = await ctx.rest('/setup/audio-check', { method: 'POST', body: {} })
      const data = setup.get().data

      if (data && audio && typeof audio.state === 'string') {
        setup.set({ ...setup.get(), data: { ...data, audio: { ...data.audio, ...audio } } })
      }
    })
  }

  function openPrivacySettings() {
    return setupAction('privacy', () => ctx.rest('/setup/open-privacy-settings', { method: 'POST', body: {} }))
  }

  /** Start with the last settings: the saved choices, else the defaults (system audio, setup's language). */
  function quickStart() {
    const language = setup.get().data?.language?.code ?? ''

    return start(startBody(prefs.get(), language))
  }

  /** Bring the Hermeneus pane forward (the chip's click while setup is not complete). */
  function revealPane() {
    if (deps.revealPane) {
      deps.revealPane(PANE_ID)

      return
    }

    try {
      host.undismissPane?.(PANE_ID)
      host.revealPane?.(PANE_ID)
    } catch {
      // An older Desktop without these doors: the pane stays where it is.
    }
  }

  /** One click on the status bar item. A start first reads the setup again: if it is not known
   *  to be complete (the backend restarted, lost its libraries, or never answered), the click
   *  opens the pane instead, so macOS is never asked for the permission unannounced. */
  async function toggle() {
    const what = chipClick(store.get().status)

    if (what === 'stop') {
      return stop()
    }

    if (what !== 'start') {
      return
    }

    await refreshSetup()

    if (setupNeeded(setup.get().data)) {
      revealPane()

      return
    }

    return quickStart()
  }

  function takeOverlay(result) {
    if (result && typeof result.visible === 'boolean') {
      overlay.set({
        ...overlay.get(),
        visible: result.visible,
        armed: result.armed === true,
        settings: result.settings && typeof result.settings === 'object' ? result.settings : overlay.get().settings
      })
    }
  }

  async function refreshOverlay() {
    try {
      const result = await ctx.rest('/overlay')

      if (!overlay.get().busy) {
        takeOverlay(result)
      }
    } catch {
      // The next tick asks again.
    }
  }

  // Plugin settings are written the way Desktop's own Settings form writes them
  // (plugins.manage settings, website/docs/developer-guide/desktop-plugin-sdk.md:1026-1028).
  function saveSettings(values) {
    return deps.request('plugins.manage', { action: 'settings', key: ID, values })
  }

  async function busyOverlay(work) {
    if (overlay.get().busy) {
      return
    }

    overlay.set({ ...overlay.get(), busy: true, error: '' })

    try {
      await work()
      overlay.set({ ...overlay.get(), busy: false, error: '' })
    } catch (error) {
      overlay.set({ ...overlay.get(), busy: false, error: errorText(error) })
    }
  }

  /** While a session runs: show or hide the overlay now. While idle: arm it for the next session
   *  (the `overlay` setting), so it opens with Start. */
  function setOverlay(on) {
    return busyOverlay(async () => {
      if (RUNNING_STATES.has(store.get().status?.state)) {
        takeOverlay(await ctx.rest('/overlay', { method: 'POST', body: { visible: on } }))
      } else {
        await saveSettings({ overlay: on })
        takeOverlay(await ctx.rest('/overlay'))
      }
    })
  }

  /** One look setting (overlay_text_size, overlay_max_lines, ...): saved, then applied to an open overlay. */
  function setOverlayLook(values) {
    return busyOverlay(async () => {
      await saveSettings(values)
      takeOverlay(await ctx.rest('/overlay/apply', { method: 'POST', body: {} }))
    })
  }

  /** The active Desktop theme's resolved tokens; sent only when they change. */
  async function sendTheme(payload) {
    const key = JSON.stringify(payload)

    if (key === sentTheme) {
      return
    }

    sentTheme = key

    try {
      await ctx.rest('/overlay/theme', { method: 'POST', body: payload })
    } catch {
      sentTheme = '' // try again with the next change or mount
    }
  }

  function setPrefs(patch) {
    const next = { ...prefs.get(), ...patch }
    prefs.set(next)
    ctx.storage.set('prefs', next)
  }

  async function control(path, body) {
    if (action.get().busy) {
      return
    }

    action.set({ busy: true, error: '' })

    try {
      const result = await ctx.rest(path, { method: 'POST', body })
      // POST /start and /stop answer a status; POST /settings answers {accepted, applying, status}.
      const status = result && typeof result === 'object' && result.status && typeof result.status === 'object'
        ? result.status
        : result

      if (status && typeof status === 'object' && typeof status.state === 'string') {
        store.set(applyEvent(store.get(), { ...status, type: 'status', seq: undefined }))
      }

      action.set({ busy: false, error: '' })
    } catch (error) {
      action.set({ busy: false, error: errorText(error) })
    }
  }

  function start(config) {
    return control('/start', config)
  }

  function stop() {
    return control('/stop', {})
  }

  /** Change the running session (POST /settings). The answer comes at once; background changes
   *  (speech on, voice, output, source) report through later status events. */
  function settings(changes) {
    return control('/settings', changes)
  }

  function connect() {
    ctx.socket('/events', onFrame)
    ctx.setInterval(tick, POLL_TICK_MS)
    // A change in Settings > Plugins reaches an open overlay without hiding it.
    ctx.onEvent?.(`plugin.${ID}.settings.changed`, () => {
      void ctx.rest('/overlay/apply', { method: 'POST', body: {} }).then(takeOverlay, () => {})
    })
    void resync()
    void refreshOverlay()
    void refreshSetup()
  }

  return {
    action,
    checkAudio,
    os: ctx.os,
    revealPane,
    openPrivacySettings,
    quickStart,
    refreshSetup,
    setup,
    toggle,
    connect,
    link,
    onFrame,
    overlay,
    prefs,
    refreshOverlay,
    resync,
    rest: ctx.rest,
    sendTheme,
    setOverlay,
    setOverlayLook,
    setPrefs,
    settings,
    start,
    stop,
    store,
    tick
  }
}

// ---------------------------------------------------------------------------
// Copy
// ---------------------------------------------------------------------------

const EN = {
  title: 'Hermeneus',
  subtitles: 'Subtitles',
  doctor: 'Doctor',
  state: {
    idle: 'Idle',
    starting: 'Starting',
    live: 'Live',
    reconnecting: 'Reconnecting',
    stopping: 'Stopping',
    failed: 'Failed',
    unknown: 'Waiting for backend'
  },
  heard: lang => `heard: ${lang}`,
  subtitlesIn: lang => `subtitles: ${lang}`,
  offline: 'Hermeneus backend unreachable',
  offlineHint:
    'Turn on Hermeneus in Capabilities > Plugins (its Agent switch), then press Try again. If it stays unreachable, open the gateway item in the status bar and click Restart gateway so it loads the plugin.',
  source: 'Source',
  sourcePlaceholder: 'Choose a source',
  refreshSources: 'Refresh sources and devices',
  target: 'Subtitle language',
  targetPlaceholder: 'Choose a language',
  noLanguages: 'Language list unavailable',
  notSpeakable: 'Spoken output is not available for this language',
  output: 'Output device',
  outputPlaceholder: 'Choose an output',
  // Proposal for Seher (W26): mode and voice pickers, reachable while running.
  mode: 'Mode',
  modes: { subtitles: 'Subtitles only', both: 'Subtitles and voice', voice: 'Voice only' },
  voiceSettings: 'Voice and output device',
  needsVoiceMode: 'Choose a mode with voice',
  voice: 'Voice',
  voicePlaceholder: 'Default voice',
  voicesError: error => `Could not list voices: ${error}`,
  start: 'Start',
  stop: 'Stop',
  devicesError: error => `Could not list sources: ${error}`,
  actionError: error => `Request failed: ${error}`,
  emptyTitle: 'No subtitles yet',
  emptyDescription: 'Choose a source and a subtitle language, then press Start.',
  connectingTitle: 'Connecting to Hermeneus',
  connectingDescription: 'Reading the session state from the Hermes gateway.',
  startingTitle: 'Starting',
  startingDescription: 'Opening the audio source and the speech service.',
  listeningTitle: 'Listening',
  listeningDescription: 'Subtitles appear here as soon as someone speaks.',
  reconnectingTitle: 'Reconnecting',
  reconnectingDescription: 'The speech service connection dropped. Hermeneus is reconnecting.',
  failedTitle: 'The session stopped with an error',
  failedFallback: 'Open Doctor to see which check fails and how to fix it.',
  openDoctor: 'Open Doctor',
  retry: 'Try again',
  lastError: error => `Last error: ${error}.`,
  noticeOffline: error => `Hermes gateway unreachable${error ? ` (${error})` : ''}. Retrying.`,
  noticeReconnecting: 'Reconnecting to the speech service. New lines continue when it is back.',
  noticeFailed: message => `Session failed${message ? `: ${message}` : '.'}`,
  noticeStopped: 'Session stopped. These lines stay until the next Start.',
  speaker: n => `Speaker ${n}`,
  translating: 'translating',
  failedLine: error => (error ? `Translation failed: ${error}. Original shown.` : 'Translation failed. Original shown.'),
  jumpToLive: 'Jump to live',
  newLines: n => (n === 1 ? '1 new line' : `${n} new lines`),
  smaller: 'Smaller subtitles',
  larger: 'Larger subtitles',
  scale: percent => `${percent}%`,
  runDoctor: 'Run checks again',
  doctorRunning: 'Running checks',
  doctorError: error => `Doctor failed: ${error}`,
  doctorShape: 'Doctor returned an answer this pane cannot read.',
  check: { ok: 'ok', warn: 'warning', fail: 'failed' },
  statusBar: state => `Hermeneus: ${state}`,
  overlay: 'Subtitle overlay',
  overlayArmed: 'Opens with the next session',
  overlayNotArmed: 'Off. Click to open it with the next session',
  overlayNotTranslated: 'Not translated',
  overlayWaiting: 'Waiting for speech',
  overlayReconnecting: 'Reconnecting',
  overlayLook: 'Overlay look',
  overlayTextSize: 'Text size',
  overlayLines: 'Lines',
  overlayOriginal: 'Show the original',
  overlaySpeaker: 'Speaker names',
  overlayOpacity: 'Background',
  overlayStyle: 'Style',
  overlayStyles: { banner: 'Banner', theme: 'Hermes theme' },
  percent: n => `${n}%`,
  on: 'On',
  off: 'Off',
  overlayHint: 'Floats the current line above other apps on this Mac',
  showOverlay: 'Show subtitle overlay',
  hideOverlay: 'Hide subtitle overlay',
  overlayError: error => `Overlay: ${error}`,
  // First-run setup (W29 proposal for Seher)
  setupTitle: 'Set up live subtitles',
  setupIntro: 'Add the key and allow system audio, then Start. The model and the language are already set; change them if you like. Nothing here is typed into a chat.',
  setupLoading: 'Checking what is already set up',
  setupError: error => `Could not read the setup: ${error}`,
  stepDone: 'Done',
  keyTitle: 'ElevenLabs API key',
  keyTodo:
    'Speech to text runs on ElevenLabs. Create a key with Speech to Text access (add Text to Speech and Voices read for spoken translation), then press Add key in Hermes: in Tools & Keys, click the ELEVENLABS_API_KEY field, paste the key and press Save. The key stays in Hermes; this pane never sees it.',
  keyDone: 'Added in Hermes. The value is not shown here.',
  keyUnknown: 'Hermes could not say whether the key is set.',
  keyCreate: 'Create a key',
  keyAdd: 'Add key in Hermes',
  keyRecheck: 'Check again',
  audioTitle: 'System audio',
  audioTodo:
    'Hermeneus listens to the sound your Mac plays. Press the button once: macOS asks for System Audio Recording, and a short muted test checks that real audio arrives.',
  audioRunning: 'Waiting for macOS and the test capture (up to 30 seconds). Answer the macOS prompt if it shows.',
  audioDone: 'Real audio arrives.',
  audioNotNeeded: detail => `No permission needed here${detail ? ` (${detail})` : ''}.`,
  audioDenied:
    'macOS gave silence instead of audio, so the permission is off. Open System Settings, turn on Hermes under System Audio Recording Only, then check again.',
  audioAllow: 'Allow system audio',
  audioAgain: 'Check again',
  audioOpenSettings: 'Open System Settings',
  audioWaiting: 'Checking',
  modelTitle: 'Translation model',
  modelMain: model => `Uses your main Hermes model${model ? ` (${model})` : ''}. Nothing to set up.`,
  modelPinned: model => `Uses ${model} for Live translation.`,
  modelMissing: 'Hermes has no main model yet. Choose one in Settings > Models.',
  modelFaster: 'Optional: a small, fast model for Live translation keeps subtitles closer to the speech.',
  modelChoose: 'Choose a faster model',
  languageTitle: 'Subtitle language',
  languageFrom: { setting: 'Your saved choice.', hermes: 'From your Hermes language.', system: 'From your system language.', fallback: 'English until you choose.' },
  setupStartHint: 'Start opens the floating subtitle overlay too.',
  setupNotReady: 'Finish the steps above to start.',
  chipStart: 'Start live subtitles with the last settings',
  chipStop: 'Stop live subtitles',
  chipSetup: 'Open Hermeneus to finish setup',
  chipMenu: 'Live subtitle settings'
}

const LOCALES = { en: EN }

// ---------------------------------------------------------------------------
// Styles: layout only, colours and type sizes come from Hermes theme tokens
// ---------------------------------------------------------------------------

// Subtitle text follows Appearance > Chat Text Size (--chat-text-scale on the root element)
// and then the pane's own size control on top of it.
const scaled = (token, scale) => `calc(var(${token}) * var(--chat-text-scale, 1) * ${scale})`
const TEXT_SIZE = scale => scaled('--conversation-text-base-size', scale)
const TEXT_LINE = scale => scaled('--conversation-base-line-height', scale)
const CAPTION_SIZE = scale => scaled('--conversation-caption-base-size', scale)
const CAPTION_LINE = scale => scaled('--conversation-caption-base-line-height', scale)

// Tailwind v4 spacing unit the app is built with (--spacing: .25rem in the built CSS).
const sp = n => `calc(var(--spacing) * ${n})`

const S = {
  pane: { display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, color: 'var(--ui-text-secondary)' },
  header: {
    display: 'flex',
    flexDirection: 'column',
    gap: sp(1.5),
    padding: `${sp(2.5)} ${sp(2.5)} 0`
  },
  statusRow: {
    display: 'flex',
    alignItems: 'center',
    gap: sp(2),
    minWidth: 0,
    fontSize: 'var(--conversation-caption-base-size)',
    lineHeight: 'var(--conversation-caption-base-line-height)'
  },
  statusState: { color: 'var(--ui-text-primary)', fontWeight: 'var(--font-weight-medium)', flexShrink: 0 },
  statusPart: { color: 'var(--ui-text-tertiary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
  statusMessage: {
    color: 'var(--ui-text-tertiary)',
    fontSize: 'var(--conversation-caption-base-size)',
    lineHeight: 'var(--conversation-caption-base-line-height)'
  },
  alert: {
    color: 'var(--ui-red)',
    fontSize: 'var(--conversation-caption-base-size)',
    lineHeight: 'var(--conversation-caption-base-line-height)'
  },
  controls: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: sp(2) },
  // In the status bar popover the same row wraps inside a fixed width: source, language, mode, then
  // voice, overlay and look on one line, Start or Stop at the end.
  controlsCompact: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: sp(2), width: sp(92) },
  setup: { display: 'flex', flexDirection: 'column', gap: sp(1), padding: `${sp(1)} 0` },
  setupScroll: { flex: 1, minHeight: 0, overflowY: 'auto', padding: `0 ${sp(2.5)} ${sp(2.5)}` },
  setupHead: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: sp(2) },
  setupTitle: { color: 'var(--ui-text-primary)', fontWeight: 'var(--font-weight-medium)' },
  setupActions: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'flex-end', gap: sp(1.5) },
  setupState: { display: 'inline-flex', alignItems: 'center', gap: sp(1.5), color: 'var(--ui-text-secondary)' },
  setupDetail: { color: 'var(--ui-text-tertiary)' },
  setupFoot: { display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: sp(2), paddingTop: sp(1) },
  chipButton: { background: 'none', border: 0, cursor: 'pointer', font: 'inherit' },
  chipMenu: { display: 'flex', flexDirection: 'column', gap: sp(2) },
  chipMenuPanel: { width: 'auto' },
  field: { display: 'flex', alignItems: 'center', gap: sp(1), minWidth: 0 },
  select: { width: sp(44) },
  popover: { display: 'flex', flexDirection: 'column', gap: sp(2) },
  startStop: { marginLeft: 'auto' },
  topRow: { display: 'flex', alignItems: 'center', gap: sp(3), minWidth: 0 },
  topStatus: { flex: 1, minWidth: 0 },
  fontControls: { display: 'flex', alignItems: 'center', gap: sp(0.5) },
  fontScale: {
    minWidth: sp(10),
    textAlign: 'center',
    fontSize: 'var(--conversation-caption-base-size)',
    color: 'var(--ui-text-tertiary)',
    fontVariantNumeric: 'tabular-nums'
  },
  listWrap: { position: 'relative', display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 },
  // scroll-fade-y is the app's own edge fade for scrollers (apps/desktop/src/styles.css:1175-1194);
  // --scroll-fade-pad matches the top padding so the fade starts at the first line.
  list: {
    flex: 1,
    minHeight: 0,
    overflowY: 'auto',
    overflowAnchor: 'auto',
    overscrollBehavior: 'contain',
    padding: `${sp(1)} ${sp(3)} ${sp(3)}`,
    '--scroll-fade-pad': sp(1)
  },
  // The glide moves `content` with a transform, which neither reflows nor counts as a layout shift.
  // A transform still adds to the scroller's scrollable overflow, which would read as "the user
  // scrolled up" mid-glide; `overflow: clip` on the box around it keeps the scroll height true.
  // What it clips is the part below the scroll end, which is off screen anyway.
  contentClip: { overflow: 'clip' },
  // One grid for every line: the speaker column is as wide as the widest speaker name
  // (max-content), and each row takes the columns as a subgrid, so names and text align
  // across rows without a fixed column width.
  content: { display: 'grid', gridTemplateColumns: 'max-content minmax(0, 1fr)', columnGap: sp(3), willChange: 'transform' },
  row: { display: 'grid', gridColumn: '1 / -1', gridTemplateColumns: 'subgrid', padding: `${sp(1)} 0` },
  turnGap: { paddingTop: sp(2.5) },
  speaker: { whiteSpace: 'nowrap', fontWeight: 'var(--font-weight-medium)' },
  // Long lines wrap at word boundaries first; a single unbroken token (a URL) still breaks
  // instead of widening the pane. `lang` on the row lets hyphenation pick the right rules.
  text: { overflowWrap: 'anywhere', hyphens: 'auto', textWrap: 'pretty' },
  // Weight, tracking and width tokens are the Tailwind theme variables the app is built with
  // (--font-weight-medium 500, --tracking-wider .05em, --container-xl 36rem in dist main CSS).
  langTag: {
    color: 'var(--ui-text-quaternary)',
    fontWeight: 'var(--font-weight-medium)',
    marginRight: sp(1.5),
    letterSpacing: 'var(--tracking-wider)'
  },
  state: {
    display: 'flex',
    flex: 1,
    minHeight: 0,
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    gap: sp(2),
    padding: `${sp(3)} ${sp(4)}`,
    textAlign: 'center'
  },
  stateTitle: {
    display: 'flex',
    alignItems: 'center',
    gap: sp(2),
    color: 'var(--ui-text-primary)',
    fontSize: 'var(--conversation-text-base-size)',
    lineHeight: 'var(--conversation-base-line-height)',
    fontWeight: 'var(--font-weight-medium)'
  },
  stateDescription: {
    maxWidth: 'var(--container-xl)',
    color: 'var(--ui-text-tertiary)',
    fontSize: 'var(--conversation-caption-base-size)',
    lineHeight: 'var(--conversation-caption-base-line-height)'
  },
  stateActions: { display: 'flex', justifyContent: 'center', gap: sp(2), marginTop: sp(1) },
  notice: {
    display: 'flex',
    alignItems: 'center',
    gap: sp(2),
    margin: `${sp(1)} ${sp(3)} 0`,
    padding: `${sp(1.5)} ${sp(2.5)}`,
    borderRadius: 'var(--radius-md)',
    background: 'var(--ui-bg-tertiary)',
    color: 'var(--ui-text-secondary)',
    fontSize: 'var(--conversation-caption-base-size)',
    lineHeight: 'var(--conversation-caption-base-line-height)'
  },
  noticeText: { flex: 1, minWidth: 0, overflowWrap: 'anywhere' },
  endMarker: {
    gridColumn: '1 / -1',
    padding: `${sp(3)} 0 ${sp(1)}`,
    textAlign: 'center',
    color: 'var(--ui-text-quaternary)',
    fontSize: 'var(--conversation-caption-base-size)',
    lineHeight: 'var(--conversation-caption-base-line-height)'
  },
  doctor: { flex: 1, minHeight: 0, overflowY: 'auto', padding: `${sp(2)} ${sp(3)}` },
  doctorHead: { display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: sp(2), marginBottom: sp(2) },
  check: { display: 'grid', gridTemplateColumns: `${sp(4)} minmax(0, 1fr)`, columnGap: sp(2), padding: `${sp(1.5)} 0` },
  checkLabel: { color: 'var(--ui-text-primary)', fontSize: 'var(--conversation-text-base-size)' },
  checkDetail: {
    color: 'var(--ui-text-tertiary)',
    fontSize: 'var(--conversation-caption-base-size)',
    lineHeight: 'var(--conversation-caption-base-line-height)'
  },
  chip: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: sp(1.5),
    height: '100%',
    padding: `0 ${sp(1.5)}`,
    // The core status bar's own item size, a literal in core too: STATUSBAR_ACTION_CLASS
    // `text-[0.6875rem]` (apps/desktop/src/app/shell/statusbar-controls.tsx:31). No token carries it.
    fontSize: '0.6875rem',
    color: 'var(--ui-text-tertiary)'
  },
  chipGroup: { display: 'inline-flex', alignItems: 'center', gap: sp(1) },
  probe: { display: 'none' },
  lookPanel: { display: 'flex', flexDirection: 'column', gap: sp(2), width: 'auto' },
  lookRow: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: sp(3) },
  lookLabel: { color: 'var(--ui-text-secondary)', fontSize: 'var(--conversation-caption-base-size)' },
  chipName: { color: 'var(--ui-text-secondary)' },
  // A fixed width while a session shows text: the status bar's right cluster is laid out from the
  // right edge, so a width that followed each translation would push Smart and the version
  // sideways on every line (measured as layout shift in the status bar).
  chipText: { width: sp(64), overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  visuallyHidden: {
    position: 'absolute',
    width: '1px',
    height: '1px',
    margin: '-1px',
    padding: 0,
    overflow: 'hidden',
    clip: 'rect(0 0 0 0)',
    whiteSpace: 'nowrap',
    border: 0
  }
}

const CHECK_LOOK = {
  ok: { icon: 'CheckCircle2', color: 'var(--ui-green)' },
  warn: { icon: 'AlertTriangle', color: 'var(--ui-yellow)' },
  fail: { icon: 'AlertCircle', color: 'var(--ui-red)' }
}

// ---------------------------------------------------------------------------
// Components
// ---------------------------------------------------------------------------

function statusTone(state) {
  if (state === 'live') {
    return 'good'
  }

  if (state === 'failed') {
    return 'bad'
  }

  return state === 'starting' || state === 'reconnecting' || state === 'stopping' ? 'warn' : 'muted'
}

function languageNames(status) {
  const names = {}

  for (const lang of Array.isArray(status?.languages) ? status.languages : []) {
    if (lang && typeof lang.code === 'string') {
      names[lang.code] = typeof lang.name === 'string' ? lang.name : lang.code
    }
  }

  return names
}

function stateLabel(t, state) {
  return t(`state.${Object.prototype.hasOwnProperty.call(EN.state, state) ? state : 'unknown'}`)
}

function StatusLine({ ctrl }) {
  const t = usePluginI18n(ID)
  const state = useValue(ctrl.store)
  const link = useValue(ctrl.link)
  const action = useValue(ctrl.action)
  const status = state.status
  const names = languageNames(status)
  const heard = detectedLanguage(state)
  const target = status?.target_language
  const parts = []

  if (status?.source && typeof status.source.label === 'string') {
    parts.push(status.source.label)
  }

  if (heard) {
    parts.push(t('heard', names[heard] ?? heard))
  }

  if (typeof target === 'string' && target) {
    parts.push(t('subtitlesIn', names[target] ?? target))
  }

  return jsxs('div', {
    children: [
      jsxs('div', {
        style: S.statusRow,
        children: [
          jsx(StatusDot, { tone: statusTone(status?.state) }),
          jsx('span', { style: S.statusState, children: stateLabel(t, status?.state) }),
          ...parts.map((part, index) => jsx('span', { style: S.statusPart, children: `· ${part}` }, index))
        ]
      }),
      // Starting, reconnecting and failed messages are shown in the subtitle area. A live
      // session's message is shown here only when it says more than "live" (a capture note).
      status?.state === 'live' && status.message && status.message.trim().toLowerCase() !== 'live'
        ? jsx('div', { style: S.statusMessage, children: status.message })
        : null,
      action.error ? jsx('div', { role: 'alert', style: S.alert, children: t('actionError', action.error) }) : null
    ]
  })
}

function Picker({ disabled, label, onChange, options, placeholder, value }) {
  return jsxs(Select, {
    disabled,
    value: value || undefined,
    onValueChange: onChange,
    children: [
      jsx(SelectTrigger, { 'aria-label': label, size: 'sm', style: S.select, children: jsx(SelectValue, { placeholder }) }),
      jsx(SelectContent, {
        children: options.map(option => jsx(SelectItem, { value: option.value, children: option.label }, option.value))
      })
    ]
  })
}

function Controls({ ctrl, compact = false }) {
  const t = usePluginI18n(ID)
  const state = useValue(ctrl.store)
  const prefs = useValue(ctrl.prefs)
  // Before the first choice: the setup's default language and the whole-system source.
  const defaultLanguage = str(useValue(ctrl.setup).data?.language?.code)
  const action = useValue(ctrl.action)
  const overlay = useValue(ctrl.overlay)
  const devices = useQuery({
    // Outputs depend on the source: the backend lists only the outputs that are safe to
    // speak into with that source (docs/events.md, GET /devices).
    queryKey: [ID, 'devices', prefs.source],
    queryFn: () => ctrl.rest(devicesPath(prefs.source)),
    // Keep the previous lists on screen while the next source's lists load.
    placeholderData: previous => previous,
    staleTime: 30000,
    retry: false
  })

  const status = state.status
  const running = RUNNING_STATES.has(status?.state)
  // GET /voices is cached 60 s on the backend; ask only once speech is on or chosen.
  const wantsVoice = running ? status?.speak === true : prefs.speak
  const voicesQuery = useQuery({
    queryKey: [ID, 'voices'],
    queryFn: () => ctrl.rest('/voices'),
    enabled: wantsVoice,
    staleTime: 60000,
    retry: false
  })
  const sources = Array.isArray(devices.data?.sources) ? devices.data.sources : []
  const outputs = Array.isArray(devices.data?.outputs) ? devices.data.outputs : []
  const voices = Array.isArray(voicesQuery.data?.voices) ? voicesQuery.data.voices.filter(v => v && typeof v.voice_id === 'string') : []
  const languages = Array.isArray(status?.languages) ? status.languages.filter(l => l && typeof l.code === 'string') : []

  // While a session runs, show what the backend runs; while idle, show the user's choice.
  const target = running ? str(status?.target_language) : prefs.targetLanguage || defaultLanguage
  const speak = running ? status?.speak === true : prefs.speak
  const mode = running ? str(status?.mode) || (speak ? 'both' : 'subtitles') : prefs.speak ? prefs.mode === 'voice' ? 'voice' : 'both' : 'subtitles'
  const chosenLanguage = languages.find(l => l.code === target)
  const speakable = chosenLanguage ? chosenLanguage.speakable !== false : true
  const sourceValue = running && status?.source ? sourceKey(status.source) : prefs.source || defaultSourceKey(sources)
  const source = sources.find(s => sourceKey(s) === sourceValue) ?? null
  const outputValue = running && status?.output_device ? str(status.output_device) : prefs.outputDevice
  const output = outputs.find(o => o && o.device_id === outputValue) ?? null
  const voiceValue = running && status?.voice_id ? str(status.voice_id) : prefs.voiceId
  const applying = running && Array.isArray(status?.applying) ? status.applying : []

  // No source listed (the list failed or is empty): Start still goes, and the backend says what is missing.
  const canStart =
    !action.busy && (source !== null || sources.length === 0) && target !== '' &&
    (!prefs.speak || (speakable && output !== null))

  // Each control saves the choice; while running it also changes the session (POST /settings).
  const choose = (patch, change) => {
    ctrl.setPrefs(patch)

    if (running && change) {
      void ctrl.settings(change)
    }
  }

  const onMode = value => {
    const { patch, change } = modeChoice(value, { outputId: outputValue, outputs, voiceId: voiceValue })
    choose(patch, change)
  }

  const onStart = () => {
    const current = ctrl.prefs.get()
    const speakNow = current.speak && speakable

    void ctrl.start(
      startBody({
        ...current,
        source: source ? sourceKey(source) : '',
        targetLanguage: target,
        speak: speakNow,
        outputDevice: speakNow && output ? output.device_id : ''
      })
    )
  }

  return jsxs('div', {
    children: [
      jsxs('div', {
        style: compact ? S.controlsCompact : S.controls,
        children: [
          jsxs('div', {
            style: S.field,
            children: [
              jsx(Picker, {
                disabled: sources.length === 0 || applying.includes('source'),
                label: t('source'),
                onChange: value => {
                  const picked = sources.find(s => sourceKey(s) === value)
                  choose({ source: value }, picked ? { source: { backend: picked.backend, source_id: picked.source_id } } : null)
                },
                options: sources.map(s => ({ value: sourceKey(s), label: str(s.label) || sourceKey(s) })),
                placeholder: t('sourcePlaceholder'),
                value: source ? sourceValue : ''
              }),
              jsx(Tip, {
                label: t('refreshSources'),
                children: jsx(Button, {
                  'aria-label': t('refreshSources'),
                  disabled: devices.isFetching,
                  onClick: () => void devices.refetch(),
                  size: 'icon-xs',
                  variant: 'ghost',
                  children: jsx(icons.RefreshCw, {})
                })
              })
            ]
          }),
          languages.length > 0
            ? jsx(Picker, {
                label: t('target'),
                onChange: value => choose({ targetLanguage: value }, { target_language: value }),
                options: languages.map(l => ({ value: l.code, label: typeof l.name === 'string' ? l.name : l.code })),
                placeholder: t('targetPlaceholder'),
                value: target
              })
            : jsx('span', { style: S.statusPart, children: running && target ? target : t('noLanguages') }),
          jsx(Tip, {
            label: speakable ? t('mode') : t('notSpeakable'),
            children: jsx('span', {
              children: jsx(Picker, {
                disabled: !speakable && mode === 'subtitles',
                label: t('mode'),
                onChange: onMode,
                options: MODES.filter(m => m === 'subtitles' || speakable).map(m => ({ value: m, label: t(`modes.${m}`) })),
                placeholder: t('mode'),
                value: mode
              })
            })
          }),
          // Voice and output device sit behind one popover; they matter only in a mode with voice.
          jsxs(Popover, {
            children: [
              jsx(Tip, {
                label: speak ? t('voiceSettings') : t('needsVoiceMode'),
                children: jsx('span', {
                  children: jsx(PopoverTrigger, {
                    asChild: true,
                    children: jsxs(Button, {
                      'aria-label': t('voiceSettings'),
                      disabled: !speak || !speakable,
                      size: 'xs',
                      variant: 'ghost',
                      children: [jsx(icons.Volume2, {}), voiceLabel(voices, voiceValue, t('voicePlaceholder'))]
                    })
                  })
                })
              }),
              jsx(PopoverContent, {
                align: 'start',
                children: jsxs('div', {
                  style: S.popover,
                  children: [
                    jsx(Picker, {
                      disabled: outputs.length === 0 || applying.includes('output_device'),
                      label: t('output'),
                      onChange: value => choose({ outputDevice: value }, speak ? { output_device: value } : null),
                      options: outputs
                        .filter(o => o && typeof o.device_id === 'string' && o.device_id !== '')
                        .map(o => ({ value: o.device_id, label: str(o.label) || o.device_id })),
                      placeholder: t('outputPlaceholder'),
                      value: output ? outputValue : ''
                    }),
                    jsx(Picker, {
                      disabled: voices.length === 0 || applying.includes('voice_id'),
                      label: t('voice'),
                      onChange: value => choose({ voiceId: value }, speak ? { voice_id: value } : null),
                      options: voices.map(v => ({ value: v.voice_id, label: str(v.name) || v.voice_id })),
                      placeholder: t('voicePlaceholder'),
                      value: voices.some(v => v.voice_id === voiceValue) ? voiceValue : ''
                    }),
                    voicesQuery.isError
                      ? jsx('div', { role: 'alert', style: S.alert, children: t('voicesError', errorText(voicesQuery.error)) })
                      : null
                  ]
                })
              })
            ]
          }),
          // Look-and-feel proposal for Seher: while a session runs the button shows or hides the overlay;
          // while idle it arms the overlay for the next session.
          jsx(Tip, {
            label: overlayButtonLabel(t, running, overlay),
            children: jsx('span', {
              children: jsx(Button, {
                'aria-label': t('overlay'),
                'aria-pressed': running ? overlay.visible : overlay.armed,
                disabled: overlay.busy,
                onClick: () => void ctrl.setOverlay(!(running ? overlay.visible : overlay.armed)),
                size: 'icon-xs',
                variant: (running ? overlay.visible : overlay.armed) ? 'secondary' : 'ghost',
                children: jsx(icons.PanelBottom, {})
              })
            })
          }),
          jsx(OverlayLook, { ctrl }),
          jsx('div', {
            style: S.startStop,
            children: running
              ? jsxs(Button, {
                  disabled: action.busy || status?.state === 'stopping',
                  onClick: () => void ctrl.stop(),
                  size: 'sm',
                  variant: 'secondary',
                  children: [jsx(icons.StopFilled, {}), t('stop')]
                })
              : jsxs(Button, {
                  disabled: !canStart,
                  onClick: onStart,
                  size: 'sm',
                  children: [jsx(icons.Play, {}), t('start')]
                })
          })
        ]
      }),
      devices.isError
        ? jsx('div', { role: 'alert', style: S.alert, children: t('devicesError', errorText(devices.error)) })
        : null,
      overlay.error ? jsx('div', { role: 'alert', style: S.alert, children: t('overlayError', overlay.error) }) : null
    ]
  })
}

// Motion: short and functional (apps/desktop/DESIGN.md:490-491). The glide eases out on
// the curve the HUD uses to arrive (apps/desktop/src/styles.css:3046). Reduced motion
// settles instantly. Web Animations are not covered by the app's CSS reduced-motion
// override (styles.css:24-33), so the check is made here.
const GLIDE_MS = 180
const FADE_MS = 140
const EASE_ENTER = 'cubic-bezier(0.16, 1, 0.3, 1)'

function reducedMotion() {
  return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
}

function translateYOf(el) {
  const transform = getComputedStyle(el).transform

  if (!transform || transform === 'none') {
    return 0
  }

  const match = /matrix(3d)?\(([^)]+)\)/.exec(transform)

  if (!match) {
    return 0
  }

  const values = match[2].split(',').map(Number)

  return match[1] ? values[13] || 0 : values[5] || 0
}

const LineRow = memo(function LineRow({ color, enter, line, number, scale, showSpeaker, t }) {
  const primaryRef = useRef(null)
  const rowRef = useRef(null)
  const shownState = useRef(line.state)
  const translated = line.state === 'translated'
  const speaker =
    !showSpeaker || line.speaker_id === null ? '' : number === null ? line.speaker_id : t('speaker', number)

  // A line that arrives after the first paint fades in where it lands; it never slides
  // other lines (opacity only, so no layout shift).
  useLayoutEffect(() => {
    if (enter && rowRef.current && !reducedMotion()) {
      rowRef.current.animate([{ opacity: 0 }, { opacity: 1 }], { duration: FADE_MS, easing: 'ease-out' })
    }
  }, [])

  // The partial line is replaced in place by its translation: same row, same box, the
  // new text fades up from the old one.
  useLayoutEffect(() => {
    const before = shownState.current
    shownState.current = line.state

    if (before !== 'translated' && translated && primaryRef.current && !reducedMotion()) {
      primaryRef.current.animate([{ opacity: 0.35 }, { opacity: 1 }], { duration: FADE_MS, easing: 'ease-out' })
    }
  }, [line.state, translated])

  let primaryText = line.original
  let primaryColor = 'var(--ui-text-secondary)'
  let caption = null
  let captionColor = 'var(--ui-text-tertiary)'

  if (translated) {
    // Translation on top in the primary text colour; the original underneath, smaller and
    // dimmer, tagged with the language it was heard in.
    primaryText = line.translation
    primaryColor = 'var(--ui-text-primary)'
    caption = [
      line.source_language
        ? jsx('span', { style: S.langTag, children: line.source_language.toUpperCase() }, 'tag')
        : null,
      jsx('span', { lang: line.source_language ?? undefined, children: line.original }, 'text')
    ]
  } else if (line.state === 'partial') {
    primaryColor = 'var(--ui-text-tertiary)'
  } else if (line.state === 'committed') {
    caption = t('translating')
    captionColor = 'var(--ui-text-quaternary)'
  } else if (line.state === 'failed') {
    caption = t('failedLine', line.error)
    captionColor = 'var(--ui-red)'
  }

  return jsxs('div', {
    ref: rowRef,
    'data-line-state': line.state,
    style: showSpeaker ? { ...S.row, ...S.turnGap } : S.row,
    children: [
      jsx('div', {
        style: {
          ...S.speaker,
          color: color ?? 'var(--ui-text-tertiary)',
          fontSize: CAPTION_SIZE(scale),
          lineHeight: TEXT_LINE(scale)
        },
        title: speaker || undefined,
        children: speaker
      }),
      jsxs('div', {
        lang: translated ? line.target_language ?? undefined : line.source_language ?? undefined,
        children: [
          jsx('div', {
            ref: primaryRef,
            style: { ...S.text, color: primaryColor, fontSize: TEXT_SIZE(scale), lineHeight: TEXT_LINE(scale) },
            children: primaryText
          }),
          // Always one caption line at least, so a partial line keeps its height when the
          // caption fills in.
          jsx('div', {
            style: {
              ...S.text,
              color: captionColor,
              fontSize: CAPTION_SIZE(scale),
              lineHeight: CAPTION_LINE(scale),
              minHeight: CAPTION_LINE(scale)
            },
            children: caption ?? ' '
          })
        ]
      })
    ]
  })
})

function StateBlock({ actions, busy, description, title, tone }) {
  return jsxs('div', {
    style: S.state,
    children: [
      jsxs('div', {
        style: S.stateTitle,
        children: [
          busy ? jsx(GlyphSpinner, { ariaLabel: title }) : tone ? jsx(StatusDot, { tone }) : null,
          jsx('span', { children: title })
        ]
      }),
      description ? jsx('div', { style: S.stateDescription, children: description }) : null,
      actions ? jsx('div', { style: S.stateActions, children: actions }) : null
    ]
  })
}

function EmptyBody({ ctrl, kind, link, onDoctor, status, t }) {
  switch (kind) {
    case 'connecting':
      return jsx(StateBlock, { busy: true, title: t('connectingTitle'), description: t('connectingDescription') })
    case 'starting':
      return jsx(StateBlock, { busy: true, title: t('startingTitle'), description: status?.message || t('startingDescription') })
    case 'listening':
      return jsx(StateBlock, { tone: 'good', title: t('listeningTitle'), description: t('listeningDescription') })
    case 'reconnecting':
      return jsx(StateBlock, {
        busy: true,
        title: t('reconnectingTitle'),
        description: status?.message || t('reconnectingDescription')
      })
    case 'offline':
      return jsx(StateBlock, {
        tone: 'bad',
        title: t('offline'),
        description: link.error ? `${t('offlineHint')} ${t('lastError', link.error)}` : t('offlineHint'),
        actions: jsxs(Button, {
          onClick: () => void ctrl.resync(),
          size: 'xs',
          variant: 'secondary',
          children: [jsx(icons.RefreshCw, {}), t('retry')]
        })
      })
    case 'failed':
      return jsx(StateBlock, {
        tone: 'bad',
        title: t('failedTitle'),
        description: status?.message || t('failedFallback'),
        actions: jsxs(Button, {
          onClick: onDoctor,
          size: 'xs',
          variant: 'secondary',
          children: [jsx(icons.Wrench, {}), t('openDoctor')]
        })
      })
    default:
      return jsx(StateBlock, { tone: 'muted', title: t('emptyTitle'), description: t('emptyDescription') })
  }
}

function Notice({ ctrl, kind, link, onDoctor, status, t }) {
  if (kind === null || kind === 'stopped') {
    return null
  }

  const look = {
    offline: { tone: 'bad', text: t('noticeOffline', link.error) },
    reconnecting: { tone: 'warn', text: status?.message || t('noticeReconnecting') },
    failed: { tone: 'bad', text: t('noticeFailed', status?.message) }
  }[kind]

  return jsxs('div', {
    role: kind === 'reconnecting' ? 'status' : 'alert',
    style: S.notice,
    children: [
      jsx(StatusDot, { tone: look.tone }),
      jsx('span', { style: S.noticeText, children: look.text }),
      kind === 'offline'
        ? jsx(Button, { onClick: () => void ctrl.resync(), size: 'xs', variant: 'ghost', children: t('retry') })
        : null,
      kind === 'failed'
        ? jsx(Button, { onClick: onDoctor, size: 'xs', variant: 'ghost', children: t('openDoctor') })
        : null
    ]
  })
}

function SubtitleList({ ctrl, onDoctor, onJumpChange }) {
  const t = usePluginI18n(ID)
  const state = useValue(ctrl.store)
  const link = useValue(ctrl.link)
  const { fontScale } = useValue(ctrl.prefs)
  const listRef = useRef(null)
  const contentRef = useRef(null)
  const followRef = useRef(true)
  const jumpingRef = useRef(false)
  const lastTopRef = useRef(0)
  const glideRef = useRef(null)
  const paintedRef = useRef(false)
  const listElRef = useRef(null)
  const [follow, setFollow] = useState(true)
  const [anchorId, setAnchorId] = useState(null)
  const view = subtitleView(state, link)
  const lines = state.lines

  const setFollowing = (on, el) => {
    if (followRef.current === on) {
      return
    }

    followRef.current = on
    setFollow(on)
    setAnchorId(on ? null : lines.length > 0 ? lines[lines.length - 1].line_id : null)

    if (!on && glideRef.current) {
      glideRef.current.cancel()
      glideRef.current = null
    }

    if (el) {
      lastTopRef.current = el.scrollTop
    }
  }

  // Follow the newest line. The scroll position jumps to the bottom before paint, so it is
  // always truthful for the scroll handler; the content wrapper then glides from where it
  // was on screen to its new place with a transform. A second change during a glide starts
  // from wherever the first one had got to, so a burst of updates reads as one movement.
  useLayoutEffect(() => {
    const el = listRef.current
    const content = contentRef.current

    if (!el || !content) {
      return
    }

    if (!followRef.current) {
      lastTopRef.current = el.scrollTop

      return
    }

    // A list element that just mounted (first lines of a session) has no previous position.
    const fresh = listElRef.current !== el
    listElRef.current = el
    const before = fresh ? el.scrollTop : lastTopRef.current
    let residual = 0

    if (glideRef.current) {
      residual = translateYOf(content)
      glideRef.current.cancel()
      glideRef.current = null
    }

    el.scrollTop = el.scrollHeight
    const after = el.scrollTop
    lastTopRef.current = after
    const shift = after - before + residual

    if (!fresh && paintedRef.current && Math.abs(shift) >= 0.5 && Math.abs(shift) < el.clientHeight && !reducedMotion()) {
      const glide = content.animate([{ transform: `translateY(${shift}px)` }, { transform: 'translateY(0)' }], {
        duration: GLIDE_MS,
        easing: EASE_ENTER
      })
      glide.onfinish = () => {
        if (glideRef.current === glide) {
          glideRef.current = null
        }
      }
      glideRef.current = glide
    }

    paintedRef.current = true
  }, [lines, fontScale, view.notice, follow])

  const onScroll = () => {
    const el = listRef.current

    if (!el) {
      return
    }

    const live = atLiveEdge(el)

    if (jumpingRef.current) {
      // A smooth jump to live is under way: only its arrival counts.
      if (live) {
        jumpingRef.current = false
        lastTopRef.current = el.scrollTop
      }

      return
    }

    setFollowing(live, el)
    lastTopRef.current = el.scrollTop
  }

  // Any direct input ends a jump in progress, so the reader can stop it halfway.
  const onUserInput = () => {
    jumpingRef.current = false
  }

  const jumpToLive = () => {
    const el = listRef.current

    followRef.current = true
    setFollow(true)
    setAnchorId(null)

    if (el) {
      jumpingRef.current = !reducedMotion()
      el.scrollTo({ top: el.scrollHeight, behavior: reducedMotion() ? 'auto' : 'smooth' })
    }
  }

  const live = state.lastTranslated
  const unseen = follow ? 0 : unseenCount(lines, anchorId)
  const paused = !follow && view.body === 'lines'
  const jumpRef = useRef(jumpToLive)
  jumpRef.current = jumpToLive

  // The way back to live sits in the pane's top row (HermeneusPane), outside the list, so it
  // never covers a line. The list tells it when following stopped and how many lines arrived.
  useLayoutEffect(() => {
    onJumpChange?.(paused ? { unseen, jump: () => jumpRef.current() } : null)
  }, [paused, unseen])

  useLayoutEffect(() => () => onJumpChange?.(null), [])


  return jsxs('div', {
    style: S.listWrap,
    children: [
      jsx(Notice, { ctrl, kind: view.notice, link, onDoctor, status: state.status, t }),
      view.body === 'lines'
        ? jsx('div', {
            ref: listRef,
            className: 'scroll-fade-y',
            onKeyDown: onUserInput,
            onPointerDown: onUserInput,
            onScroll,
            onTouchStart: onUserInput,
            onWheel: onUserInput,
            role: 'log',
            'aria-label': t('subtitles'),
            style: S.list,
            tabIndex: 0,
            children: jsx('div', {
              style: S.contentClip,
              children: jsxs('div', {
                ref: contentRef,
                style: S.content,
                children: [
                  ...lines.map((line, index) =>
                    jsx(
                      LineRow,
                      {
                        color: speakerColor(state, line.speaker_id),
                        enter: paintedRef.current,
                        line,
                        number: speakerNumber(state, line.speaker_id),
                        scale: fontScale,
                        showSpeaker: startsTurn(lines, index),
                        t
                      },
                      line.line_id
                    )
                  ),
                  view.notice === 'stopped' ? jsx('div', { style: S.endMarker, children: t('noticeStopped') }, 'end') : null
                ]
              })
            })
          })
        : jsx(EmptyBody, { ctrl, kind: view.body, link, onDoctor, status: state.status, t }),
      // Screen readers hear each new translation once; partial updates stay silent.
      jsx('div', {
        'aria-live': 'polite',
        'aria-atomic': 'true',
        lang: live?.target_language ?? undefined,
        style: S.visuallyHidden,
        children: live ? live.translation : ''
      })
    ]
  })
}

function FontSize({ ctrl }) {
  const t = usePluginI18n(ID)
  const { fontScale } = useValue(ctrl.prefs)
  const index = Math.max(0, FONT_SCALES.indexOf(fontScale))
  const step = delta => {
    const next = FONT_SCALES[Math.min(FONT_SCALES.length - 1, Math.max(0, index + delta))]
    ctrl.setPrefs({ fontScale: next })
  }

  return jsxs('div', {
    style: S.fontControls,
    children: [
      jsx(Button, {
        'aria-label': t('smaller'),
        disabled: index === 0,
        onClick: () => step(-1),
        size: 'icon-xs',
        variant: 'ghost',
        children: jsx(icons.ZoomOut, {})
      }),
      jsx('span', { 'aria-hidden': true, style: S.fontScale, children: t('scale', Math.round(fontScale * 100)) }),
      jsx(Button, {
        'aria-label': t('larger'),
        disabled: index === FONT_SCALES.length - 1,
        onClick: () => step(1),
        size: 'icon-xs',
        variant: 'ghost',
        children: jsx(icons.ZoomIn, {})
      })
    ]
  })
}

function DoctorPanel({ ctrl }) {
  const t = usePluginI18n(ID)
  const doctor = useQuery({
    queryKey: [ID, 'doctor'],
    queryFn: () => ctrl.rest('/doctor'),
    staleTime: 0,
    retry: false
  })
  const checks = Array.isArray(doctor.data?.checks) ? doctor.data.checks : null

  let body

  if (doctor.isPending) {
    body = jsx(GlyphSpinner, { ariaLabel: t('doctorRunning') })
  } else if (doctor.isError) {
    body = jsx('div', { role: 'alert', style: S.alert, children: t('doctorError', errorText(doctor.error)) })
  } else if (checks === null) {
    body = jsx('div', { role: 'alert', style: S.alert, children: t('doctorShape') })
  } else {
    body = jsx('ul', {
      style: { listStyle: 'none', margin: 0, padding: 0 },
      children: checks.map((check, index) => {
        const look = CHECK_LOOK[check?.state] ?? CHECK_LOOK.warn
        const Icon = icons[look.icon]

        return jsxs(
          'li',
          {
            style: S.check,
            children: [
              jsx('span', {
                style: { color: look.color, display: 'flex', paddingTop: sp(0.5) },
                children: jsx(Icon, { 'aria-label': t(`check.${CHECK_LOOK[check?.state] ? check.state : 'warn'}`) })
              }),
              jsxs('div', {
                children: [
                  jsx('div', { style: S.checkLabel, children: str(check?.label) || str(check?.id) }),
                  check?.detail ? jsx('div', { style: S.checkDetail, children: str(check.detail) }) : null,
                  check?.fix ? jsx('div', { style: S.checkDetail, children: str(check.fix) }) : null
                ]
              })
            ]
          },
          str(check?.id) || index
        )
      })
    })
  }

  return jsxs('div', {
    style: S.doctor,
    children: [
      jsx('div', {
        style: S.doctorHead,
        children: jsxs(Button, {
          disabled: doctor.isFetching,
          onClick: () => void doctor.refetch(),
          size: 'xs',
          variant: 'secondary',
          children: [jsx(icons.RefreshCw, {}), t('runDoctor')]
        })
      }),
      body
    ]
  })
}

// First-run setup, shown in the pane until GET /setup reports it complete. Built from Hermes's own
// settings rows (ListRow) and buttons so it reads like core Settings. Proposal for Seher.
const KEY_SETTINGS_ROUTE = '/settings?tab=keys&key=ELEVENLABS_API_KEY'
const LIVE_TRANSLATE_ROUTE = '/settings?tab=config:model&aux=live_translate'

function StepState({ busy, label, tone }) {
  return jsxs('span', {
    style: S.setupState,
    children: [busy ? jsx(GlyphSpinner, { ariaLabel: label }) : jsx(StatusDot, { tone }), jsx('span', { children: label })]
  })
}

function SetupPanel({ ctrl, os }) {
  const t = usePluginI18n(ID)
  const setup = useValue(ctrl.setup)
  const state = useValue(ctrl.store)
  const prefs = useValue(ctrl.prefs)
  const action = useValue(ctrl.action)
  const data = setup.data
  const steps = setupSteps(data)

  if (!steps) {
    return jsxs('div', {
      style: S.setup,
      children: [
        setup.loadError
          ? jsx(StateBlock, {
              tone: 'bad',
              title: t('setupError', setup.loadError),
              actions: jsxs(Button, {
                onClick: () => void ctrl.refreshSetup(),
                size: 'xs',
                variant: 'secondary',
                children: [jsx(icons.RefreshCw, {}), t('retry')]
              })
            })
          : jsx(StateBlock, { busy: true, title: t('setupLoading') })
      ]
    })
  }

  // Desktop opens only http, https and mailto links (apps/shared/src/external-url.ts:10,38), so
  // System Settings is opened by the backend route; the key page is an https link.
  const open = url => void Promise.resolve(os?.openExternal ? os.openExternal(url) : false).catch(() => false)
  const busy = setup.busy
  const done = jsx(StepState, { label: t('stepDone'), tone: 'good' })
  const languages = Array.isArray(state.status?.languages) ? state.status.languages.filter(l => l && typeof l.code === 'string') : []
  const language = prefs.targetLanguage || str(data.language?.code)
  const model = data.model ?? {}
  const modelName = [str(model.provider) !== 'auto' ? str(model.provider) : '', str(model.model)].filter(Boolean).join('/')
  const audio = str(data.audio?.state)
  const ready = steps.key === 'done' && steps.audio === 'done' && language !== ''

  const keyRow = jsx(ListRow, {
    title: t('keyTitle'),
    description: steps.key === 'done' ? t('keyDone') : steps.key === 'unknown' ? t('keyUnknown') : t('keyTodo'),
    action:
      steps.key === 'done'
        ? done
        : jsxs('div', {
            style: S.setupActions,
            children: [
              jsxs(Button, {
                onClick: () => open(str(data.key?.url)),
                size: 'xs',
                variant: 'ghost',
                children: [jsx(icons.ExternalLink, {}), t('keyCreate')]
              }),
              jsxs(Button, {
                onClick: () => host.navigate(KEY_SETTINGS_ROUTE),
                size: 'xs',
                variant: 'secondary',
                children: [jsx(icons.KeyRound, {}), t('keyAdd')]
              }),
              jsx(Button, { onClick: () => void ctrl.refreshSetup(), size: 'xs', variant: 'ghost', children: t('keyRecheck') })
            ]
          })
  })

  let audioText = t('audioTodo')
  let audioAction = jsxs(Button, {
    disabled: busy !== '',
    onClick: () => void ctrl.checkAudio(),
    size: 'xs',
    variant: 'secondary',
    children: [jsx(icons.AudioLines, {}), t('audioAllow')]
  })

  if (audio === 'ok' || audio === 'not_needed') {
    audioText = audio === 'ok' ? t('audioDone') : t('audioNotNeeded', str(data.audio?.message))
    audioAction = done
  } else if (audio === 'running') {
    audioText = t('audioRunning')
    audioAction = jsx(StepState, { busy: true, label: t('audioWaiting') })
  } else if (audio === 'denied' || audio === 'failed') {
    audioText = audio === 'denied' ? t('audioDenied') : str(data.audio?.message) || t('audioTodo')
    audioAction = jsxs('div', {
      style: S.setupActions,
      children: [
        audio === 'denied'
          ? jsxs(Button, {
              onClick: () => void ctrl.openPrivacySettings(),
              size: 'xs',
              variant: 'secondary',
              children: [jsx(icons.Settings, {}), t('audioOpenSettings')]
            })
          : null,
        jsxs(Button, {
          disabled: busy !== '',
          onClick: () => void ctrl.checkAudio(),
          size: 'xs',
          variant: audio === 'denied' ? 'ghost' : 'secondary',
          children: [jsx(icons.RefreshCw, {}), t('audioAgain')]
        })
      ]
    })
  }

  const audioRow = jsx(ListRow, { title: t('audioTitle'), description: audioText, action: audioAction })

  const modelRow = jsx(ListRow, {
    title: t('modelTitle'),
    description:
      model.route === 'auxiliary'
        ? t('modelPinned', modelName)
        : steps.model === 'todo'
          ? t('modelMissing')
          : jsxs('span', { children: [t('modelMain', modelName), ' ', jsx('span', { style: S.setupDetail, children: t('modelFaster') })] }),
    action:
      model.route === 'auxiliary'
        ? done
        : jsx(Button, {
            onClick: () => host.navigate(LIVE_TRANSLATE_ROUTE),
            size: 'xs',
            variant: 'ghost',
            children: t('modelChoose')
          })
  })

  const languageRow = jsx(ListRow, {
    title: t('languageTitle'),
    description: prefs.targetLanguage ? t('languageFrom.setting') : t(`languageFrom.${str(data.language?.from) || 'fallback'}`),
    action:
      languages.length > 0
        ? jsx(Picker, {
            label: t('target'),
            onChange: value => ctrl.setPrefs({ targetLanguage: value }),
            options: languages.map(l => ({ value: l.code, label: typeof l.name === 'string' ? l.name : l.code })),
            placeholder: t('targetPlaceholder'),
            value: language
          })
        : jsx('span', { style: S.statusPart, children: language })
  })

  return jsxs('section', {
    'aria-label': t('setupTitle'),
    'data-hermeneus-setup': audio || 'unknown',
    style: S.setup,
    children: [
      jsxs('div', {
        style: S.setupHead,
        children: [
          jsxs('div', {
            children: [
              jsx('div', { style: S.setupTitle, children: t('setupTitle') }),
              jsx('div', { style: S.statusMessage, children: t('setupIntro') })
            ]
          })
        ]
      }),
      keyRow,
      audioRow,
      modelRow,
      languageRow,
      setup.error ? jsx('div', { role: 'alert', style: S.alert, children: t('actionError', setup.error) }) : null,
      action.error ? jsx('div', { role: 'alert', style: S.alert, children: t('actionError', action.error) }) : null,
      jsxs('div', {
        style: S.setupFoot,
        children: [
          jsx('span', { style: S.statusMessage, children: ready ? t('setupStartHint') : t('setupNotReady') }),
          jsxs(Button, {
            disabled: !ready || action.busy,
            onClick: () => void ctrl.quickStart(),
            size: 'sm',
            children: [jsx(icons.Play, {}), t('start')]
          })
        ]
      })
    ]
  })
}

function HermeneusPane({ ctrl }) {
  const t = usePluginI18n(ID)
  const [view, setView] = useState('subtitles')
  const [jump, setJump] = useState(null)
  // Guided setup until it is complete; a running session always shows its subtitles.
  const setupData = useValue(ctrl.setup).data
  const running = RUNNING_STATES.has(useValue(ctrl.store).status?.state)
  const guided = view === 'subtitles' && !running && setupNeeded(setupData)

  return jsxs('div', {
    'data-hermeneus-pane': view,
    style: S.pane,
    children: [
      jsxs('div', {
        style: S.header,
        children: [
          // One row for where things stand and how to look at them, so the lines keep the height.
          jsxs('div', {
            style: S.topRow,
            children: [
              jsx('div', { style: S.topStatus, children: jsx(StatusLine, { ctrl }) }),
              view === 'subtitles' && jump
                ? jsxs(Button, {
                    onClick: jump.jump,
                    size: 'xs',
                    variant: 'secondary',
                    children: [
                      jsx(icons.ArrowBarToDown, {}),
                      jump.unseen > 0 ? `${t('jumpToLive')} · ${t('newLines', jump.unseen)}` : t('jumpToLive')
                    ]
                  })
                : null,
              view === 'subtitles' ? jsx(FontSize, { ctrl }) : null,
              jsx(SegmentedControl, {
                onChange: setView,
                options: [
                  { id: 'subtitles', label: t('subtitles') },
                  { id: 'doctor', label: t('doctor') }
                ],
                value: view
              })
            ]
          }),
          guided ? null : jsx(Controls, { ctrl })
        ]
      }),
      guided
        ? jsx('div', { style: S.setupScroll, children: jsx(SetupPanel, { ctrl, os: ctrl.os }) })
        : view === 'subtitles'
          ? jsx(SubtitleList, { ctrl, onDoctor: () => setView('doctor'), onJumpChange: setJump })
          : jsx(DoctorPanel, { ctrl })
    ]
  })
}

function StatusChip({ ctrl }) {
  const t = usePluginI18n(ID)
  const state = useValue(ctrl.store)
  const { session, showState, last } = chipParts(state)
  const label = stateLabel(t, session)
  const overlay = useValue(ctrl.overlay)
  const running = RUNNING_STATES.has(session)
  const on = running ? overlay.visible : overlay.armed
  const overlayLabel = overlayButtonLabel(t, running, overlay)

  const setup = useValue(ctrl.setup)
  const action = useValue(ctrl.action)
  const needsSetup = !running && setupNeeded(setup.data)
  const what = needsSetup ? 'setup' : chipClick(state.status)
  const clickLabel = what === 'stop' ? t('chipStop') : what === 'setup' ? t('chipSetup') : t('chipStart')
  // One click: stop a running session, or start one with the last settings. Before setup is
  // complete the click opens the pane, where the guided setup is.
  const onClick = () => {
    if (what === 'setup') {
      ctrl.revealPane()
    } else {
      void ctrl.toggle()
    }
  }

  const chip = jsx(Tip, {
    label: action.error ? t('actionError', action.error) : `${last || t('statusBar', label)} · ${clickLabel}`,
    children: jsxs('button', {
      'aria-label': `${t('statusBar', label)}. ${clickLabel}`,
      disabled: action.busy || what === 'wait',
      onClick,
      type: 'button',
      style: { ...S.chip, ...S.chipButton },
      children: [
        jsx(StatusDot, { tone: action.error && !running ? 'bad' : statusTone(session) }),
        jsx('span', { style: S.chipName, children: EN.title }),
        showState ? jsx('span', { children: label }) : null,
        last ? jsx('span', { style: S.chipText, children: truncate(last, 80) }) : null
      ]
    })
  })

  // Look-and-feel proposal for Seher: one icon button next to the chip shows or hides the overlay
  // while a session runs, and arms it for the next session while idle.
  return jsxs('span', {
    style: S.chipGroup,
    children: [
      chip,
      jsx(Tip, {
        label: overlayLabel,
        children: jsx(Button, {
          'aria-label': overlayLabel,
          'aria-pressed': on,
          disabled: overlay.busy,
          onClick: () => void ctrl.setOverlay(!on),
          size: 'icon-xs',
          variant: on ? 'secondary' : 'ghost',
          children: jsx(icons.PanelBottom, {})
        })
      }),
      // The daily controls (source, language, mode, voice, overlay, start and stop) in a popover.
      jsxs(Popover, {
        children: [
          jsx(Tip, {
            label: t('chipMenu'),
            children: jsx('span', {
              children: jsx(PopoverTrigger, {
                asChild: true,
                children: jsx(Button, {
                  'aria-label': t('chipMenu'),
                  size: 'icon-xs',
                  variant: 'ghost',
                  children: jsx(icons.ChevronUp, {})
                })
              })
            })
          }),
          jsx(PopoverContent, {
            align: 'end',
            side: 'top',
            style: S.chipMenuPanel,
            children: jsx('div', {
              style: S.chipMenu,
              children: needsSetup
                ? jsxs(Button, { onClick: () => ctrl.revealPane(), size: 'sm', variant: 'secondary', children: [jsx(icons.Settings2, {}), t('chipSetup')] })
                : jsx(Controls, { ctrl, compact: true })
            })
          })
        ]
      }),
      jsx(ThemeProbe, { ctrl })
    ]
  })
}

/** Resolves the active theme's tokens on this plugin's own hidden element, the way the SDK guide
 *  says to for drawing outside CSS (desktop-plugin-sdk.md:2119-2122), and sends them to the overlays.
 *  It reads nothing of the app's markup: every span below is rendered here. */
function ThemeProbe({ ctrl }) {
  const t = usePluginI18n(ID)
  const { renderedMode, theme, themeName } = useTheme()
  const ref = useRef(null)
  const strings = overlayStrings(t)
  const stringsKey = JSON.stringify(strings)

  useEffect(() => {
    const root = ref.current

    if (!root || typeof getComputedStyle !== 'function') {
      return
    }

    // Desktop's ThemeProvider writes the new variables in its own effect, which React runs after
    // this child's effect (apps/desktop/src/themes/context.tsx:630). Reading in the next animation
    // frame sees what it wrote.
    return afterPaint(() => {
      const read = (index, property) => getComputedStyle(root.children[index])[property]

      void ctrl.sendTheme(readThemeTokens(read, { renderedMode, themeName }, JSON.parse(stringsKey)))
    })
    // Only a theme or language change re-reads the tokens; the status chip re-renders on every line.
  }, [renderedMode, theme, themeName, stringsKey])

  return jsx('span', {
    'aria-hidden': true,
    ref,
    style: S.probe,
    children: THEME_PROBES.map((style, i) => jsx('span', { style }, i))
  })
}

/** Run ``work`` in the next animation frame (after the parent effects of this commit); returns a canceller. */
export function afterPaint(work, raf = globalThis.requestAnimationFrame, cancel = globalThis.cancelAnimationFrame) {
  if (typeof raf !== 'function') {
    work()

    return () => {}
  }

  const id = raf(() => work())

  return () => cancel?.(id)
}

/** The overlay words in the pane's language, the same i18n copy the pane draws. */
export function overlayStrings(t) {
  return {
    speaker: t('speaker', '{n}'),
    not_translated: t('overlayNotTranslated'),
    waiting: t('overlayWaiting'),
    reconnecting: t('overlayReconnecting')
  }
}

/** The POST /overlay/theme body from a ``read(index, property)`` over the THEME_PROBES spans. */
export function readThemeTokens(read, { renderedMode, themeName }, strings) {
  const colorCount = Object.keys(OVERLAY_COLOR_TOKENS).length
  const colors = Object.fromEntries(Object.keys(OVERLAY_COLOR_TOKENS).map((role, i) => [role, read(i, 'color')]))

  return {
    ...overlayThemePayload({
      colors,
      font: read(THEME_PROBES.length - 1, 'fontFamily'),
      mode: renderedMode,
      name: themeName,
      radius: Number.parseFloat(read(THEME_PROBES.length - 2, 'borderTopLeftRadius')) || 0,
      speakers: SPEAKER_TOKENS.map((_, i) => read(colorCount + i, 'color'))
    }),
    strings
  }
}

/** One hidden span per token: colours, then --radius-lg, then --dt-font-sans. */
const THEME_PROBES = Object.freeze([
  ...[...Object.values(OVERLAY_COLOR_TOKENS), ...SPEAKER_TOKENS].map(token => ({ color: `var(${token})` })),
  { borderRadius: 'var(--radius-lg)' },
  { fontFamily: 'var(--dt-font-sans)' }
])

/** Look-and-feel proposal for Seher: the overlay's look in a small popover next to its switch.
 *  Each change is saved as the plugin setting and reaches an open overlay at once. */
function OverlayLook({ ctrl }) {
  const t = usePluginI18n(ID)
  const overlay = useValue(ctrl.overlay)
  const look = overlay.settings ?? {}
  const pick = (key, values, label) =>
    jsxs('div', {
      style: S.lookRow,
      children: [
        jsx('span', { style: S.lookLabel, children: label }),
        jsx(SegmentedControl, {
          disabled: overlay.busy,
          onChange: id => void ctrl.setOverlayLook({ [`overlay_${key}`]: Number(id) }),
          options: lookOptions(values, look[key]).map(v => ({
            id: String(v),
            label: key === 'opacity' ? t('percent', v) : String(v)
          })),
          value: String(look[key] ?? '')
        })
      ]
    })
  const toggle = (key, label) =>
    jsxs('div', {
      style: S.lookRow,
      children: [
        jsx('span', { style: S.lookLabel, children: label }),
        jsx(SegmentedControl, {
          disabled: overlay.busy,
          onChange: id => void ctrl.setOverlayLook({ [`overlay_${key}`]: id === 'on' }),
          options: [
            { id: 'off', label: t('off') },
            { id: 'on', label: t('on') }
          ],
          value: look[key] === true ? 'on' : 'off'
        })
      ]
    })

  return jsxs(Popover, {
    children: [
      jsx(PopoverTrigger, {
        asChild: true,
        children: jsx(Button, {
          'aria-label': t('overlayLook'),
          size: 'icon-xs',
          variant: 'ghost',
          children: jsx(icons.SlidersHorizontal, {})
        })
      }),
      jsxs(PopoverContent, {
        style: S.lookPanel,
        children: [
          pick('text_size', OVERLAY_TEXT_SIZES, t('overlayTextSize')),
          pick('max_lines', [1, 2, 3], t('overlayLines')),
          pick('opacity', OVERLAY_OPACITIES, t('overlayOpacity')),
          jsxs('div', {
            style: S.lookRow,
            children: [
              jsx('span', { style: S.lookLabel, children: t('overlayStyle') }),
              jsx(SegmentedControl, {
                disabled: overlay.busy,
                onChange: id => void ctrl.setOverlayLook({ overlay_style: id }),
                options: OVERLAY_STYLES.map(id => ({ id, label: t(`overlayStyles.${id}`) })),
                value: OVERLAY_STYLES.includes(look.style) ? look.style : OVERLAY_STYLES[0]
              })
            ]
          }),
          toggle('show_original', t('overlayOriginal')),
          toggle('show_speaker', t('overlaySpeaker'))
        ]
      })
    ]
  })
}

export default {
  id: ID,
  name: 'Hermeneus',
  description: (typeof navigator !== 'undefined' && /^Win/.test(navigator.platform || ''))
    ? 'Live subtitles for audio playing on this PC.' : 'Live subtitles for audio playing on this Mac.',
  register(ctx) {
    ctx.i18n.register(LOCALES)

    const ctrl = createController(ctx)

    ctx.registerMany([
      {
        id: 'pane',
        area: PANES_AREA,
        title: EN.title,
        // Sized like the core Logs pane (apps/desktop/src/app/contrib/controller.tsx:640-646),
        // taller because the pane carries its own controls above the lines.
        data: { placement: 'bottom', dock: { pane: 'workspace', pos: 'bottom' }, height: '30vh', maxHeight: '80vh' },
        render: () => jsx(HermeneusPane, { ctrl })
      },
      {
        id: 'status',
        area: STATUSBAR_AREAS.right,
        order: 125,
        render: () => jsx(StatusChip, { ctrl })
      }
    ])

    ctrl.connect()
  }
}
