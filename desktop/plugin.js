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
  EmptyState,
  ErrorState,
  GlyphSpinner,
  icons,
  PANES_AREA,
  SegmentedControl,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  STATUSBAR_AREAS,
  StatusDot,
  Switch,
  Tip,
  usePluginI18n,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { memo, useLayoutEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hermeneus'

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

  return `var(${SPEAKER_TOKENS[(state.speakers[speakerId] - 1) % SPEAKER_TOKENS.length]})`
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
  outputDevice: '',
  source: '',
  speak: false,
  targetLanguage: ''
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

export function createController(ctx, clock = () => Date.now()) {
  const store = atom(createSubtitleState())
  const link = atom({ state: 'connecting', error: '' })
  const action = atom({ busy: false, error: '' })
  const stored = ctx.storage.get('prefs', {})
  const prefs = atom({ ...DEFAULT_PREFS, ...(stored && typeof stored === 'object' ? stored : {}) })

  let resyncing = false
  let resyncAgain = false
  let pending = []
  let lastFrameAt = 0
  let lastResyncAt = 0

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

      link.set({ state: 'online', error: '' })
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

    // hello opens every (re)connected socket: catch up on what was missed while it was down.
    if (frame.type === 'hello') {
      void resync()
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

      if (result && typeof result === 'object' && typeof result.state === 'string') {
        store.set(applyEvent(store.get(), { ...result, type: 'status', seq: undefined }))
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

  function connect() {
    ctx.socket('/events', onFrame)
    ctx.setInterval(tick, POLL_TICK_MS)
    void resync()
  }

  return { action, connect, link, onFrame, prefs, resync, rest: ctx.rest, setPrefs, start, stop, store, tick }
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
  offlineHint: 'Enable hermeneus under plugins.enabled in config.yaml and restart the gateway.',
  source: 'Source',
  sourcePlaceholder: 'Choose a source',
  refreshSources: 'Refresh sources and devices',
  target: 'Subtitle language',
  targetPlaceholder: 'Choose a language',
  noLanguages: 'Language list unavailable',
  speak: 'Speak translation',
  notSpeakable: 'Spoken output is not available for this language',
  output: 'Output device',
  outputPlaceholder: 'Choose an output',
  start: 'Start',
  stop: 'Stop',
  devicesError: error => `Could not list sources: ${error}`,
  actionError: error => `Request failed: ${error}`,
  emptyTitle: 'No subtitles yet',
  emptyDescription: 'Press Start and play audio on this Mac.',
  speaker: n => `Speaker ${n}`,
  translating: 'translating',
  failedLine: error => (error ? `translation failed: ${error}` : 'translation failed'),
  jumpToLatest: 'Jump to latest',
  smaller: 'Smaller subtitles',
  larger: 'Larger subtitles',
  scale: percent => `${percent}%`,
  runDoctor: 'Run checks again',
  doctorRunning: 'Running checks',
  doctorError: error => `Doctor failed: ${error}`,
  doctorShape: 'Doctor returned an answer this pane cannot read.',
  check: { ok: 'ok', warn: 'warning', fail: 'failed' },
  statusBar: state => `Hermeneus: ${state}`
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
  statusState: { color: 'var(--ui-text-primary)', fontWeight: 500, flexShrink: 0 },
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
  field: { display: 'flex', alignItems: 'center', gap: sp(1), minWidth: 0 },
  select: { width: sp(44) },
  switchLabel: {
    display: 'flex',
    alignItems: 'center',
    gap: sp(1.5),
    fontSize: 'var(--conversation-caption-base-size)',
    color: 'var(--ui-text-secondary)'
  },
  toolbar: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: sp(2),
    padding: `${sp(1)} ${sp(3)}`
  },
  fontControls: { display: 'flex', alignItems: 'center', gap: sp(0.5) },
  fontScale: {
    minWidth: sp(10),
    textAlign: 'center',
    fontSize: 'var(--conversation-caption-base-size)',
    color: 'var(--ui-text-tertiary)',
    fontVariantNumeric: 'tabular-nums'
  },
  listWrap: { position: 'relative', flex: 1, minHeight: 0 },
  list: { height: '100%', overflowY: 'auto', overflowAnchor: 'auto', padding: `${sp(1)} ${sp(3)} ${sp(3)}` },
  row: { display: 'grid', gridTemplateColumns: '6.5em minmax(0, 1fr)', columnGap: sp(3), padding: `${sp(1)} 0` },
  speaker: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 500 },
  primary: { overflowWrap: 'anywhere' },
  secondary: { overflowWrap: 'anywhere' },
  jump: { position: 'absolute', right: sp(4), bottom: sp(3) },
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
    gap: sp(1),
    height: '100%',
    padding: `0 ${sp(1.5)}`,
    // The core status bar's own item size and gaps (apps/desktop/src/app/shell/statusbar-controls.tsx:31).
    fontSize: '0.6875rem',
    color: 'var(--ui-text-tertiary)'
  },
  chipText: { maxWidth: sp(64), overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
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
      status?.message ? jsx('div', { style: S.statusMessage, children: status.message }) : null,
      link.state === 'offline' && state.lines.length > 0
        ? jsx('div', { role: 'alert', style: S.alert, children: `${t('offline')}: ${link.error}. ${t('offlineHint')}` })
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

function Controls({ ctrl }) {
  const t = usePluginI18n(ID)
  const state = useValue(ctrl.store)
  const prefs = useValue(ctrl.prefs)
  const action = useValue(ctrl.action)
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
  const sources = Array.isArray(devices.data?.sources) ? devices.data.sources : []
  const outputs = Array.isArray(devices.data?.outputs) ? devices.data.outputs : []
  const languages = Array.isArray(status?.languages) ? status.languages.filter(l => l && typeof l.code === 'string') : []

  // While a session runs, show what the backend runs; while idle, show the user's choice.
  const target = running ? str(status?.target_language) : prefs.targetLanguage
  const speak = running ? status?.speak === true : prefs.speak
  const chosenLanguage = languages.find(l => l.code === target)
  const speakable = chosenLanguage ? chosenLanguage.speakable !== false : true
  const source = sources.find(s => sourceKey(s) === prefs.source) ?? null
  const output = outputs.find(o => o && o.device_id === prefs.outputDevice) ?? null

  const canStart =
    !action.busy && source !== null && target !== '' && (!prefs.speak || (speakable && output !== null))

  const onStart = () => {
    const current = ctrl.prefs.get()

    void ctrl.start({
      source: { backend: source.backend, source_id: source.source_id },
      target_language: current.targetLanguage,
      speak: current.speak && speakable,
      output_device: current.speak && speakable ? output.device_id : null
    })
  }

  return jsxs('div', {
    children: [
      jsxs('div', {
        style: S.controls,
        children: [
          jsxs('div', {
            style: S.field,
            children: [
              jsx(Picker, {
                disabled: running || sources.length === 0,
                label: t('source'),
                onChange: value => ctrl.setPrefs({ source: value }),
                options: sources.map(s => ({ value: sourceKey(s), label: str(s.label) || sourceKey(s) })),
                placeholder: t('sourcePlaceholder'),
                value: source ? prefs.source : ''
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
                disabled: running,
                label: t('target'),
                onChange: value => ctrl.setPrefs({ targetLanguage: value }),
                options: languages.map(l => ({ value: l.code, label: typeof l.name === 'string' ? l.name : l.code })),
                placeholder: t('targetPlaceholder'),
                value: target
              })
            : jsx('span', { style: S.statusPart, children: running && target ? target : t('noLanguages') }),
          jsxs('label', {
            style: S.switchLabel,
            title: speakable ? undefined : t('notSpeakable'),
            children: [
              jsx(Switch, {
                'aria-label': t('speak'),
                checked: speak && speakable,
                disabled: running || !speakable,
                onCheckedChange: on => ctrl.setPrefs({ speak: on }),
                size: 'xs'
              }),
              t('speak')
            ]
          }),
          jsx(Picker, {
            disabled: running || !prefs.speak || !speakable || outputs.length === 0,
            label: t('output'),
            onChange: value => ctrl.setPrefs({ outputDevice: value }),
            options: outputs
              .filter(o => o && typeof o.device_id === 'string' && o.device_id !== '')
              .map(o => ({ value: o.device_id, label: str(o.label) || o.device_id })),
            placeholder: t('outputPlaceholder'),
            value: output ? prefs.outputDevice : ''
          }),
          running
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
        ]
      }),
      devices.isError
        ? jsx('div', { role: 'alert', style: S.alert, children: t('devicesError', errorText(devices.error)) })
        : null
    ]
  })
}

const LineRow = memo(function LineRow({ color, line, number, scale, t }) {
  const speaker = line.speaker_id === null ? '' : number === null ? line.speaker_id : t('speaker', number)
  const translated = line.state === 'translated'

  let primaryText = line.original
  let primaryColor = 'var(--ui-text-secondary)'
  let secondaryText = ' '
  let secondaryColor = 'var(--ui-text-tertiary)'

  if (translated) {
    primaryText = line.translation
    primaryColor = 'var(--ui-text-primary)'
    secondaryText = line.original
  } else if (line.state === 'partial') {
    primaryColor = 'var(--ui-text-tertiary)'
  } else if (line.state === 'committed') {
    secondaryText = t('translating')
    secondaryColor = 'var(--ui-text-quaternary)'
  } else if (line.state === 'failed') {
    secondaryText = t('failedLine', line.error)
    secondaryColor = 'var(--ui-red)'
  }

  return jsxs('div', {
    'data-line-state': line.state,
    style: S.row,
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
            style: { ...S.primary, color: primaryColor, fontSize: TEXT_SIZE(scale), lineHeight: TEXT_LINE(scale) },
            children: primaryText
          }),
          // Always rendered, one caption line at least, so a partial line does not grow when it is translated.
          jsx('div', {
            lang: translated ? line.source_language ?? undefined : undefined,
            style: {
              ...S.secondary,
              color: secondaryColor,
              fontSize: CAPTION_SIZE(scale),
              lineHeight: CAPTION_LINE(scale),
              minHeight: CAPTION_LINE(scale)
            },
            children: secondaryText
          })
        ]
      })
    ]
  })
})

/** Pixels from the bottom that still count as following the newest line; the core Logs
 *  pane uses the same stick-to-bottom slack (apps/desktop/src/app/contrib/panes.tsx:37). */
const FOLLOW_SLACK_PX = 48

function SubtitleList({ ctrl }) {
  const t = usePluginI18n(ID)
  const state = useValue(ctrl.store)
  const link = useValue(ctrl.link)
  const { fontScale } = useValue(ctrl.prefs)
  const listRef = useRef(null)
  const [follow, setFollow] = useState(true)

  useLayoutEffect(() => {
    const el = listRef.current

    if (follow && el) {
      el.scrollTop = el.scrollHeight
    }
  }, [state.lines, follow, fontScale])

  const onScroll = () => {
    const el = listRef.current

    if (el) {
      setFollow(el.scrollHeight - el.scrollTop - el.clientHeight <= FOLLOW_SLACK_PX)
    }
  }

  const live = state.lastTranslated

  return jsxs('div', {
    style: S.listWrap,
    children: [
      jsx('div', {
        ref: listRef,
        onScroll,
        style: S.list,
        children:
          state.lines.length === 0
            ? link.state === 'offline'
              ? jsx(ErrorState, { title: t('offline'), description: `${t('offlineHint')} (${link.error})` })
              : jsx(EmptyState, { title: t('emptyTitle'), description: t('emptyDescription') })
            : state.lines.map(line =>
                jsx(
                  LineRow,
                  {
                    color: speakerColor(state, line.speaker_id),
                    line,
                    number: speakerNumber(state, line.speaker_id),
                    scale: fontScale,
                    t
                  },
                  line.line_id
                )
              )
      }),
      follow
        ? null
        : jsx('div', {
            style: S.jump,
            children: jsxs(Button, {
              onClick: () => setFollow(true),
              size: 'xs',
              variant: 'secondary',
              children: [jsx(icons.ArrowBarToDown, {}), t('jumpToLatest')]
            })
          }),
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

function HermeneusPane({ ctrl }) {
  const t = usePluginI18n(ID)
  const [view, setView] = useState('subtitles')

  return jsxs('div', {
    style: S.pane,
    children: [
      jsxs('div', { style: S.header, children: [jsx(StatusLine, { ctrl }), jsx(Controls, { ctrl })] }),
      jsxs('div', {
        style: S.toolbar,
        children: [
          jsx(SegmentedControl, {
            onChange: setView,
            options: [
              { id: 'subtitles', label: t('subtitles') },
              { id: 'doctor', label: t('doctor') }
            ],
            value: view
          }),
          view === 'subtitles' ? jsx(FontSize, { ctrl }) : null
        ]
      }),
      view === 'subtitles' ? jsx(SubtitleList, { ctrl }) : jsx(DoctorPanel, { ctrl })
    ]
  })
}

function StatusChip({ ctrl }) {
  const t = usePluginI18n(ID)
  const state = useValue(ctrl.store)
  const status = state.status?.state
  const label = stateLabel(t, status)
  const last = RUNNING_STATES.has(status) && state.lastTranslated ? state.lastTranslated.translation : ''

  return jsx(Tip, {
    label: last || t('statusBar', label),
    children: jsxs('span', {
      'aria-label': t('statusBar', label),
      role: 'status',
      style: S.chip,
      children: [
        jsx(StatusDot, { tone: statusTone(status) }),
        jsx('span', { children: label }),
        last ? jsx('span', { style: S.chipText, children: truncate(last, 80) }) : null
      ]
    })
  })
}

export default {
  id: ID,
  name: 'Hermeneus',
  description: 'Live subtitles for audio playing on this Mac.',
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
