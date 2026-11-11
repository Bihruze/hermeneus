// Every speaker colour the pane draws meets WCAG AA (4.5:1) on the pane background, light and dark.
//
// Token values: the default Hermes Desktop theme (v0.23.1), resolved by its own ThemeProvider in
// Chrome with getComputedStyle (W29 screenshot harness, 2026-11-11). The pane sits on
// --ui-bg-editor (= --ui-surface-background). --ui-text-primary carries alpha 0.94.

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { plugin } from './load-plugin.mjs'

const { SPEAKER_INK_PERCENT, SPEAKER_TOKENS, speakerInk } = plugin

const f = x => x * 255
const THEMES = {
  light: {
    '--ui-blue': [0, 83, 253],
    '--ui-green': [31, 138, 101],
    '--ui-orange': [219, 112, 75],
    '--ui-purple': [158, 148, 213],
    '--ui-cyan': [76, 127, 140],
    '--ui-yellow': [192, 133, 50],
    text: [f(0.121569), f(0.137255), f(0.156863), 0.94],
    bg: [f(0.983059), f(0.984784), f(0.98651)]
  },
  dark: {
    '--ui-blue': [0, 83, 253],
    '--ui-green': [85, 165, 131],
    '--ui-orange': [219, 112, 75],
    '--ui-purple': [158, 148, 213],
    '--ui-cyan': [111, 155, 166],
    '--ui-yellow': [192, 133, 50],
    text: [f(0.901961), f(0.929412), f(0.952941), 0.94],
    bg: [f(0.0549804), f(0.059451), f(0.0717647)]
  }
}

function luminance(rgb) {
  const [r, g, b] = rgb.map(c => {
    const v = c / 255

    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
  })

  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)

  return (hi + 0.05) / (lo + 0.05)
}

// CSS color-mix(in srgb, A p%, B): premultiplied alpha, then painted over the background.
function mixOver(color, text, percent, bg) {
  const p = percent / 100
  const alpha = p + (1 - p) * text[3]
  const rgb = [0, 1, 2].map(i => (p * color[i] + (1 - p) * text[i] * text[3]) / alpha)

  return rgb.map((c, i) => c * alpha + bg[i] * (1 - alpha))
}

function inkPercent(css, token) {
  const match = new RegExp(`^color-mix\\(in srgb, var\\(${token}\\) (\\d+)%, var\\(--ui-text-primary\\)\\)$`).exec(css)

  assert.ok(match, `unexpected speaker colour ${css}`)

  return Number(match[1])
}

test('every pane speaker colour reaches 4.5:1 on the pane background in light and dark', () => {
  for (const [name, theme] of Object.entries(THEMES)) {
    for (const token of SPEAKER_TOKENS) {
      const percent = inkPercent(speakerInk(token), token)
      const ratio = contrast(mixOver(theme[token], theme.text, percent, theme.bg), theme.bg)

      assert.ok(ratio >= 4.5, `${name} ${token}: ${ratio.toFixed(2)}:1`)
    }
  }
})

test('the raw tokens alone would fail, so the mix is what makes them readable', () => {
  const failing = []

  for (const [name, theme] of Object.entries(THEMES)) {
    for (const token of SPEAKER_TOKENS) {
      if (contrast(theme[token], theme.bg) < 4.5) {
        failing.push(`${name} ${token}`)
      }
    }
  }

  assert.ok(failing.includes('dark --ui-blue'))
  assert.ok(failing.includes('light --ui-purple'))
  assert.ok(SPEAKER_INK_PERCENT < 100)
})
