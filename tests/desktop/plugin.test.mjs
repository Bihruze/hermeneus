// Tests for the pure subtitle reducer and the controller in desktop/plugin.js.
// Run from the repo root: node --test tests/desktop/

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { plugin } from './load-plugin.mjs'

const {
  applyEvent,
  applyResync,
  createController,
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
    [...SPEAKER_TOKENS, SPEAKER_TOKENS[0]].map(token => `var(${token})`)
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
  assert.equal(speakerColor(state, 's2:speaker_4'), `var(${SPEAKER_TOKENS[2]})`)

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
