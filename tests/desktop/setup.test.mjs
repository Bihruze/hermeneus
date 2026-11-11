// First-run setup and one-click start in desktop/plugin.js: the setup steps, the start body,
// what one click on the status bar item does, and the controller's setup, failure and retry paths.

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { plugin } from './load-plugin.mjs'

const { chipClick, clientLocales, createController, defaultSourceKey, setupNeeded, setupPath, setupSteps, startBody } =
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

test('the guided setup shows until the backend reports it complete', () => {
  assert.equal(setupNeeded(null), false) // not known yet: nothing flashes
  assert.equal(setupNeeded(SETUP), true)
  assert.equal(setupNeeded({ ...SETUP, complete: true }), false)
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
