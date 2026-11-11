// The overlay page's DOM code (render and fit in hermeneus_engine/overlay_page.html) under
// node --test, on a small fake DOM. The fake lays text out the way the row limit depends on:
// a .main element is ceil(characters / ROW_CHARS) rows high, and an element that is hidden, or
// inside a hidden element, is 0 high, as in a browser. No dependency is installed.

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import vm from 'node:vm'

const PAGE_PATH = process.env.OVERLAY_PAGE_HTML || new URL('../../hermeneus_engine/overlay_page.html', import.meta.url)
const html = readFileSync(PAGE_PATH, 'utf8')
const script = /<script>([\s\S]*?)<\/script>/.exec(html)[1]

const ROW_CHARS = 30
const LINE_HEIGHT = 52

class El {
  constructor(tag) {
    this.tagName = tag
    this.children = []
    this.parent = null
    this.hidden = false
    this.className = ''
    this._text = ''
    this.attrs = {}
    const classes = new Set()
    this.classList = { toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)) }
    this.style = { setProperty() {} }
  }

  get textContent() {
    return this.children.length ? this.children.map(c => c.textContent).join('') : this._text
  }

  set textContent(value) {
    this.children = []
    this._text = String(value)
  }

  _detach(child) {
    if (child.parent) {
      const list = child.parent.children
      list.splice(list.indexOf(child), 1)
    }
    child.parent = this
  }

  appendChild(child) {
    this._detach(child)
    this.children.push(child)
    return child
  }

  insertBefore(child, ref) {
    this._detach(child)
    const at = ref ? this.children.indexOf(ref) : -1
    if (at === -1) this.children.push(child)
    else this.children.splice(at, 0, child)
    return child
  }

  remove() {
    if (this.parent) {
      const list = this.parent.children
      list.splice(list.indexOf(this), 1)
      this.parent = null
    }
  }

  setAttribute(name, value) {
    this.attrs[name] = String(value)
  }

  get shown() {
    for (let el = this; el; el = el.parent) if (el.hidden) return false
    return true
  }

  getBoundingClientRect() {
    if (!this.shown) return { height: 0, width: 0 }
    const rows = this.className === 'main' ? Math.max(1, Math.ceil(this.textContent.length / ROW_CHARS)) : 1
    return { height: rows * LINE_HEIGHT, width: 0 }
  }

  get scrollWidth() {
    return this.shown ? this.textContent.length : 0
  }

  get clientWidth() {
    return this.shown ? ROW_CHARS * 2 : 0
  }
}

function loadPage(settings) {
  const container = new El('main')
  const settingsBlock = { textContent: JSON.stringify(settings) }
  let source = null
  let tick = null
  let now = 0
  const context = {
    URLSearchParams,
    setTimeout,
    clearTimeout,
    // A fake clock: 5 s of 100 ms ticks follow each event, so the display queue has given the
    // previous line its reading time and every step is on screen (the timing has its own tests).
    performance: { now: () => now * 1000 },
    setInterval: fn => {
      tick = fn
    },
    location: { search: '' },
    addEventListener() {},
    getComputedStyle: () => ({ lineHeight: `${LINE_HEIGHT}px` }),
    EventSource: function () {
      source = this
    },
    document: {
      documentElement: { style: { setProperty() {} }, setAttribute() {} },
      getElementById: id => (id === 'subtitles' ? container : id === 'hermeneus-settings' ? settingsBlock : null),
      createElement: tag => new El(tag)
    }
  }
  vm.createContext(context)
  vm.runInContext(script, context)
  const frames = []
  const snapshot = () => pageSnapshot(container)
  const wait = seconds => {
    for (let k = 0; k < Math.round(seconds * 10); k++) {
      now += 0.1
      tick()
      frames.push(snapshot())
    }
  }
  return {
    send: event => {
      // The event arrives, then 5 s of the page's 100 ms timer run (under the 7 s hold).
      source.onmessage({ data: JSON.stringify(event) })
      wait(5)
    },
    wait,
    frames,
    snapshot
  }
}

function pageSnapshot(container) {
  return container.children.filter(el => el.className === 'line').map(el => {
    const main = el.children.find(c => c.className === 'main')
    const text = main.children.find(c => c.className === 'text').textContent
    return { text, hidden: el.hidden, rows: el.hidden ? 0 : main.getBoundingClientRect().height / LINE_HEIGHT }
  })
}

function line(seq, id, state, original, translation) {
  return { type: 'line', seq, line: { line_id: id, state, original, translation, speaker_id: 's1:speaker_0' } }
}

// The sequence from A3's probe (a3-ui/final2/probes/page_rows.py), Lines = 2.
const A = 'Komite bulgularini gelecek bahar her uye devleti bolgesel bankalari ve bagimsiz denetcileri dinledikten sonra yayimlayacak'
const STEPS = [
  line(3, 'a', 'translated', 'x', A),
  line(4, 'b', 'partial', 'Thanks', ''),
  line(5, 'b', 'translated', 'Thanks', 'Tesekkurler'),
  line(6, 'b', 'translated', 'Thanks a lot', 'Cok tesekkurler'),
  line(7, 'b', 'translated', 'Thanks a lot!', 'Cok tesekkurler!'),
  line(8, 'b', 'translated', 'Thanks a lot!!', 'Cok tesekkurler!!')
]

function run(maxLines) {
  const page = loadPage({ max_lines: maxLines, text_size: 40, show_original: false, show_speaker: false })
  page.send({ type: 'hello', seq: 1, session: { state: 'live' } })
  page.send({ type: 'status', seq: 2, state: 'live' })
  return STEPS.map(step => {
    page.send(step)
    return page.snapshot()
  })
}

for (const maxLines of [1, 2, 3]) {
  test(`Lines ${maxLines}: the rows on screen stay within the limit on every update`, () => {
    for (const [i, snap] of run(maxLines).entries()) {
      const total = snap.reduce((sum, item) => sum + item.rows, 0)
      assert.ok(total <= maxLines, `step ${i + 1}: ${total} rows ${JSON.stringify(snap)}`)
      assert.ok(total >= 1, `step ${i + 1}: nothing shown`)
    }
  })

  test(`Lines ${maxLines}: no line is hidden and shown again between consecutive updates`, () => {
    const snaps = run(maxLines)
    for (let i = 3; i < snaps.length; i++) {
      // From step 3 on the same two lines are candidates (a partial line is not shown: target language
      // only); each keeps its visibility.
      assert.deepEqual(snaps[i].map(s => s.hidden), snaps[i - 1].map(s => s.hidden), `step ${i + 1}`)
    }
  })
}

// The words a run of frames put on screen, in order: each visible unit once, its ellipsis taken off.
function wordsShown(frames) {
  const seen = []
  for (const frame of frames) {
    for (const item of frame) {
      if (item.hidden || item.text === '') continue
      const text = item.text.replace(/^\u2026/, '')
      if (seen[seen.length - 1] !== text && !seen.includes(text)) seen.push(text)
    }
  }
  return seen
}

for (const maxLines of [1, 2, 3]) {
  test(`Lines ${maxLines}: a line taller than the rows goes up in units, so every word is shown and no start is cut`, () => {
    const page = loadPage({ max_lines: maxLines, text_size: 40, show_original: false, show_speaker: false })
    page.send({ type: 'status', seq: 1, state: 'live' })
    page.send(line(2, 'a', 'translated', 'x', A))
    page.wait(6)
    const units = wordsShown(page.frames)
    assert.ok(units.length > 1, JSON.stringify(units))
    assert.equal(units.join(' '), A) // every word, in order, none twice
    for (const frame of page.frames) {
      assert.ok(frame.reduce((n, s) => n + s.rows, 0) <= maxLines, JSON.stringify(frame))
      for (const item of frame) {
        // A unit on screen is whole: with its ellipsis off it is one of the units, never a cut tail.
        if (!item.hidden && item.text !== '') assert.ok(units.includes(item.text.replace(/^\u2026/, '')), item.text)
      }
    }
  })
}

test('Lines 2: units after the first start with an ellipsis on top; the first unit does not', () => {
  const page = loadPage({ max_lines: 2, text_size: 40, show_original: false, show_speaker: false })
  page.send({ type: 'status', seq: 1, state: 'live' })
  page.send(line(2, 'a', 'translated', 'x', A))
  const tops = page.frames.map(f => f.find(s => !s.hidden)).filter(Boolean).map(s => s.text)
  assert.ok(tops[0].startsWith('Komite'))
  assert.ok(tops.slice(1).some(t => t.startsWith('\u2026')))
  for (const t of tops) if (t.startsWith('Komite')) assert.ok(!t.startsWith('\u2026'))
})

test('Lines 3: a newest line that needs every row hides the older ones; its next update still shows every word', () => {
  const page = loadPage({ max_lines: 3, show_original: false, show_speaker: false })
  page.send({ type: 'status', seq: 1, state: 'live' })
  page.send(line(2, 'a', 'translated', 'x', 'short line one'))
  page.send(line(3, 'b', 'translated', 'y', 'short line two'))
  page.frames.length = 0
  page.send(line(4, 'c', 'translated', 'z', A))
  assert.equal(wordsShown(page.frames).filter(t => !t.startsWith('short')).join(' '), A)
  page.send(line(5, 'c', 'translated', 'z', A + ' bugun'))
  const snap = page.snapshot()
  assert.ok(snap.reduce((n, s) => n + s.rows, 0) <= 3)
  assert.ok(snap.filter(s => !s.hidden).map(s => s.text).join(' ').endsWith('bugun'))
})

test('a sentence the session split across two lines starts its top unit with an ellipsis', () => {
  const page = loadPage({ max_lines: 2, show_original: false, show_speaker: false })
  page.send({ type: 'status', seq: 1, state: 'live' })
  page.send(line(2, 'a', 'translated', 'x', 'Bakan bu sabah'))
  page.send(line(3, 'b', 'translated', 'y', 'telefonla Ankara\'dan uzun uzun konuştu.'))
  page.send(line(4, 'c', 'translated', 'z', 'Yeni bir cümle.'))
  const tops = page.frames.map(f => f.find(s => !s.hidden)).filter(Boolean).map(s => s.text)
  assert.ok(tops.includes('Bakan bu sabah'))
  assert.ok(tops.includes('\u2026telefonla Ankara\'dan uzun uzun konuştu.'))
  assert.ok(tops.includes('Yeni bir cümle.')) // a real sentence start never takes one
})

test('Lines 3: short lines that fit together all stay shown across updates', () => {
  const page = loadPage({ max_lines: 3, show_original: false, show_speaker: false })
  page.send({ type: 'status', seq: 1, state: 'live' })
  page.send(line(2, 'a', 'translated', 'x', 'one'))
  page.send(line(3, 'b', 'translated', 'y', 'two'))
  page.send(line(4, 'c', 'partial', 'three', ''))
  const before = page.snapshot()
  page.send(line(5, 'c', 'translated', 'three', 'uc'))
  const after = page.snapshot()
  assert.deepEqual(before.map(s => s.hidden), [false, false]) // the partial line is not shown
  assert.deepEqual(after.map(s => s.hidden), [false, false, false])
  assert.equal(after.reduce((n, s) => n + s.rows, 0), 3)
})
