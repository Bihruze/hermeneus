// Tests for the pure subtitle reducer and the controller in desktop/plugin.js.
// Run from the repo root: node --test tests/desktop/

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { plugin } from './load-plugin.mjs'

const {
  applyEvent,
  applyResync,
  createController,
  OVERLAY_COLOR_TOKENS,
  OVERLAY_TEXT_SIZES,
  overlayThemePayload,
  createSubtitleState,
  detectedLanguage,
  devicesPath,
  errorText,
  LINE_LIMIT,
  SPEAKER_TOKENS,
  speakerColor,
  speakerNumber,
  truncate
} = plugin

function line(id, state, extra = {}) {
  return {
    line_id: id,
    state,
    original: `original ${id}`,
    translation: state === 'translated' ? `translation ${id}` : '',
    speaker_id: 's1:speaker_0',
    source_language: 'en',
    target_language: 'tr',
    segment_index: 0,
    error: '',
    ...extra
  }
}

const lineEvent = (seq, l) => ({ type: 'line', seq, line: l })

function run(events, state = createSubtitleState()) {
  return events.reduce(applyEvent, state)
}

test('a line update with the same line_id replaces the line in place', () => {
  const state = run([
    lineEvent(1, line('a', 'partial', { original: 'hel' })),
    lineEvent(2, line('b', 'partial')),
    lineEvent(3, line('a', 'partial', { original: 'hello' })),
    lineEvent(4, line('a', 'translated', { original: 'hello', translation: 'merhaba' }))
  ])

  assert.deepEqual(
    state.lines.map(l => [l.line_id, l.state, l.original, l.translation]),
    [
      ['a', 'translated', 'hello', 'merhaba'],
      ['b', 'partial', 'original b', '']
    ]
  )
  assert.equal(state.seq, 4)
})

test('an event whose seq is not above the applied seq is ignored', () => {
  const state = run([
    lineEvent(5, line('a', 'translated', { translation: 'new' })),
    lineEvent(4, line('a', 'partial', { original: 'old' })),
    lineEvent(5, line('a', 'partial', { original: 'dup' }))
  ])

  assert.equal(state.lines[0].state, 'translated')
  assert.equal(state.lines[0].translation, 'new')
  assert.equal(state.seq, 5)
})

test('relabel moves every earlier from line to the to speaker', () => {
  let state = run([
    lineEvent(1, line('a', 'translated', { speaker_id: 's1:speaker_0' })),
    lineEvent(2, line('b', 'translated', { speaker_id: 's1:speaker_1' })),
    lineEvent(3, line('c', 'partial', { speaker_id: 's1:speaker_1' }))
  ])
  const colourOf0 = speakerColor(state, 's1:speaker_0')

  state = applyEvent(state, { type: 'relabel', seq: 4, from: 's1:speaker_1', to: 's1:speaker_0' })

  assert.deepEqual(state.lines.map(l => l.speaker_id), ['s1:speaker_0', 's1:speaker_0', 's1:speaker_0'])
  assert.equal(speakerColor(state, 's1:speaker_0'), colourOf0)
  assert.equal(state.lastTranslated.speaker_id, 's1:speaker_0')
  assert.equal(state.seq, 4)
})

test('relabel to an unseen speaker id keeps the merged speaker colour', () => {
  let state = run([lineEvent(1, line('a', 'partial', { speaker_id: 's1:speaker_3' }))])
  const before = speakerColor(state, 's1:speaker_3')

  state = applyEvent(state, { type: 'relabel', seq: 2, from: 's1:speaker_3', to: 's1:speaker_9' })

  assert.equal(state.lines[0].speaker_id, 's1:speaker_9')
  assert.equal(speakerColor(state, 's1:speaker_9'), before)
})

test('speakers get theme tokens in order of first appearance and wrap around', () => {
  const ids = Array.from({ length: SPEAKER_TOKENS.length + 1 }, (_, i) => `s1:speaker_${i}`)
  const state = run(ids.map((id, i) => lineEvent(i + 1, line(`l${i}`, 'partial', { speaker_id: id }))))

  assert.deepEqual(
    ids.map(id => speakerColor(state, id)),
    [...SPEAKER_TOKENS, SPEAKER_TOKENS[0]].map(token => plugin.speakerInk(token))
  )
  assert.equal(speakerColor(state, null), null)
  assert.ok(!SPEAKER_TOKENS.includes('--ui-red'))
})

test('resync applies the page after the current seq and takes the page seq', () => {
  let state = run([lineEvent(1, line('a', 'committed'))])

  state = applyResync(state, {
    seq: 9,
    reset: false,
    events: [
      lineEvent(1, line('a', 'partial')),
      lineEvent(2, line('a', 'translated')),
      lineEvent(3, line('b', 'partial'))
    ]
  })

  assert.deepEqual(state.lines.map(l => [l.line_id, l.state]), [
    ['a', 'translated'],
    ['b', 'partial']
  ])
  assert.equal(state.seq, 9)
})

test('a reset page replaces the lines and keeps speaker colours', () => {
  let state = run([
    lineEvent(40, line('old', 'translated', { speaker_id: 's1:speaker_2' })),
    { type: 'status', seq: 41, state: 'live', message: '' }
  ])
  const colour = speakerColor(state, 's1:speaker_2')

  state = applyResync(state, {
    seq: 3,
    reset: true,
    events: [lineEvent(2, line('new', 'translated', { speaker_id: 's1:speaker_2' }))]
  })

  assert.deepEqual(state.lines.map(l => l.line_id), ['new'])
  assert.equal(state.seq, 3)
  assert.equal(speakerColor(state, 's1:speaker_2'), colour)
  assert.equal(state.status.state, 'live')
})

test('the line list is bounded and an evicted line does not come back', () => {
  let state = createSubtitleState(3)

  for (let i = 1; i <= 5; i += 1) {
    state = applyEvent(state, lineEvent(i, line(`l${i}`, 'committed')))
  }

  assert.deepEqual(state.lines.map(l => l.line_id), ['l3', 'l4', 'l5'])

  state = applyEvent(state, lineEvent(6, line('l1', 'translated')))

  assert.deepEqual(state.lines.map(l => l.line_id), ['l3', 'l4', 'l5'])
  assert.equal(state.seq, 6)
  assert.equal(createSubtitleState().limit, LINE_LIMIT)
})

test('lastTranslated follows new translations and ignores partial updates', () => {
  let state = run([lineEvent(1, line('a', 'translated', { translation: 'bir' }))])

  assert.equal(state.lastTranslated.translation, 'bir')

  state = applyEvent(state, lineEvent(2, line('b', 'partial')))
  assert.equal(state.lastTranslated.line_id, 'a')

  state = applyEvent(state, lineEvent(3, line('b', 'translated', { translation: 'iki' })))
  assert.equal(state.lastTranslated.translation, 'iki')
})

test('hello and status events set the status; malformed events change nothing', () => {
  let state = applyEvent(createSubtitleState(), { type: 'hello', version: 1, session: { state: 'idle' } })

  assert.equal(state.status.state, 'idle')
  assert.equal(state.seq, null)

  state = applyEvent(state, { type: 'status', seq: 1, state: 'live', message: 'ok', target_language: 'tr' })
  assert.deepEqual(state.status, { state: 'live', message: 'ok', target_language: 'tr' })

  const same = state

  for (const bad of [null, 'x', lineEvent(undefined, { line_id: 'x', state: 'weird' }), lineEvent(undefined, null)]) {
    state = applyEvent(state, bad)
  }

  assert.equal(state, same)
})

test('helpers: detected language, truncation, devices path, REST error text', () => {
  const state = run([
    lineEvent(1, line('a', 'translated', { source_language: 'de' })),
    lineEvent(2, line('b', 'partial', { source_language: null }))
  ])

  assert.equal(detectedLanguage(state), 'de')
  assert.equal(detectedLanguage(applyEvent(state, { type: 'status', seq: 3, state: 'live', detected_language: 'fr' })), 'fr')
  assert.equal(detectedLanguage(createSubtitleState()), null)
  assert.equal(truncate('  short  text ', 20), 'short text')
  assert.equal(truncate('abcdefghij', 5), 'abcd\u2026')

  assert.equal(devicesPath(''), '/devices')
  assert.equal(devicesPath('virtual-device:BlackHole 2ch'), '/devices?backend=virtual-device&source_id=BlackHole%202ch')
  assert.equal(devicesPath('process-tap:com.apple.Music:1'), '/devices?backend=process-tap&source_id=com.apple.Music%3A1')

  const body = JSON.stringify({ ok: false, detail: 'a session already runs', error: 'a session already runs' })
  assert.equal(errorText(new Error(`409: ${body}`)), 'a session already runs (409)')
  assert.equal(errorText(new Error("Error invoking remote method 'hermes:api': Error: 400: " + body)), 'a session already runs (400)')
  assert.equal(errorText(new Error('404: Not Found')), '404: Not Found')
})

test('speakers from different Scribe sessions get different numbers in order of first appearance', () => {
  const state = run([
    lineEvent(1, line('a', 'translated', { speaker_id: 's1:speaker_0' })),
    lineEvent(2, line('b', 'translated', { speaker_id: 's1:speaker_1' })),
    lineEvent(3, line('c', 'translated', { speaker_id: 's2:speaker_0' })),
    lineEvent(4, line('d', 'translated', { speaker_id: 's1:speaker_0' }))
  ])

  assert.equal(speakerNumber(state, 's1:speaker_0'), 1)
  assert.equal(speakerNumber(state, 's1:speaker_1'), 2)
  assert.equal(speakerNumber(state, 's2:speaker_0'), 3)
  assert.equal(speakerNumber(state, null), null)
  assert.notEqual(speakerColor(state, 's1:speaker_0'), speakerColor(state, 's2:speaker_0'))
})

test('relabel keeps speaker numbers: the survivor keeps its own, a new id inherits', () => {
  let state = run([
    lineEvent(1, line('a', 'translated', { speaker_id: 's1:speaker_0' })),
    lineEvent(2, line('b', 'translated', { speaker_id: 's1:speaker_1' })),
    lineEvent(3, line('c', 'translated', { speaker_id: 's2:speaker_0' }))
  ])

  state = applyEvent(state, { type: 'relabel', seq: 4, from: 's1:speaker_1', to: 's1:speaker_0' })
  assert.deepEqual(state.lines.map(l => speakerNumber(state, l.speaker_id)), [1, 1, 3])

  state = applyEvent(state, { type: 'relabel', seq: 5, from: 's2:speaker_0', to: 's2:speaker_4' })
  assert.equal(speakerNumber(state, 's2:speaker_4'), 3)
  assert.equal(speakerColor(state, 's2:speaker_4'), plugin.speakerInk(SPEAKER_TOKENS[2]))

  // The next new speaker still gets the next number.
  state = applyEvent(state, lineEvent(6, line('d', 'partial', { speaker_id: 's3:speaker_0' })))
  assert.equal(speakerNumber(state, 's3:speaker_0'), 4)
})

test('hello sets the status without moving the seq', () => {
  let state = run([lineEvent(10, line('a', 'translated'))])

  state = applyEvent(state, { type: 'hello', version: 1, seq: 41, session: { state: 'live', seq: 41 } })

  assert.equal(state.seq, 10)
  assert.deepEqual(state.status, { state: 'live' })
})

test('a reset event drops the lines, keeps speaker numbers and takes its seq even when lower', () => {
  let state = run([lineEvent(90, line('old', 'translated', { speaker_id: 's1:speaker_1' }))])

  state = applyEvent(state, { type: 'reset', seq: 5 })
  assert.deepEqual(state.lines, [])
  assert.equal(state.seq, 5)
  assert.equal(speakerNumber(state, 's1:speaker_1'), 1)

  state = applyEvent(state, lineEvent(6, line('new', 'partial')))
  assert.deepEqual(state.lines.map(l => l.line_id), ['new'])
})

test('a new run clears the lines and starts speaker numbering again', () => {
  let state = run([
    { type: 'status', seq: 1, state: 'live' },
    lineEvent(2, line('a', 'translated', { speaker_id: 's1:speaker_3' })),
    { type: 'status', seq: 3, state: 'reconnecting' },
    { type: 'status', seq: 4, state: 'live' }
  ])

  assert.equal(state.lines.length, 1, 'reconnecting inside a run keeps the lines')

  state = run([{ type: 'status', seq: 5, state: 'idle' }, { type: 'status', seq: 6, state: 'starting' }], state)
  assert.deepEqual(state.lines, [])
  assert.equal(state.seq, 6)
  assert.equal(state.status.state, 'starting')

  state = applyEvent(state, lineEvent(7, line('b', 'partial', { speaker_id: 's1:speaker_0' })))
  assert.equal(speakerNumber(state, 's1:speaker_0'), 1)
})

// ---------------------------------------------------------------------------
// Controller
// ---------------------------------------------------------------------------

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })

  return { promise, reject, resolve }
}

function fakeCtx(routes) {
  const calls = []
  const ctx = {
    calls,
    frames: null,
    interval: null,
    stored: new Map(),
    rest: (path, opts) => {
      calls.push({ path, opts })
      const route = routes[path.split('?')[0]]

      return typeof route === 'function' ? route(path, opts) : Promise.resolve(route)
    },
    socket: (path, onMessage) => {
      ctx.socketPath = path
      ctx.frames = onMessage

      return () => {}
    },
    setInterval: (fn, ms) => {
      ctx.interval = { fn, ms }

      return () => {}
    },
    storage: {
      get: (key, fallback) => (ctx.stored.has(key) ? ctx.stored.get(key) : fallback),
      set: (key, value) => ctx.stored.set(key, value)
    }
  }

  return ctx
}

const settle = () => new Promise(resolve => setImmediate(resolve))

test('frames that arrive during a resync wait and are replayed after the page', async () => {
  const page = deferred()
  const ctx = fakeCtx({ '/lines': () => page.promise, '/status': { state: 'live', message: '' } })
  const ctrl = createController(ctx)

  ctrl.connect()
  assert.equal(ctx.socketPath, '/events')

  ctx.frames({ type: 'hello', version: 1, session: { state: 'live' } })
  ctx.frames(lineEvent(12, line('c', 'partial')))

  page.resolve({
    seq: 11,
    reset: false,
    events: [lineEvent(10, line('a', 'translated')), lineEvent(11, line('b', 'translated'))]
  })
  await settle()

  const state = ctrl.store.get()

  assert.deepEqual(state.lines.map(l => l.line_id), ['a', 'b', 'c'])
  assert.equal(state.seq, 12)
  assert.equal(ctrl.link.get().state, 'online')
})

test('a resync asks for lines after the last applied seq', async () => {
  const ctx = fakeCtx({ '/lines': { seq: 7, reset: true, events: [] }, '/status': { state: 'idle' } })
  const ctrl = createController(ctx)

  await ctrl.resync()
  await ctrl.resync()

  assert.deepEqual(
    ctx.calls.filter(c => c.path.startsWith('/lines')).map(c => c.path),
    ['/lines', '/lines?after=7']
  )
})

test('an unreachable backend marks the link offline and the poll tick retries after the quiet window', async () => {
  let now = 0
  let fail = true
  const ctx = fakeCtx({
    '/lines': () => (fail ? Promise.reject(new Error('HTTP 404')) : Promise.resolve({ seq: 1, events: [] })),
    '/status': { state: 'idle' }
  })
  const ctrl = createController(ctx, () => now)

  ctrl.connect()
  await settle()
  assert.deepEqual(ctrl.link.get(), { state: 'offline', error: 'HTTP 404' })

  fail = false
  now = 5000
  ctx.interval.fn()
  await settle()
  assert.equal(ctrl.link.get().state, 'offline', 'idle backend is not polled before 10 s of quiet')

  now = 10000
  ctx.interval.fn()
  await settle()
  assert.equal(ctrl.link.get().state, 'online')
})

test('start posts the chosen configuration and applies the returned status', async () => {
  const ctx = fakeCtx({ '/start': { state: 'starting', message: '', target_language: 'tr' } })
  const ctrl = createController(ctx)
  const body = { source: { backend: 'virtual-device', source_id: 'BlackHole 2ch' }, target_language: 'tr', speak: false, output_device: null }

  await ctrl.start(body)

  assert.deepEqual(ctx.calls, [{ path: '/start', opts: { method: 'POST', body } }])
  assert.equal(ctrl.store.get().status.state, 'starting')
  assert.deepEqual(ctrl.action.get(), { busy: false, error: '' })
})

test('preferences persist through plugin storage', () => {
  const ctx = fakeCtx({})
  ctx.stored.set('prefs', { fontScale: 1.3 })
  const ctrl = createController(ctx)

  assert.equal(ctrl.prefs.get().fontScale, 1.3)
  ctrl.setPrefs({ targetLanguage: 'tr' })
  assert.equal(ctx.stored.get('prefs').targetLanguage, 'tr')
  assert.equal(ctx.stored.get('prefs').fontScale, 1.3)
})

test('register contributes one pane and one status bar item and opens the event socket', () => {
  const ctx = fakeCtx({ '/lines': new Promise(() => {}) })
  const contributions = []
  let locales = null

  ctx.i18n = { register: bundles => (locales = bundles) }
  ctx.registerMany = cs => contributions.push(...cs)

  plugin.default.register(ctx)

  assert.equal(plugin.default.id, 'hermeneus')
  assert.ok(locales.en)
  assert.deepEqual(
    contributions.map(c => [c.id, c.area]),
    [
      ['pane', 'panes'],
      ['status', 'statusBar.right']
    ]
  )
  assert.equal(contributions[0].data.placement, 'bottom')
  assert.equal(ctx.socketPath, '/events')
})

test('settings posts the change and applies the status inside the answer', async () => {
  const reply = { ok: true, accepted: { target_language: 'de' }, applying: ['speak'], status: { state: 'live', target_language: 'de', applying: ['speak'] } }
  const ctx = fakeCtx({ '/settings': reply })
  const ctrl = createController(ctx)

  await ctrl.settings({ target_language: 'de', speak: true, output_device: 'Speakers' })

  assert.deepEqual(ctx.calls, [{ path: '/settings', opts: { method: 'POST', body: { target_language: 'de', speak: true, output_device: 'Speakers' } } }])
  assert.equal(ctrl.store.get().status.target_language, 'de')
  assert.deepEqual(ctrl.store.get().status.applying, ['speak'])
})

function live(ctrl) {
  ctrl.store.set({ ...ctrl.store.get(), status: { state: 'live' } })
}

function fakeHost() {
  const calls = []

  return { calls, request: (method, params) => (calls.push({ method, params }), Promise.resolve({ ok: true })) }
}

test('while a session runs the overlay switch shows or hides it now', async () => {
  let visible = false
  const ctx = fakeCtx({
    '/overlay': (path, opts) => {
      if (opts?.method === 'POST') {
        visible = opts.body.visible
      }

      return Promise.resolve({ visible, pid: visible ? 7 : null, armed: false })
    }
  })
  const deps = fakeHost()
  const ctrl = createController(ctx, undefined, deps)
  live(ctrl)

  await ctrl.setOverlay(true)
  assert.equal(ctrl.overlay.get().visible, true)
  assert.deepEqual(ctx.calls.at(-1), { path: '/overlay', opts: { method: 'POST', body: { visible: true } } })
  assert.equal(deps.calls.length, 0)

  visible = false // the overlay closed by itself (session stopped, or its own menu)
  await ctrl.refreshOverlay()
  assert.equal(ctrl.overlay.get().visible, false)
})

test('while idle the overlay switch arms the overlay setting for the next session', async () => {
  // Before: the switch and the status bar button were disabled while idle (A3 F8).
  let armed = false
  const deps = {
    calls: [],
    request: (method, params) => {
      deps.calls.push({ method, params })
      armed = params.values.overlay

      return Promise.resolve({ ok: true })
    }
  }
  const ctx = fakeCtx({ '/overlay': () => Promise.resolve({ visible: false, pid: null, armed }) })
  const ctrl = createController(ctx, undefined, deps)

  await ctrl.setOverlay(true)
  assert.deepEqual(deps.calls, [
    { method: 'plugins.manage', params: { action: 'settings', key: 'hermeneus', values: { overlay: true } } }
  ])
  assert.equal(ctrl.overlay.get().armed, true)
  assert.equal(ctx.calls.filter(c => c.opts?.method === 'POST').length, 0)
})

test('a look change is saved and applied to the open overlay', async () => {
  const deps = fakeHost()
  const ctx = fakeCtx({
    '/overlay/apply': () => Promise.resolve({ visible: true, armed: false, settings: { text_size: 34, max_lines: 2 } })
  })
  const ctrl = createController(ctx, undefined, deps)

  await ctrl.setOverlayLook({ overlay_text_size: 34 })
  assert.deepEqual(deps.calls[0].params.values, { overlay_text_size: 34 })
  assert.deepEqual(ctx.calls.at(-1), { path: '/overlay/apply', opts: { method: 'POST', body: {} } })
  assert.equal(ctrl.overlay.get().settings.text_size, 34)
})

test('an overlay refusal shows as an error and leaves the switch off', async () => {
  const ctx = fakeCtx({
    '/overlay': () => Promise.reject(new Error('no translation session is running'))
  })
  const ctrl = createController(ctx, undefined, fakeHost())
  live(ctrl)

  await ctrl.setOverlay(true)
  assert.equal(ctrl.overlay.get().visible, false)
  assert.match(ctrl.overlay.get().error, /no translation session/)
})

test('the theme is sent once per change, in the shape POST /overlay/theme takes', async () => {
  const ctx = fakeCtx({ '/overlay/theme': () => Promise.resolve({ visible: false }) })
  const ctrl = createController(ctx, undefined, fakeHost())
  const payload = overlayThemePayload({
    colors: { surface: 'rgb(255, 255, 255)', text: 'rgba(31, 35, 40, 0.94)' },
    font: "'Segoe WPC', -apple-system",
    mode: 'dark',
    name: 'midnight',
    radius: 2.4,
    speakers: ['rgb(0, 83, 253)']
  })

  assert.deepEqual(payload.colors.speakers, ['rgb(0, 83, 253)'])
  assert.equal(payload.mode, 'dark')
  await ctrl.sendTheme(payload)
  await ctrl.sendTheme(payload)
  assert.equal(ctx.calls.filter(c => c.path === '/overlay/theme').length, 1)
  await ctrl.sendTheme({ ...payload, mode: 'light' })
  assert.equal(ctx.calls.filter(c => c.path === '/overlay/theme').length, 2)
})

test('the pane resolves the same tokens the backend table names', () => {
  assert.deepEqual(Object.keys(OVERLAY_COLOR_TOKENS), ['surface', 'text', 'text_secondary', 'text_tertiary', 'stroke', 'failed'])
  assert.equal(OVERLAY_COLOR_TOKENS.failed, '--ui-red')
  assert.deepEqual([...OVERLAY_TEXT_SIZES], [20, 26, 34, 44])
})

test('the mode select shows the running mode, or the saved choice while idle', () => {
  const { shownMode } = plugin
  assert.equal(shownMode({ mode: 'voice', speak: true }, {}, true), 'voice')
  assert.equal(shownMode({ speak: true }, {}, true), 'both')
  assert.equal(shownMode({}, {}, true), 'subtitles')
  assert.equal(shownMode(null, { speak: true, mode: 'voice' }, false), 'voice')
  assert.equal(shownMode(null, { speak: true, mode: 'subtitles' }, false), 'both')
  assert.equal(shownMode(null, { speak: false, mode: 'voice' }, false), 'subtitles')
})

test('one mode choice carries speech, output and voice; no separate speak switch', () => {
  const { modeChoice } = plugin
  const outputs = [{ device_id: 'Speakers', label: 'Speakers' }, { device_id: 'AirPods', label: 'AirPods' }]
  assert.deepEqual(modeChoice('subtitles', { outputs }), { patch: { speak: false }, change: { mode: 'subtitles' } })
  assert.deepEqual(modeChoice('voice', { outputId: 'AirPods', outputs, voiceId: 'v1' }), {
    patch: { speak: true, mode: 'voice', outputDevice: 'AirPods' },
    change: { mode: 'voice', output_device: 'AirPods', voice_id: 'v1' }
  })
  // No output chosen yet: the first safe output the backend listed.
  assert.deepEqual(modeChoice('both', { outputId: 'Gone', outputs }).change, { mode: 'both', output_device: 'Speakers' })
  // No safe output at all: the backend refuses with its own message.
  assert.deepEqual(modeChoice('both', { outputs: [] }).change, { mode: 'both' })
})

test('the voice popover trigger names the chosen voice, the default voice, or the fallback', () => {
  const { voiceLabel } = plugin
  const voices = [{ voice_id: 'a', name: 'Ada' }, { voice_id: 'OZ0', name: 'Talia', is_default: true }]
  assert.equal(voiceLabel(voices, 'a', 'Default voice'), 'Ada')
  assert.equal(voiceLabel(voices, '', 'Default voice'), 'Talia')
  assert.equal(voiceLabel(voices, 'zz', 'Default voice'), 'zz')
  assert.equal(voiceLabel([], '', 'Default voice'), 'Default voice')
})

test('the controls row imports the SDK popover and no switch', async () => {
  const { readFileSync } = await import('node:fs')
  const source = readFileSync(new URL('../../desktop/plugin.js', import.meta.url), 'utf8')
  const sdk = /import\s*\{([^}]*)\}\s*from\s*'@hermes\/plugin-sdk'/.exec(source)[1]
  for (const name of ['Popover', 'PopoverContent', 'PopoverTrigger']) {
    assert.match(sdk, new RegExp(`\\b${name}\\b`))
  }
  assert.doesNotMatch(sdk, /\bSwitch\b/)
})

test('the look popover shows a value set elsewhere as one more choice', () => {
  // Before: a size of 30 set from the CLI left no option selected (A3 re-audit N3).
  const { lookOptions } = plugin
  assert.deepEqual(lookOptions([20, 26, 34, 44], 30), [20, 26, 30, 34, 44])
  assert.deepEqual(lookOptions([20, 26, 34, 44], 26), [20, 26, 34, 44])
  assert.deepEqual(lookOptions([0, 40, 80, 100], undefined), [0, 40, 80, 100])
})

test('the idle overlay tooltip says whether the overlay is armed', () => {
  // Before: it read "Opens with the next session" armed or not (A3 re-audit N4).
  const { overlayButtonLabel } = plugin
  const t = key => key
  assert.equal(overlayButtonLabel(t, false, { armed: true }), 'overlay: overlayArmed')
  assert.equal(overlayButtonLabel(t, false, { armed: false }), 'overlay: overlayNotArmed')
  assert.equal(overlayButtonLabel(t, true, { visible: true }), 'hideOverlay')
  assert.equal(overlayButtonLabel(t, true, { visible: false }), 'showOverlay')
})

test('the theme reader runs on theme changes only', async () => {
  // Before: its effect had no dependency list and read 13 styles per subtitle event (A3 re-audit N5).
  const { readFileSync } = await import('node:fs')
  const source = readFileSync(new URL('../../desktop/plugin.js', import.meta.url), 'utf8')
  const probe = source.slice(source.indexOf('function ThemeProbe('), source.indexOf('const THEME_PROBES'))
  assert.match(probe, /\}, \[renderedMode, theme, themeName, stringsKey\]\)/)
})

test('the theme is read after Desktop has written it, not in the same effect pass', () => {
  // Before: the reader ran in the child effect, before ThemeProvider's effect wrote the new variables,
  // and nothing read again (A3 reaudit2 N6). Simulate that order: reader effect, then the provider's write.
  const { afterPaint, readThemeTokens, overlayStrings } = plugin
  let surface = 'rgb(255, 255, 255)' // the old theme
  const frames = []
  const sent = []
  const read = (index, property) => (index === 0 && property === 'color' ? surface : property === 'fontFamily' ? '-apple-system' : property === 'borderTopLeftRadius' ? '2.4px' : 'rgb(1, 2, 3)')
  const t = (key, arg) => (key === 'speaker' ? `Konuşmacı ${arg}` : key)

  afterPaint(() => sent.push(readThemeTokens(read, { renderedMode: 'dark', themeName: 'midnight' }, overlayStrings(t))), fn => frames.push(fn))
  surface = 'rgb(22, 27, 34)' // ThemeProvider's own effect writes the new variables
  assert.equal(sent.length, 0)
  frames.shift()()
  assert.equal(sent[0].colors.surface, 'rgb(22, 27, 34)')
  assert.equal(sent[0].mode, 'dark')
  assert.equal(sent[0].radius_px, 2.4)
  assert.equal(sent[0].strings.speaker, 'Konuşmacı {n}')
})

test('afterPaint can be cancelled and runs at once without animation frames', () => {
  const { afterPaint } = plugin
  let ran = 0
  let cancelled = null
  const stop = afterPaint(() => ran++, () => 7, id => { cancelled = id })
  stop()
  assert.equal(cancelled, 7)
  afterPaint(() => ran++, undefined, undefined)
  assert.equal(ran, 1)
})
