// The source picker keeps a long label inside its button: the label ends in an
// ellipsis and the full text is the trigger's title. The same Picker serves the
// pane, the chip popover and the setup rows.

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { plugin } from './load-plugin.mjs'

const { Picker } = plugin

// The macOS default source label (hermeneus_engine/capture/process_tap.py).
const LONG = 'All system audio except Hermeneus'

function render(value) {
  const tree = Picker({
    label: 'Source',
    onChange: () => {},
    options: [
      { value: 'process_tap:system', label: LONG },
      { value: 'device:1', label: 'MacBook Pro Microphone' }
    ],
    placeholder: 'Choose a source',
    value
  })
  const trigger = tree.props.children[0]
  return { trigger, value: trigger.props.children }
}

test('a long source label truncates with an ellipsis inside the trigger', () => {
  const { trigger, value } = render('process_tap:system')

  assert.equal(value.type, 'span')
  assert.equal(value.props.style.overflow, 'hidden')
  assert.equal(value.props.style.textOverflow, 'ellipsis')
  assert.equal(value.props.style.whiteSpace, 'nowrap')
  assert.equal(value.props.style.minWidth, 0)
  // The SDK SelectValue sits inside the truncating span.
  assert.equal(value.props.children.props.placeholder, 'Choose a source')
  // The trigger keeps its width and may shrink in a narrow pane.
  assert.equal(trigger.props.style.minWidth, 0)
  assert.equal(trigger.props.style.maxWidth, '100%')
})

test('the trigger title is the full label of the chosen option', () => {
  assert.equal(render('process_tap:system').trigger.props.title, LONG)
  assert.equal(render('device:1').trigger.props.title, 'MacBook Pro Microphone')
})

test('with nothing chosen there is no title', () => {
  assert.equal(render('').trigger.props.title, undefined)
  assert.equal(render('missing').trigger.props.title, undefined)
})
