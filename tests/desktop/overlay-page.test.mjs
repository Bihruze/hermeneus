// The overlay page's reducer (hermeneus_engine/overlay_page.html) under node --test.
//
// The page's one inline script exports its pure functions on globalThis.HermeneusOverlay
// and only touches the DOM when `document` exists, so it runs here in a plain vm context.

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import vm from 'node:vm'

const PAGE_URL = new URL('../../hermeneus_engine/overlay_page.html', import.meta.url)
const html = readFileSync(PAGE_URL, 'utf8')
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1])

function load() {
  const context = vm.createContext({ URLSearchParams })
  vm.runInContext(scripts[0], context)
  const api = context.HermeneusOverlay
  // Arrays made in the vm context have that context's prototypes; copy views into this one.
  return { ...api, visibleLines: (state, settings) => JSON.parse(JSON.stringify(api.visibleLines(state, settings))) }
}

const O = load()
const ALL = O.resolveSettings({}, '?max_lines=3&show_original=1&show_speaker=1')

function line(seq, id, state, original, translation = '', speaker = null) {
  return { type: 'line', seq, line: { line_id: id, state, original, translation, speaker_id: speaker } }
}

function run(events, state = O.createState()) {
  return events.reduce((s, e) => O.applyEvent(s, e), state)
}

test('the page has one inline script and loads nothing remote', () => {
  assert.equal(scripts.length, 1)
  assert.doesNotMatch(html, /https?:\/\/(?!127\.0\.0\.1)/)
  assert.doesNotMatch(html, /<link|@import|src=/)
})

test('a partial shows at once and is replaced in place by its translation', () => {
  let s = run([{ type: 'hello', seq: 1, session: { state: 'live' } }, line(2, 'a', 'partial', 'hello wor')])
  let view = O.visibleLines(s, ALL)
  assert.deepEqual(view.map(v => [v.id, v.text, v.pending, v.original]), [['a', 'hello wor', true, '']])

  s = run([line(3, 'a', 'committed', 'hello world'), line(4, 'a', 'translated', 'hello world', 'merhaba dünya')], s)
  view = O.visibleLines(s, ALL)
  assert.deepEqual(view.map(v => [v.id, v.text, v.pending, v.original]), [['a', 'merhaba dünya', false, 'hello world']])
})

test('original is hidden when show_original is off; failed lines show the original', () => {
  const settings = O.resolveSettings({ show_original: false, max_lines: 3 }, '')
  const s = run([line(1, 'a', 'translated', 'hi', 'selam'), line(2, 'b', 'failed', 'bye')])
  assert.deepEqual(O.visibleLines(s, settings).map(v => [v.text, v.original, v.pending]), [['selam', '', false], ['bye', '', false]])
})

test('at most max_lines lines, newest last, empty lines skipped', () => {
  const s = run([
    line(1, 'a', 'translated', 'one', '1'),
    line(2, 'b', 'translated', 'two', '2'),
    line(3, 'e', 'translated', '', ''),
    line(4, 'c', 'translated', 'three', '3')
  ])
  assert.deepEqual(O.visibleLines(s, O.resolveSettings({ max_lines: 2 }, '')).map(v => v.id), ['b', 'c'])
  assert.deepEqual(O.visibleLines(s, O.resolveSettings({ max_lines: 1 }, '')).map(v => v.id), ['c'])
})

test('speakers are numbered by first appearance and relabels keep the number', () => {
  let s = run([
    line(1, 'a', 'translated', 'x', 'X', 's1:speaker_1'),
    line(2, 'b', 'translated', 'y', 'Y', 's1:speaker_0'),
    line(3, 'c', 'translated', 'z', 'Z', 's1:speaker_1')
  ])
  assert.deepEqual(O.visibleLines(s, ALL).map(v => [v.speaker, v.slot]), [['Speaker 1', 1], ['Speaker 2', 2], ['Speaker 1', 1]])
  s = run([{ type: 'relabel', seq: 4, from: 's1:speaker_0', to: 's2:speaker_5' }], s)
  assert.equal(O.visibleLines(s, ALL)[1].speaker, 'Speaker 2')
  s = run([line(5, 'd', 'translated', 'w', 'W', 's2:speaker_5')], s)
  assert.equal(O.visibleLines(s, ALL)[2].speaker, 'Speaker 2')
  const off = O.resolveSettings({ show_speaker: false, max_lines: 3 }, '')
  assert.ok(O.visibleLines(s, off).every(v => v.speaker === ''))
})

test('duplicates by seq are ignored; reset and a lost stream clear the lines', () => {
  let s = run([line(5, 'a', 'translated', 'x', 'new'), line(4, 'a', 'translated', 'x', 'old')])
  assert.equal(O.visibleLines(s, ALL)[0].text, 'new')
  s = run([{ type: 'reset' }, line(2, 'b', 'translated', 'y', 'Y')], s)
  assert.deepEqual(O.visibleLines(s, ALL).map(v => v.id), ['b'])
  s = run([{ type: 'lost' }], s)
  assert.deepEqual(O.visibleLines(s, ALL), [])
})

test('nothing is shown once the session is idle or failed; a new run starts fresh', () => {
  let s = run([{ type: 'status', seq: 1, state: 'live' }, line(2, 'a', 'translated', 'x', 'X', 's1:speaker_3')])
  assert.equal(O.visibleLines(s, ALL).length, 1)
  s = run([{ type: 'status', seq: 3, state: 'idle' }], s)
  assert.deepEqual(O.visibleLines(s, ALL), [])
  s = run([{ type: 'status', seq: 4, state: 'starting' }, line(5, 'b', 'translated', 'y', 'Y', 's2:speaker_0')], s)
  assert.deepEqual(O.visibleLines(s, ALL).map(v => [v.id, v.speaker]), [['b', 'Speaker 1']])
})

test('URL query parameters override the shared settings and are clamped', () => {
  const base = { text_size: 30, max_lines: 2, show_original: true, opacity: 80, show_speaker: true }
  assert.deepEqual(
    { ...O.resolveSettings(base, '') },
    { text_size: 30, max_lines: 2, show_original: true, opacity: 80, show_speaker: true, theme: 'auto' }
  )
  assert.deepEqual(
    { ...O.resolveSettings(base, '?text_size=500&max_lines=0&show_original=0&opacity=-5&show_speaker=off&theme=dark') },
    { text_size: 64, max_lines: 1, show_original: false, opacity: 0, show_speaker: false, theme: 'dark' }
  )
  assert.equal(O.resolveSettings(base, '?theme=neon').theme, 'auto')
  assert.equal(O.resolveSettings(null, '?text_size=abc').text_size, O.DEFAULTS.text_size)
})

test('a theme event picks the forced mode, then Desktop mode, then the system appearance', () => {
  const tokens = {
    common: { '--label-weight': '600' },
    light: { '--surface': 'L' },
    dark: { '--surface': 'D' },
    default: { '--surface': 'S' },
    follow_system: true
  }
  const plain = v => JSON.parse(JSON.stringify(v))
  assert.deepEqual(plain(O.themeVars(tokens, 'auto', true)), { '--label-weight': '600', '--surface': 'D' })
  assert.deepEqual(plain(O.themeVars(tokens, 'auto', false)), { '--label-weight': '600', '--surface': 'L' })
  assert.equal(O.themeVars({ ...tokens, follow_system: false }, 'auto', false)['--surface'], 'S')
  assert.equal(O.themeVars({ ...tokens, follow_system: false }, 'light', true)['--surface'], 'L')
  const odd = O.themeVars({ ...tokens, common: { 'color': 'red', '--x': 5, '--ok': 'y' } }, 'dark', false)
  assert.deepEqual(plain(odd), { '--ok': 'y', '--surface': 'D' })
  assert.equal(O.themeVars(null, 'auto', false), null)
})
