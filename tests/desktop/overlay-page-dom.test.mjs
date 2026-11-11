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
  const context = {
    URLSearchParams,
    setTimeout,
    clearTimeout,
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
  return {
    send: event => source.onmessage({ data: JSON.stringify(event) }),
    snapshot: () =>
      container.children.map(el => {
        const main = el.children.find(c => c.className === 'main')
        const text = main.children.find(c => c.className === 'text').textContent
        return { text, hidden: el.hidden, rows: el.hidden ? 0 : main.getBoundingClientRect().height / LINE_HEIGHT }
      })
  }
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
    for (let i = 2; i < snaps.length; i++) {
      // From step 2 on the same two lines are candidates; each keeps its visibility.
      assert.deepEqual(snaps[i].map(s => s.hidden), snaps[i - 1].map(s => s.hidden), `step ${i + 1}`)
    }
  })
}

test('Lines 2: the long first line rolls to 2 rows, then leaves whole when the second line comes', () => {
  const snaps = run(2)
  assert.equal(snaps[0].length, 1)
  assert.equal(snaps[0][0].rows, 2)
  assert.ok(A.endsWith(snaps[0][0].text) && snaps[0][0].text !== A)
  for (const snap of snaps.slice(1)) {
    assert.equal(snap[0].hidden, true)
    assert.equal(snap[1].hidden, false)
    assert.equal(snap[1].rows, 1)
  }
})

test('Lines 3: a newest line that needs every row hides the older ones and stays cut on its next update', () => {
  const page = loadPage({ max_lines: 3, show_original: false, show_speaker: false })
  page.send({ type: 'status', seq: 1, state: 'live' })
  page.send(line(2, 'a', 'translated', 'x', 'short line one'))
  page.send(line(3, 'b', 'translated', 'y', 'short line two'))
  page.send(line(4, 'c', 'translated', 'z', A))
  let snap = page.snapshot()
  assert.deepEqual(snap.map(s => s.hidden), [true, true, false])
  assert.equal(snap[2].rows, 3)
  page.send(line(5, 'c', 'translated', 'z', A + ' bugun'))
  snap = page.snapshot()
  assert.deepEqual(snap.map(s => s.hidden), [true, true, false])
  assert.equal(snap.reduce((n, s) => n + s.rows, 0), 3)
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
  assert.deepEqual(before.map(s => s.hidden), [false, false, false])
  assert.deepEqual(after.map(s => s.hidden), [false, false, false])
  assert.equal(after.reduce((n, s) => n + s.rows, 0), 3)
})
