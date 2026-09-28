import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const root = new URL('../src/', import.meta.url)

test('annotation workbench exposes the shared light capsule shell', async () => {
  const [authGate, workbench, stylesheet] = await Promise.all([
    readFile(new URL('components/AuthGate.vue', root), 'utf8'),
    readFile(new URL('components/AnnotationWorkbench.vue', root), 'utf8'),
    readFile(new URL('style.css', root), 'utf8'),
  ])

  assert.match(authGate, /annotation-topbar/)
  assert.match(workbench, /annotation-shell/)
  assert.match(authGate, /\.annotation-topbar\s*\{[^}]*border-radius:999px/)
  assert.match(stylesheet, /--annotation-violet:\s*#7658d8/)
  assert.match(stylesheet, /@media\s*\(prefers-reduced-motion:reduce\)/)
})
