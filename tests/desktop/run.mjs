import fs from 'node:fs'
import React from 'react'
import { renderToString } from 'react-dom/server'
import * as sdk from './sdk-stub.mjs'
import plugin from './plugin.mjs'
const fx = JSON.parse(fs.readFileSync(process.argv[2] || 'fixtures.json', 'utf8'))
const contributions = []
let settingsPage = null
const events = []
const ctx = {
  rest: async p => fx[p] ?? fx[p.split('?')[0]],
  registerMany: cs => { contributions.push(...cs); return () => {} },
  register: c => { contributions.push(c); return () => {} },
  registerSettingsPage: p => { settingsPage = p; return () => {} },
  onDispose: () => {}, onEvent: (t) => { events.push(t); return () => {} },
  pet: { say: () => () => {}, visible: { get: () => true } }, os: { notify: () => {} }
}
plugin.register(ctx)
const KEY = ['hypnos', 'c1', 'default']
for (const [path, data] of Object.entries(fx)) sdk.queryClient.setQueryData([...KEY, path], data)
const out = {}
for (const c of contributions) {
  let el = null
  if (c.render) el = c.render()
  else if (c.data && c.data.render) el = c.data.render(c.area === 'transcript.directives' ? { attrs: { t: 't-demo' }, streaming: false } : { sessionId: 's3' })
  if (el) out[c.id] = renderToString(el)
}
if (settingsPage) out.settings = renderToString(settingsPage.render())
console.log('contributions:', contributions.map(c => `${c.id}@${c.area}`).join(', '))
console.log('events:', events.join(', '))
for (const [id, html] of Object.entries(out)) console.log(`--- ${id} (${html.length} chars):`, html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').slice(0, 260))
const bad = Object.entries(out).filter(([, html]) => /#[0-9a-fA-F]{3,6}\b|rgb\(/.test(html.replace(/url\(#[^)]+\)/g, '').replace(/id="[^"]+"/g, '')))
console.log('hardcoded colors:', bad.map(([id]) => id))
const page = out.page
console.log('svg count:', (page.match(/<svg/g) || []).length, '| rects:', (page.match(/<rect/g) || []).length, '| nightmare markers:', (page.match(/Nightmare/g) || []).length)
console.log(page.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').slice(240, 1500))
