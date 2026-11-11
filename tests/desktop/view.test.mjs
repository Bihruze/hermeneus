// View decisions of the subtitle pane: which designed state shows, speaker turns,
// the unseen count behind "Jump to live", the live edge and the status bar text.

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { plugin } from './load-plugin.mjs'

const { applyEvent, atLiveEdge, chipParts, createSubtitleState, startsTurn, subtitleView, unseenCount } = plugin

const online = { state: 'online', error: '' }
const offline = { state: 'offline', error: 'connect ECONNREFUSED' }
const connecting = { state: 'connecting', error: '' }

function withStatus(state, session) {
  return applyEvent(state, { type: 'status', seq: (state.seq ?? 0) + 1, state: session, message: '' })
}

function withLine(state, id, speaker = 's1:speaker_0', lineState = 'translated') {
  return applyEvent(state, {
    type: 'line',
    seq: (state.seq ?? 0) + 1,
    line: { line_id: id, state: lineState, original: 'hola', translation: 'merhaba', speaker_id: speaker }
  })
}

test('with no lines the body is one designed state per session and link state', () => {
  const empty = createSubtitleState()

  assert.equal(subtitleView(empty, connecting).body, 'connecting')
  assert.equal(subtitleView(empty, offline).body, 'offline')
  assert.equal(subtitleView(withStatus(empty, 'idle'), online).body, 'idle')
  assert.equal(subtitleView(withStatus(empty, 'starting'), online).body, 'starting')
  assert.equal(subtitleView(withStatus(empty, 'live'), online).body, 'listening')
  assert.equal(subtitleView(withStatus(empty, 'stopping'), online).body, 'listening')
  assert.equal(subtitleView(withStatus(empty, 'reconnecting'), online).body, 'reconnecting')
  assert.equal(subtitleView(withStatus(empty, 'failed'), online).body, 'failed')
  // An unreachable backend wins over a stale status.
  assert.equal(subtitleView(withStatus(empty, 'live'), offline).body, 'offline')

  for (const session of ['idle', 'starting', 'live', 'reconnecting', 'failed']) {
    assert.equal(subtitleView(withStatus(empty, session), online).notice, null)
  }
})

test('with lines the body is the list and the notice carries the session state', () => {
  const live = withLine(withStatus(createSubtitleState(), 'live'), 'l1')

  assert.deepEqual(subtitleView(live, online), { body: 'lines', notice: null })
  assert.deepEqual(subtitleView(live, offline), { body: 'lines', notice: 'offline' })
  assert.deepEqual(subtitleView(withStatus(live, 'reconnecting'), online), { body: 'lines', notice: 'reconnecting' })
  assert.deepEqual(subtitleView(withStatus(live, 'failed'), online), { body: 'lines', notice: 'failed' })
  assert.deepEqual(subtitleView(withStatus(live, 'idle'), online), { body: 'lines', notice: 'stopped' })
  assert.deepEqual(subtitleView(withStatus(live, 'stopping'), online), { body: 'lines', notice: null })
})

test('a stopped session keeps its lines until the next start clears them', () => {
  const stopped = withStatus(withLine(withStatus(createSubtitleState(), 'live'), 'l1'), 'idle')

  assert.equal(stopped.lines.length, 1)
  assert.equal(subtitleView(stopped, online).notice, 'stopped')

  const restarted = withStatus(stopped, 'starting')

  assert.equal(restarted.lines.length, 0)
  assert.equal(subtitleView(restarted, online).body, 'starting')
})

test('the speaker name shows on the first line of each turn only', () => {
  let state = withStatus(createSubtitleState(), 'live')
  state = withLine(state, 'a', 's1:speaker_0')
  state = withLine(state, 'b', 's1:speaker_0')
  state = withLine(state, 'c', 's1:speaker_1')
  state = withLine(state, 'd', 's1:speaker_0')
  state = withLine(state, 'e', null)
  state = withLine(state, 'f', null)

  assert.deepEqual(
    state.lines.map((_, index) => startsTurn(state.lines, index)),
    [true, false, true, true, true, false]
  )
})

test('the unseen count is the number of lines below the paused line', () => {
  let state = withStatus(createSubtitleState(), 'live')

  for (const id of ['a', 'b', 'c', 'd']) {
    state = withLine(state, id)
  }

  assert.equal(unseenCount(state.lines, null), 0)
  assert.equal(unseenCount(state.lines, 'd'), 0)
  assert.equal(unseenCount(state.lines, 'b'), 2)
  // The paused line was evicted by the line limit: every line on screen is new to the reader.
  assert.equal(unseenCount(state.lines, 'gone'), 4)
  // A partial line that turns into its translation is an update, not a new line.
  const updated = withLine(state, 'd', 's1:speaker_0', 'translated')

  assert.equal(unseenCount(updated.lines, 'd'), 0)
})

test('the live edge allows the same 48 px slack as the core Logs pane', () => {
  assert.equal(atLiveEdge({ scrollHeight: 1000, scrollTop: 800, clientHeight: 200 }), true)
  assert.equal(atLiveEdge({ scrollHeight: 1000, scrollTop: 752, clientHeight: 200 }), true)
  assert.equal(atLiveEdge({ scrollHeight: 1000, scrollTop: 751, clientHeight: 200 }), false)
  assert.equal(atLiveEdge({ scrollHeight: 100, scrollTop: 0, clientHeight: 200 }), true)
})

test('the status bar names the plugin and adds the state or the newest translation', () => {
  const empty = createSubtitleState()

  assert.deepEqual(chipParts(empty), { session: null, showState: false, last: '' })
  assert.equal(chipParts(withStatus(empty, 'idle')).showState, false)
  assert.equal(chipParts(withStatus(empty, 'starting')).showState, true)
  assert.equal(chipParts(withStatus(empty, 'failed')).showState, true)

  const live = withLine(withStatus(empty, 'live'), 'l1')

  assert.deepEqual(chipParts(live), { session: 'live', showState: false, last: 'merhaba' })
  // Live before the first translation: the state word stands in for the text.
  assert.equal(chipParts(withStatus(empty, 'live')).showState, true)
  // A stopped session no longer shows its last line in the status bar.
  assert.equal(chipParts(withStatus(live, 'idle')).last, '')
})
