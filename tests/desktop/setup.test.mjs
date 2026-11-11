// First-run setup and one-click start in desktop/plugin.js: the setup steps, the start body,
// what one click on the status bar item does, and the controller's setup, failure and retry paths.

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { plugin } from './load-plugin.mjs'

import { readFileSync } from 'node:fs'

const { chipClick, clientLocales, createController, defaultSourceKey, setupKnownComplete, setupNeeded, setupPath, setupSteps, setupTickDue, startBody } =
  plugin

const SETUP = {
  key: { set: false, url: 'https://elevenlabs.io/app/settings/api-keys' },
  audio: { state: 'unknown', settings_url: 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture' },
  model: { route: 'main', provider: 'nous', model: 'big', suggest_faster: true, ready: true },
  language: { code: 'tr', from: 'system' },
  complete: false
}

const settle = () => new Promise(resolve => setImmediate(resolve))

function fakeCtx(routes) {
  const calls = []
  const ctx = {
    calls,
    interval: null,
    stored: new Map(),
    rest: (path, opts) => {
      calls.push({ path, method: opts?.method ?? 'GET', body: opts?.body })
      const route = routes[path.split('?')[0]]

      return typeof route === 'function' ? route(path, opts) : Promise.resolve(route)
    },
    socket: () => () => {},
    setInterval: fn => {
      ctx.interval = fn

      return () => {}
    },
    storage: {
      get: (key, fallback) => (ctx.stored.has(key) ? ctx.stored.get(key) : fallback),
      set: (key, value) => ctx.stored.set(key, value)
    }
  }

  return ctx
}

// -- pure -------------------------------------------------------------------------------

test('setup steps: key, audio, model and language each have a state; null before the first answer', () => {
  assert.equal(setupSteps(null), null)
  assert.deepEqual(setupSteps(SETUP), { key: 'todo', audio: 'todo', model: 'done', language: 'done' })
  assert.equal(setupSteps({ ...SETUP, key: { set: true } }).key, 'done')
  assert.equal(setupSteps({ ...SETUP, key: { set: null } }).key, 'unknown')
  assert.equal(setupSteps({ ...SETUP, audio: { state: 'running' } }).audio, 'running')
  assert.equal(setupSteps({ ...SETUP, audio: { state: 'denied' } }).audio, 'error')
  assert.equal(setupSteps({ ...SETUP, audio: { state: 'failed' } }).audio, 'error')
  assert.equal(setupSteps({ ...SETUP, audio: { state: 'ok' } }).audio, 'done')
  assert.equal(setupSteps({ ...SETUP, audio: { state: 'not_needed' } }).audio, 'done')
})

test('the main Hermes model counts as set up; only a missing model is a todo', () => {
  assert.equal(setupSteps({ ...SETUP, model: { route: 'main', ready: true } }).model, 'done')
  assert.equal(setupSteps({ ...SETUP, model: { route: 'auxiliary', model: 'small' } }).model, 'done')
  assert.equal(setupSteps({ ...SETUP, model: { route: 'main', ready: false } }).model, 'todo')
})

test('the guided setup shows until the backend is known to report it complete', () => {
  // A6-2: no answer yet (or a failed read) is not "complete": the pane shows the setup's loading or
  // error state with Try again, and the chip opens the pane.
  assert.equal(setupNeeded(null), true)
  assert.equal(setupNeeded(undefined), true)
  assert.equal(setupNeeded(SETUP), true)
  assert.equal(setupNeeded({ ...SETUP, complete: true }), false)
  assert.equal(setupKnownComplete(null), false)
  // A Desktop on another computer: setup happens on the Mac (A6-7), its normal controls show.
  assert.equal(setupNeeded({ remote: true, complete: null }), false)
})

test('the tick re-reads setup every tick with no answer, every third while open, every fifteenth when done', () => {
  for (const tick of [1, 2, 3, 4]) {
    assert.equal(setupTickDue(null, tick), true)
    assert.equal(setupTickDue({ ...SETUP, complete: true, audio: { state: 'running' } }, tick), true)
  }

  assert.deepEqual([1, 2, 3, 6].map(tick => setupTickDue(SETUP, tick)), [false, false, true, true])
  const done = { ...SETUP, complete: true }

  assert.deepEqual([3, 14, 15, 30].map(tick => setupTickDue(done, tick)), [false, false, true, true])
})

test('the default source is the whole-system tap, else the only source', () => {
  const tap = { backend: 'process-tap', source_id: 'system', label: 'All system audio' }
  const app = { backend: 'process-tap', source_id: 'app:com.apple.Safari', label: 'Safari' }
  const bh = { backend: 'virtual-device', source_id: 'BlackHole 2ch', label: 'BlackHole 2ch' }

  assert.equal(defaultSourceKey([app, tap, bh]), 'process-tap:system')
  assert.equal(defaultSourceKey([bh]), 'virtual-device:BlackHole 2ch')
  assert.equal(defaultSourceKey([app, bh]), '')
  assert.equal(defaultSourceKey(undefined), '')
})

test('the start body uses the last settings, and the setup default language when none was chosen', () => {
  assert.deepEqual(startBody({ source: '', targetLanguage: '', speak: false }, 'tr'), {
    target_language: 'tr',
    speak: false,
    mode: 'subtitles'
  })
  assert.deepEqual(
    startBody(
      { source: 'process-tap:app:com.apple.Safari', targetLanguage: 'de', speak: true, mode: 'voice', outputDevice: 'AirPods', voiceId: 'v1' },
      'tr'
    ),
    {
      target_language: 'de',
      source: { backend: 'process-tap', source_id: 'app:com.apple.Safari' },
      speak: true,
      mode: 'voice',
      output_device: 'AirPods',
      voice_id: 'v1'
    }
  )
  assert.equal(startBody({ speak: true, mode: 'both' }).mode, 'both')
  assert.equal(startBody(null, '').target_language, '')
})

test('one click on the status bar item stops a running session and starts an idle or failed one', () => {
  assert.equal(chipClick({ state: 'idle' }), 'start')
  assert.equal(chipClick({ state: 'failed' }), 'start')
  assert.equal(chipClick(null), 'start')
  assert.equal(chipClick({ state: 'live' }), 'stop')
  assert.equal(chipClick({ state: 'reconnecting' }), 'stop')
  assert.equal(chipClick({ state: 'starting' }), 'wait')
  assert.equal(chipClick({ state: 'stopping' }), 'wait')
})

test('the system languages go to GET /setup as repeated locale params', () => {
  assert.deepEqual(clientLocales({ languages: ['tr-TR', 'en-US'], language: 'tr-TR' }), ['tr-TR', 'en-US'])
  assert.deepEqual(clientLocales({ language: 'de' }), ['de'])
  assert.deepEqual(clientLocales({}), [])
  assert.equal(setupPath(['tr-TR', 'en']), '/setup?locale=tr-TR&locale=en')
  assert.equal(setupPath([]), '/setup')
})

// -- controller -------------------------------------------------------------------------

test('connect reads the setup; the chip click starts with the default language and system audio', async () => {
  const ctx = fakeCtx({
    '/setup': { ...SETUP, key: { set: true }, audio: { state: 'ok' }, complete: true },
    '/start': { state: 'starting', message: '' },
    '/lines': { seq: 0, reset: true, events: [] },
    '/status': { state: 'idle' },
    '/overlay': { visible: false, armed: true }
  })
  const ctrl = createController(ctx, () => 0, { request: async () => ({}) })

  ctrl.connect()
  await settle()
  assert.equal(ctrl.setup.get().data.complete, true)

  await ctrl.toggle()
  const start = ctx.calls.find(c => c.path === '/start')

  assert.deepEqual(start.body, { target_language: 'tr', speak: false, mode: 'subtitles' })
  assert.equal(ctrl.store.get().status.state, 'starting')
})

test('the chip click stops a live session and keeps the last settings for the next start', async () => {
  const ctx = fakeCtx({
    '/setup': { ...SETUP, complete: true },
    '/start': { state: 'starting' },
    '/stop': { state: 'idle' }
  })
  const ctrl = createController(ctx, () => 0, { request: async () => ({}) })

  await ctrl.refreshSetup()
  ctrl.setPrefs({ targetLanguage: 'de', source: 'process-tap:system' })
  ctrl.store.set({ ...ctrl.store.get(), status: { state: 'live' } })
  await ctrl.toggle()
  assert.equal(ctx.calls.at(-1).path, '/stop')
  assert.equal(ctrl.store.get().status.state, 'idle')

  await ctrl.toggle()
  assert.deepEqual(ctx.calls.at(-1).body, {
    target_language: 'de',
    source: { backend: 'process-tap', source_id: 'system' },
    speak: false,
    mode: 'subtitles'
  })
  assert.deepEqual(ctx.stored.get('prefs').targetLanguage, 'de')
})

test('a failed start shows its reason and the next click tries again', async () => {
  let fail = true
  const ctx = fakeCtx({
    '/setup': { ...SETUP, complete: true },
    '/start': () =>
      fail
        ? Promise.reject(new Error('422: {"ok": false, "detail": "could not start: no ElevenLabs key"}'))
        : Promise.resolve({ state: 'starting' })
  })
  const ctrl = createController(ctx, () => 0, { request: async () => ({}) })

  await ctrl.refreshSetup()
  await ctrl.toggle()
  assert.equal(ctrl.action.get().error, 'could not start: no ElevenLabs key (422)')
  assert.equal(ctrl.action.get().busy, false)

  fail = false
  await ctrl.toggle()
  assert.equal(ctrl.action.get().error, '')
  assert.equal(ctrl.store.get().status.state, 'starting')
})

test('the audio check: running, denied, retry, then ok; the tick polls while it runs', async () => {
  let audio = { state: 'running' }
  const ctx = fakeCtx({
    '/setup': () => Promise.resolve({ ...SETUP, key: { set: true }, audio, complete: audio.state === 'ok' }),
    '/setup/audio-check': () => Promise.resolve(audio)
  })
  const ctrl = createController(ctx, () => 0, { request: async () => ({}) })

  ctrl.connect()
  await settle()
  await ctrl.checkAudio()
  assert.equal(ctrl.setup.get().data.audio.state, 'running')
  assert.equal(ctx.calls.filter(c => c.path === '/setup/audio-check').length, 1)

  audio = { state: 'denied', message: 'macOS is giving Hermeneus silence' }
  ctx.interval()
  await settle()
  assert.equal(ctrl.setup.get().data.audio.state, 'denied')
  assert.equal(setupNeeded(ctrl.setup.get().data), true)

  audio = { state: 'ok' }
  await ctrl.checkAudio()
  assert.equal(ctrl.setup.get().data.audio.state, 'ok')
  assert.equal(ctrl.setup.get().data.complete, true)
  assert.equal(ctx.calls.filter(c => c.path === '/setup/audio-check').length, 2)
})

test('a setup request that fails says why and keeps the step retryable', async () => {
  let fail = true
  const ctx = fakeCtx({
    '/setup': () => (fail ? Promise.reject(new Error('Hermes gateway unavailable')) : Promise.resolve(SETUP)),
    '/setup/audio-check': () => Promise.reject(new Error('409: {"ok": false, "detail": "a session is running; stop it first"}'))
  })
  const ctrl = createController(ctx, () => 0, { request: async () => ({}) })

  await ctrl.refreshSetup()
  assert.equal(ctrl.setup.get().loadError, 'Hermes gateway unavailable')
  assert.equal(ctrl.setup.get().data, null)

  fail = false
  await ctrl.refreshSetup()
  assert.equal(ctrl.setup.get().loadError, '')

  await ctrl.checkAudio()
  // The refusal stays visible after the refresh that follows it.
  assert.equal(ctrl.setup.get().busy, '')
  assert.equal(ctrl.setup.get().error, 'a session is running; stop it first (409)')
})

test('while setup is open the tick re-reads it every third tick, so a key added in Settings shows up', async () => {
  const ctx = fakeCtx({ '/setup': SETUP, '/lines': { seq: 0, events: [] }, '/status': { state: 'idle' } })
  const ctrl = createController(ctx, () => 0, { request: async () => ({}) })

  await ctrl.refreshSetup()
  const reads = () => ctx.calls.filter(c => c.path.startsWith('/setup')).length
  const before = reads()

  ctrl.tick()
  ctrl.tick()
  assert.equal(reads(), before)
  ctrl.tick()
  assert.equal(reads(), before + 1)
})

// -- A6-2 / A6-3: no answer, reconnect, chip start ---------------------------------------

test('a GET /setup that fails at connect is retried by the next tick until it answers', async () => {
  let fail = true
  const ctx = fakeCtx({
    '/setup': () => (fail ? Promise.reject(new Error('404: plugin routes not mounted yet')) : Promise.resolve(SETUP)),
    '/lines': { seq: 0, events: [] },
    '/status': { state: 'idle' },
    '/overlay': { visible: false, armed: true }
  })
  const ctrl = createController(ctx, () => 0, { request: async () => ({}) })

  ctrl.connect()
  await settle()
  assert.equal(ctrl.setup.get().data, null)
  assert.match(ctrl.setup.get().loadError, /not mounted/)
  assert.equal(setupNeeded(ctrl.setup.get().data), true) // the pane shows the error with Try again

  const reads = () => ctx.calls.filter(c => c.path.startsWith('/setup')).length
  const before = reads()

  ctx.interval()
  await settle()
  assert.equal(reads(), before + 1)
  fail = false
  ctx.interval()
  await settle()
  assert.equal(ctrl.setup.get().loadError, '')
  assert.equal(ctrl.setup.get().data.complete, false)
})

test('the chip opens the pane instead of starting while setup has not answered', async () => {
  const revealed = []
  const ctx = fakeCtx({ '/setup': () => Promise.reject(new Error('Hermes gateway unavailable')), '/start': { state: 'starting' } })
  const ctrl = createController(ctx, () => 0, { request: async () => ({}), revealPane: id => revealed.push(id) })

  await ctrl.toggle()
  assert.equal(ctx.calls.some(c => c.path === '/start'), false)
  assert.equal(revealed.length, 1)
})

test('a chip start re-reads setup first and opens the pane when the backend no longer reports it complete', async () => {
  // A6-3: the backend restarted; the audio check it had (or the libraries) are gone.
  let data = { ...SETUP, key: { set: true }, audio: { state: 'ok' }, complete: true }
  const revealed = []
  const ctx = fakeCtx({ '/setup': () => Promise.resolve(data), '/start': { state: 'starting' } })
  const ctrl = createController(ctx, () => 0, { request: async () => ({}), revealPane: id => revealed.push(id) })

  await ctrl.refreshSetup()
  assert.equal(setupNeeded(ctrl.setup.get().data), false)
  data = { ...data, audio: { state: 'unknown' }, complete: false }
  await ctrl.toggle()
  assert.equal(ctx.calls.some(c => c.path === '/start'), false)
  assert.equal(revealed.length, 1)
  assert.equal(setupNeeded(ctrl.setup.get().data), true) // the pane shows the audio step again

  data = { ...data, audio: { state: 'ok' }, complete: true }
  await ctrl.toggle()
  assert.equal(ctx.calls.filter(c => c.path === '/start').length, 1)
  const order = ctx.calls.map(c => c.path.split('?')[0])

  assert.equal(order.at(-2), '/setup') // read right before the start
})

test('setup is read again when the backend comes back after being offline', async () => {
  let up = true
  let data = { ...SETUP, key: { set: true }, audio: { state: 'ok' }, complete: true }
  const ctx = fakeCtx({
    '/setup': () => Promise.resolve(data),
    '/lines': () => (up ? Promise.resolve({ seq: 0, events: [] }) : Promise.reject(new Error('ECONNREFUSED'))),
    '/status': { state: 'idle' }
  })
  const ctrl = createController(ctx, () => 0, { request: async () => ({}) })

  await ctrl.resync()
  await ctrl.refreshSetup()
  up = false
  await ctrl.resync()
  assert.equal(ctrl.link.get().state, 'offline')
  // The gateway restarted and loads the libraries now; the saved check is not there.
  data = { ...data, audio: { state: 'unknown' }, complete: false }
  up = true
  await ctrl.resync()
  await settle()
  assert.equal(ctrl.link.get().state, 'online')
  assert.equal(ctrl.setup.get().data.audio.state, 'unknown')
  assert.equal(setupNeeded(ctrl.setup.get().data), true)
})

test('a hello frame from a reconnected socket reads the setup again', async () => {
  const ctx = fakeCtx({ '/setup': SETUP, '/lines': { seq: 0, events: [] }, '/status': { state: 'idle' } })
  const ctrl = createController(ctx, () => 0, { request: async () => ({}) })
  const reads = () => ctx.calls.filter(c => c.path.startsWith('/setup')).length

  ctrl.onFrame({ type: 'hello', seq: 0 })
  await settle()
  assert.equal(reads(), 1)
})

test('a slow setup answer never overwrites a newer one', async () => {
  const pending = []
  const ctx = fakeCtx({ '/setup': () => new Promise(resolve => pending.push(resolve)) })
  const ctrl = createController(ctx, () => 0, { request: async () => ({}) })

  const first = ctrl.refreshSetup()
  const second = ctrl.refreshSetup()

  pending[1]({ ...SETUP, complete: true })
  await second
  pending[0]({ ...SETUP, complete: false })
  await first
  assert.equal(ctrl.setup.get().data.complete, true)
})

// -- A6-8: System Settings through the backend route -----------------------------------

test('Open System Settings calls the backend route directly, never openExternal with the privacy URL', async () => {
  const source = readFileSync(new URL('../../desktop/plugin.js', import.meta.url), 'utf8')

  assert.doesNotMatch(source, /open\(str\(data\.audio\?\.settings_url\)\)/)
  assert.match(source, /onClick: \(\) => void ctrl\.openPrivacySettings\(\)/)

  const ctx = fakeCtx({ '/setup': SETUP, '/setup/open-privacy-settings': { ok: true } })
  const ctrl = createController(ctx, () => 0, { request: async () => ({}) })

  await ctrl.openPrivacySettings()
  assert.deepEqual(ctx.calls[0], { path: '/setup/open-privacy-settings', method: 'POST', body: {} })
})
