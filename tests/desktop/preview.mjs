// Renders desktop/plugin.js contributions into static HTML pages for preview screenshots.
// Usage: node preview.mjs fixtures.json OUT_DIR
// The pages use Hermes Desktop's own --ui-* token values (light and dark) from apps/desktop/src/styles.css.
// They are previews of the plugin's components, not screenshots of the Hermes app.
import fs from 'node:fs'
import path from 'node:path'
import { renderToString } from 'react-dom/server'
import * as sdk from './sdk-stub.mjs'
import plugin from './plugin.mjs'

const [fixturesPath = 'fixtures.json', outDir = '.'] = process.argv.slice(2)
const fx = JSON.parse(fs.readFileSync(fixturesPath, 'utf8'))
const contributions = []
const ctx = {
  rest: async p => fx[p] ?? fx[p.split('?')[0]],
  registerMany: cs => { contributions.push(...cs); return () => {} },
  register: c => { contributions.push(c); return () => {} },
  registerSettingsPage: () => () => {},
  onDispose: () => {}, onEvent: () => () => {},
  pet: { say: () => () => {}, visible: { get: () => false } }, os: { notify: () => {} }
}
plugin.register(ctx)
for (const [p, data] of Object.entries(fx)) {
  sdk.queryClient.setQueryData(['hypnos', 'c1', 'default', p], data)
  if (p.startsWith('/turns/')) {
    const encoded = '/turns/' + encodeURIComponent(p.slice('/turns/'.length))
    sdk.queryClient.setQueryData(['hypnos', 'c1', 'default', encoded], data)
  }
}
const byId = Object.fromEntries(contributions.map(c => [c.id, c]))
const turnId = Object.keys(fx).find(k => k.startsWith('/turns/'))?.slice('/turns/'.length)
const html = {
  page: renderToString(byId.page.render()),
  moon: renderToString(byId.moon.render()),
  badge: turnId ? renderToString(byId.badge.data.render({ attrs: { t: turnId }, streaming: false })) : ''
}

const TOKENS = `
:root { --base:#17171a; --ui-accent:#0053fd; --ui-red:#cf2d56; --ui-orange:#db704b; --ui-yellow:#c08532; --ui-green:#1f8a65;
  --ui-cyan:#4c7f8c; --ui-blue:#0053fd; --ui-purple:#9e94d5; --bg:#f8faff; --chrome:#f3f4f8; --card-seed:#ffffff; }
:root.dark { --base:#ececf1; --ui-accent:#6d8dff; --ui-red:#e75e78; --ui-green:#55a583; --ui-cyan:#6f9ba6; --ui-blue:#6d8dff;
  --ui-purple:#a99ff0; --ui-orange:#e08a68; --ui-yellow:#d3a052; --bg:#111113; --chrome:#0d0d0e; --card-seed:#161618; }
:root { --ui-base:var(--base);
  --ui-text-primary:color-mix(in srgb, var(--ui-base) 94%, transparent); --ui-text-secondary:color-mix(in srgb, var(--ui-base) 74%, transparent);
  --ui-text-tertiary:color-mix(in srgb, var(--ui-base) 54%, transparent); --ui-text-quaternary:color-mix(in srgb, var(--ui-base) 36%, transparent);
  --ui-stroke-secondary:color-mix(in srgb, var(--ui-base) 12%, transparent); --ui-stroke-tertiary:color-mix(in srgb, var(--ui-base) 9%, transparent);
  --ui-stroke-quaternary:color-mix(in srgb, var(--ui-base) 6%, transparent);
  --ui-bg-card:color-mix(in srgb, var(--ui-accent) 4%, color-mix(in srgb, var(--ui-base) 4%, var(--card-seed)));
  --ui-bg-elevated:var(--card-seed); --ui-bg-input:var(--card-seed); --ui-bg-chrome:var(--chrome); --ui-bg-secondary:var(--chrome); }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--ui-text-primary);
  font:13px/1.45 Inter, "Ubuntu Sans", Ubuntu, "Noto Sans", ui-sans-serif, system-ui, sans-serif; }
.window { width:1180px; border:1px solid var(--ui-stroke-secondary); border-radius:12px; overflow:hidden; margin:24px; background:var(--bg) }
.titlebar { height:38px; display:flex; align-items:center; gap:8px; padding:0 14px; background:var(--ui-bg-chrome);
  border-bottom:1px solid var(--ui-stroke-tertiary); color:var(--ui-text-tertiary); font-size:12px }
.body { display:grid; grid-template-columns:220px 1fr; min-height:820px }
.sidebar { background:var(--ui-bg-chrome); border-right:1px solid var(--ui-stroke-tertiary); padding:12px; color:var(--ui-text-secondary) }
.sidebar div { padding:6px 8px; border-radius:6px } .sidebar .active { background:color-mix(in srgb, var(--ui-accent) 10%, transparent); color:var(--ui-text-primary) }
.statusbar { height:26px; display:flex; justify-content:space-between; align-items:center; padding:0 10px; background:var(--ui-bg-chrome);
  border-top:1px solid var(--ui-stroke-tertiary); color:var(--ui-text-tertiary); font-size:11px }
.chat { padding:18px 22px; display:grid; gap:14px; align-content:start } .user { justify-self:end; background:color-mix(in srgb, var(--ui-accent) 8%, var(--card-seed));
  border-radius:12px; padding:8px 12px } .reply { max-width:640px } .tool { font:12px ui-monospace, monospace; color:var(--ui-text-secondary);
  border:1px solid var(--ui-stroke-tertiary); border-radius:8px; padding:6px 10px; margin:6px 0 }
.label { position:fixed; right:10px; top:6px; font-size:11px; color:var(--ui-text-tertiary) }
`

function frame(main, dark, title) {
  return `<!doctype html><html class="${dark ? 'dark' : ''}"><head><meta charset="utf-8"><title>${title}</title><style>${TOKENS}</style></head><body>
<div class="label">Preview: desktop/plugin.js rendered with Hermes Desktop theme tokens</div>
<div class="window"><div class="titlebar">Hermes</div><div class="body">
<div class="sidebar"><div>Chats</div><div>Skills</div><div>Capabilities</div><div class="active">Hypnos</div></div>
<div>${main}</div></div>
<div class="statusbar"><span>hypnos-demo</span><span style="display:inline-flex;align-items:center;height:100%">${html.moon}</span></div></div></body></html>`
}

const chat = `<div class="chat"><div class="user">Add dayjs to the CLI.</div>
<div class="reply"><div class="tool">$ pnpm add dayjs</div><p>Added dayjs with pnpm.</p><p>${html.badge}</p></div></div>`

fs.mkdirSync(outDir, { recursive: true })
fs.writeFileSync(path.join(outDir, 'report-light.html'), frame(html.page, false, 'Hypnos report'))
fs.writeFileSync(path.join(outDir, 'report-dark.html'), frame(html.page, true, 'Hypnos report'))
fs.writeFileSync(path.join(outDir, 'reply-dark.html'), frame(chat, true, 'Hypnos reply badge'))
console.log('wrote', fs.readdirSync(outDir).filter(f => f.endsWith('.html')).join(', '))
