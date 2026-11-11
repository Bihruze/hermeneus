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
  // visibleLines draws what the display queue put up; these tests look at content, so every final line
  // is settled on screen first. The queue's timing has its own tests below (advance).
  const plain = v => JSON.parse(JSON.stringify(v))
  return {
    ...api,
    visibleLines: (state, settings, words) => plain(api.visibleLines(api.settle(state), settings, words)),
    shownNow: (state, settings, words) => plain(api.visibleLines(state, settings, words))
  }
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

test('target language only: a partial never shows its original, only the dots, until its translation', () => {
  const plain = O.resolveSettings({}, '')
  let s = run([{ type: 'hello', seq: 1, session: { state: 'live' } }, line(2, 'a', 'partial', 'hello wor')])
  assert.deepEqual(O.visibleLines(s, plain), [])
  assert.equal(O.pendingIndicator(s), true)
  s = run([line(3, 'a', 'committed', 'hello world')], s)
  assert.deepEqual(O.visibleLines(s, ALL), []) // even with show_original: a pending line shows only the dots
  assert.equal(O.pendingIndicator(s), true)
  s = run([line(4, 'a', 'translated', 'hello world', 'merhaba dünya')], s)
  assert.deepEqual(O.visibleLines(s, plain).map(v => [v.id, v.text, v.original]), [['a', 'merhaba dünya', '']])
  assert.deepEqual(O.visibleLines(s, ALL).map(v => [v.text, v.original]), [['merhaba dünya', 'hello world']])
  assert.equal(O.pendingIndicator(s), false)
})

test('a failed line shows its marker, and its original only when originals are on', () => {
  const settings = O.resolveSettings({ max_lines: 3 }, '')
  const s = run([line(1, 'a', 'translated', 'hi', 'selam'), line(2, 'b', 'failed', 'bye')])
  assert.deepEqual(O.visibleLines(s, settings).map(v => [v.text, v.marker, v.original]),
    [['selam', '', ''], ['', 'Not translated', '']])
  assert.deepEqual(O.visibleLines(s, ALL).map(v => [v.text, v.marker]), [['selam', ''], ['bye', 'Not translated']])
})

test('the queue puts a line up when its turn comes: reading speed, minimum time, order, hold', () => {
  const settings = O.resolveSettings({ max_lines: 3 }, '')
  const tr = (seq, id, text, target = 'tr') =>
    ({ type: 'line', seq, line: { line_id: id, state: 'translated', original: 'x', translation: text, speaker_id: null, target_language: target } })
  const first = 'Bu cümle tam otuz dört karakterdir.' // 35 characters: 35 / 17 s in Turkish
  let s = O.applyEvent(O.createState(), tr(1, 'a', first), 0)
  s = O.applyEvent(s, tr(2, 'b', 'Evet.'), 0)
  s = O.applyEvent(s, tr(3, 'c', 'Hayır.'), 0)
  s = O.advance(s, 0, settings)
  assert.deepEqual(O.shownNow(s, settings).map(v => v.id), ['a'])
  s = O.advance(s, 35 / 17 - 0.01, settings)
  assert.deepEqual(O.shownNow(s, settings).map(v => v.id), ['a'])
  s = O.advance(s, 35 / 17, settings)
  assert.deepEqual(O.shownNow(s, settings).map(v => v.id), ['a', 'b'])
  s = O.advance(s, 35 / 17 + 0.99, settings) // "Evet." keeps the 1 s minimum
  assert.deepEqual(O.shownNow(s, settings).map(v => v.id), ['a', 'b'])
  s = O.advance(s, 35 / 17 + 1.0, settings)
  assert.deepEqual(O.shownNow(s, settings).map(v => v.id), ['a', 'b', 'c'])
  s = O.advance(s, 35 / 17 + 1.0 + 7.1, settings) // quiet for the hold: the page clears
  assert.deepEqual(O.shownNow(s, settings), [])
  s = O.applyEvent(s, tr(4, 'd', 'Yeni.'), 20)
  s = O.advance(s, 20, settings)
  assert.deepEqual(O.shownNow(s, settings).map(v => v.id), ['d']) // the old lines do not come back
  assert.equal(O.readingCps('en-US', 0), 20)
  assert.equal(O.readingCps('tr', 99), 30)
  // A line still being translated holds the ones after it, at most 6 s.
  let q = O.applyEvent(O.createState(), { type: 'line', seq: 1, line: { line_id: 'p', state: 'committed', original: 'one', translation: '', speaker_id: null } }, 0)
  q = O.applyEvent(q, tr(2, 'n', 'İkinci.'), 0.5)
  assert.deepEqual(O.shownNow(O.advance(q, 1, settings), settings), [])
  assert.deepEqual(O.shownNow(O.advance(q, 6.1, settings), settings).map(v => v.id), ['n'])
})

test('follow live speeds up while text waits, never below the minimum or half the reading time', () => {
  const read = O.resolveSettings({ max_lines: 3 }, '')
  const live = O.resolveSettings({ max_lines: 3, pace: 'live' }, '')
  const text = 'Cümle burada bitiyor ve sonra devam ediyor ve bitiyor.' // 54 characters
  const fill = () => {
    let s = O.createState()
    for (let i = 0; i < 10; i++) {
      s = O.applyEvent(s, { type: 'line', seq: i + 1, line: { line_id: 'l' + i, state: 'translated', original: 'x', translation: text, speaker_id: null, target_language: 'tr' } }, 0)
    }
    return s
  }
  const last = settings => {
    let s = fill()
    let t = 0
    while (t < 60) { s = O.advance(s, t, settings); t = Math.round((t + 0.01) * 100) / 100 }
    return s.shown
  }
  const r = last(read)
  const l = last(live)
  assert.ok(l.l9 < r.l9 * 0.75)
  for (let i = 1; i < 10; i++) {
    assert.ok(r['l' + i] - r['l' + (i - 1)] >= 54 / 17 - 0.011)
    assert.ok(l['l' + i] - l['l' + (i - 1)] >= Math.max(1, 54 / 17 / 2) - 0.011)
  }
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
  // Compact: the number, on the first line of a turn; the full words are its title.
  assert.deepEqual(O.visibleLines(s, ALL).map(v => [v.speaker, v.slot, v.speakerTitle]),
    [['1', 1, 'Speaker 1'], ['2', 2, 'Speaker 2'], ['1', 1, 'Speaker 1']])
  s = run([{ type: 'relabel', seq: 4, from: 's1:speaker_0', to: 's2:speaker_5' }], s)
  assert.equal(O.visibleLines(s, ALL)[1].speaker, '2')
  s = run([line(5, 'd', 'translated', 'w', 'W', 's2:speaker_5')], s)
  assert.deepEqual(O.visibleLines(s, ALL).map(v => v.speaker), ['2', '1', '2'])
  s = run([line(6, 'e', 'translated', 'v', 'V', 's2:speaker_5')], s)
  assert.deepEqual(O.visibleLines(s, ALL).map(v => [v.speaker, v.slot]), [['1', 1], ['2', 2], ['', 2]]) // same turn: no label again
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
  assert.deepEqual(O.visibleLines(s, ALL).map(v => [v.id, v.speaker]), [['b', '1']])
})

test('URL query parameters override the shared settings and are clamped', () => {
  const base = { text_size: 30, max_lines: 2, show_original: true, opacity: 80, show_speaker: true }
  assert.deepEqual(
    { ...O.resolveSettings(base, '') },
    { text_size: 30, max_lines: 2, show_original: true, opacity: 80, show_speaker: true, theme: 'auto', pace: 'read', reading_cps: 0 }
  )
  assert.deepEqual(
    { ...O.resolveSettings(base, '?text_size=500&max_lines=0&show_original=0&opacity=-5&show_speaker=off&theme=dark&pace=LIVE&reading_cps=2') },
    { text_size: 64, max_lines: 1, show_original: false, opacity: 0, show_speaker: false, theme: 'dark', pace: 'live', reading_cps: 5 }
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

test('the banner style is a scheme of its own: from the shared setting or ?theme=banner', () => {
  const tokens = { common: {}, light: { '--surface': 'L' }, dark: { '--surface': 'D' }, banner: { '--surface': 'B' },
    default: { '--surface': 'S' }, follow_system: false }
  assert.equal(O.resolveSettings({ style: 'banner' }, '').theme, 'banner')
  assert.equal(O.resolveSettings({ style: 'theme' }, '').theme, 'auto')
  assert.equal(O.resolveSettings({ style: 'banner' }, '?theme=light').theme, 'light')
  assert.equal(O.resolveSettings({}, '?theme=banner').theme, 'banner')
  assert.equal(O.themeVars(tokens, 'banner', true)['--surface'], 'B')
})

test('a translated line with no translation shows as failed with the marker, as on the strip', () => {
  // Before: the page hid it while the native strip showed its original (A3 reaudit2 N7).
  const s = run([line(1, 'a', 'translated', 'Hello there', ''), line(2, 'b', 'failed', 'Fast talk')])
  assert.deepEqual(O.visibleLines(s, ALL).map(v => [v.text, v.failed, v.marker, v.original]), [
    ['Hello there', true, 'Not translated', ''],
    ['Fast talk', true, 'Not translated', '']
  ])
})

test('the page draws the pane\'s localised words', () => {
  // Before: "Speaker N" and "Not translated" were English only (A3 reaudit2 N8).
  const words = O.applyWords({ speaker: 'Speaker {n}', not_translated: 'Not translated' },
    { speaker: 'Konuşmacı {n}', not_translated: 'Çevrilmedi' })
  const s = run([line(1, 'a', 'failed', 'Fast talk', '', 's1:speaker_0')])
  assert.deepEqual(O.visibleLines(s, ALL, JSON.parse(JSON.stringify(words))).map(v => [v.speakerTitle, v.marker]), [['Konuşmacı 1', 'Çevrilmedi']])
  // A template without {n} is not taken.
  assert.equal(O.applyWords({ speaker: 'Speaker {n}', not_translated: 'x' }, { speaker: 'Konuşmacı' }).speaker, 'Speaker {n}')
})

// A fake measure: a row holds `width` characters of a line's text.
function measure(texts, width) {
  const toks = texts.map(t => O.tokensOf(t))
  return {
    counts: toks.map(t => t.parts.length),
    rowsOf: (i, cut) => Math.max(1, Math.ceil(toks[i].parts.slice(cut).join(toks[i].joiner).length / width)),
    shown: (i, cut) => toks[i].parts.slice(cut).join(toks[i].joiner)
  }
}

test('Lines limits rendered rows: one long sentence at Lines 1 keeps its newest words on one row', () => {
  const long = 'the committee will meet again on thursday to vote on the final version of the budget'
  const m = measure([long], 30)
  const fit = O.fitRows(m.counts, 1, m.rowsOf)
  assert.equal(fit.first, 0)
  assert.ok(fit.cut > 0)
  assert.equal(m.rowsOf(0, fit.cut), 1)
  assert.ok(m.rowsOf(0, fit.cut - 1) > 1, 'the smallest cut that fits')
  assert.ok(long.endsWith(m.shown(0, fit.cut)))
})

test('Lines 2: older lines leave whole before the newest rolls', () => {
  const m = measure(['short one', 'a second line that wraps onto two rows here'], 25)
  assert.deepEqual(plainFit(O.fitRows(m.counts, 2, m.rowsOf)), { first: 1, cut: 0 })
  const fits = measure(['short one', 'fits on one row'], 25)
  assert.deepEqual(plainFit(O.fitRows(fits.counts, 2, fits.rowsOf)), { first: 0, cut: 0 })
  const three = measure(['x', 'a newest line long enough to need three rows of text at this width'], 25)
  const fit = O.fitRows(three.counts, 2, three.rowsOf)
  assert.equal(fit.first, 1)
  assert.equal(three.rowsOf(1, fit.cut), 2)
})

test('text without spaces rolls by characters; a line is never emptied', () => {
  assert.deepEqual(plainFit(O.tokensOf('今日は良い天気です')), { parts: [...'今日は良い天気です'], joiner: '' })
  const m = measure(['今日は良い天気ですね明日も晴れるでしょう'], 5)
  const fit = O.fitRows(m.counts, 1, m.rowsOf)
  assert.equal(m.rowsOf(0, fit.cut), 1)
  const one = measure(['Supercalifragilistic'], 5)
  const oneFit = O.fitRows(one.counts, 1, one.rowsOf)
  assert.equal(one.shown(0, oneFit.cut), 'istic')
  const dot = measure(['.'], 5)
  assert.equal(O.fitRows(dot.counts, 1, dot.rowsOf).cut, 0)
  assert.deepEqual(plainFit(O.fitRows([], 2, () => 1)), { first: 0, cut: 0 })
})

function plainFit(v) {
  return JSON.parse(JSON.stringify(v))
}
