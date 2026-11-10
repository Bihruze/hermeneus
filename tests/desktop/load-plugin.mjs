// Loads desktop/plugin.js under plain Node for tests.
//
// Hermes Desktop resolves @hermes/plugin-sdk, react and react/jsx-runtime to
// the running app's modules (apps/desktop/src/sdk/runtime.ts). Node has none of
// them, so a module.registerHooks resolver maps each of those specifiers to a
// generated stub module. The stub exports exactly the names plugin.js imports:
// a few with working test doubles (atom, memo, jsx, area constants), the rest as
// functions that throw if a test reaches UI code. No dependency is installed.

import { readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'

export const PLUGIN_URL = new URL('../../desktop/plugin.js', import.meta.url)

const SPECIFIERS = ['@hermes/plugin-sdk', 'react', 'react/jsx-runtime']
const STUB_SCHEME = 'hermeneus-test-stub:'

function importedNames(source, specifier) {
  const quoted = specifier.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&')
  const match = new RegExp(`import\\s*\\{([^}]*)\\}\\s*from\\s*'${quoted}'`).exec(source)

  if (!match) {
    return []
  }

  return match[1]
    .split(',')
    .map(name => name.trim())
    .filter(Boolean)
}

// Working doubles. Every other imported name becomes a function that throws.
const DOUBLES = {
  atom: `function (initial) {
    let value = initial
    const listeners = new Set()
    return {
      get: () => value,
      set: next => { value = next; for (const fn of listeners) fn(value) },
      subscribe: fn => { listeners.add(fn); fn(value); return () => listeners.delete(fn) }
    }
  }`,
  memo: 'component => component',
  jsx: '(type, props, key) => ({ type, props, key })',
  jsxs: '(type, props, key) => ({ type, props, key })',
  PANES_AREA: "'panes'",
  STATUSBAR_AREAS: "Object.freeze({ left: 'statusBar.left', right: 'statusBar.right' })"
}

function stubSource(names) {
  return names
    .map(name =>
      DOUBLES[name]
        ? `export const ${name} = ${DOUBLES[name]};`
        : `export const ${name} = () => { throw new Error('${name} is UI code and is not available under node --test'); };`
    )
    .join('\n')
}

const pluginSource = readFileSync(PLUGIN_URL, 'utf8')
const stubs = new Map(SPECIFIERS.map(spec => [spec, stubSource(importedNames(pluginSource, spec))]))

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (stubs.has(specifier)) {
      return { url: `${STUB_SCHEME}${specifier}`, format: 'module', shortCircuit: true }
    }

    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (url.startsWith(STUB_SCHEME)) {
      return { source: stubs.get(url.slice(STUB_SCHEME.length)), format: 'module', shortCircuit: true }
    }

    // plugin.js sits in a folder with no package.json; Desktop treats it as ESM, so do the same.
    if (url === PLUGIN_URL.href) {
      return { source: pluginSource, format: 'module', shortCircuit: true }
    }

    return nextLoad(url, context)
  }
})

export const plugin = await import(PLUGIN_URL.href)
