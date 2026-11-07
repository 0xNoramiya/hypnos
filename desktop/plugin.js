/**
 * Hypnos for Hermes Desktop: sleep pressure, the hypnogram, morning reports and brain badges.
 *
 * SDK-only and build-free: imports nothing but @hermes/plugin-sdk, react and react/jsx-runtime, uses
 * jsx() calls instead of JSX, colors only with the host's --ui-* theme tokens, and never touches the app's
 * DOM. Data comes from the Python half at /api/plugins/hypnos via ctx.rest; live pushes arrive as
 * plugin.hypnos.* events, with polling as the fallback while a night is running.
 */

import * as sdk from '@hermes/plugin-sdk'
import { useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hypnos'
const KEY = [ID]
const { host, queryClient, useQuery, useValue } = sdk

const STAGES = ['WAKE', 'REM', 'N1', 'N2', 'N3']
const STAGE_LABEL = { WAKE: 'Wake', REM: 'REM', N1: 'N1', N2: 'N2', N3: 'N3' }
const STAGE_COLOR = {
  WAKE: 'var(--ui-yellow)',
  REM: 'var(--ui-purple)',
  N1: 'var(--ui-cyan)',
  N2: 'var(--ui-blue)',
  N3: 'var(--ui-accent)'
}
const STAGE_HINT = {
  WAKE: 'Awake: lights out, or the waking exam',
  REM: 'REM: dreams. DPO on corrected pairs and practised nightmares',
  N1: 'N1: light sleep. Re-checking that captured data is scrubbed',
  N2: 'N2: sorting the day. Families, held-out split, habits, dream writing',
  N3: 'N3: slow-wave replay. LoRA SFT on successful turns'
}

/** jsx() with variadic children and a `key` prop, so the views below stay readable. */
function h(type, props, ...children) {
  const p = props ? { ...props } : {}
  const key = p.key
  delete p.key
  const kids = children.flat().filter(c => c !== null && c !== undefined && c !== false && c !== '')
  if (kids.length === 1) p.children = kids[0]
  else if (kids.length > 1) p.children = kids
  return kids.length > 1 ? jsxs(type, p, key) : jsx(type, p, key)
}

const pct = x => (x === null || x === undefined ? '-' : `${Math.round(x * 100)}%`)
const compact = n => (typeof sdk.compactNumber === 'function' ? sdk.compactNumber(n || 0) : String(n || 0))
const minutes = s => `${(s / 60).toFixed(1)} min`

function useHypnos(ctx, path, opts = {}) {
  const conn = useValue(host.state.connectionId)
  const profile = useValue(host.state.profile)
  return useQuery({
    queryKey: [...KEY, conn, profile, path],
    queryFn: () => ctx.rest(path),
    retry: false,
    staleTime: 1500,
    ...opts
  })
}

function Tip({ label, children }) {
  return typeof sdk.Tip === 'function' ? h(sdk.Tip, { label }, children) : children
}

/* ---------------------------------------------------------------- moon (status bar) */

function MoonGlyph({ fill, sleeping, size = 14 }) {
  const level = Math.max(0, Math.min(1, fill))
  const clipId = useMemo(() => `hypnos-moon-${Math.random().toString(36).slice(2)}`, [])
  const top = 2 + 20 * (1 - level)
  return h('svg', { width: size, height: size, viewBox: '0 0 24 24', 'aria-hidden': 'true', style: { display: 'block' } },
    h('defs', null, h('clipPath', { id: clipId }, h('circle', { cx: 12, cy: 12, r: 10 }))),
    h('circle', { cx: 12, cy: 12, r: 10, style: { fill: 'none', stroke: 'currentColor', strokeWidth: 1.6, opacity: 0.55 } }),
    h('rect', { x: 0, y: top, width: 24, height: 24, clipPath: `url(#${clipId})`, style: { fill: 'currentColor', opacity: 0.9 } }),
    sleeping ? h('circle', { cx: 16, cy: 9, r: 8, style: { fill: 'var(--ui-bg-chrome, var(--ui-bg-secondary))' } }) : null
  )
}

function Moon({ ctx, onOpen }) {
  const { data } = useHypnos(ctx, '/status', { refetchInterval: q => (q.state.data && q.state.data.sleeping ? 3000 : 30000) })
  usePetAndNotices(ctx, data)
  if (!data) return null
  const sleeping = !!data.sleeping
  const brain = data.brain ? `v${data.brain.version}` : 'no brain'
  const color = sleeping ? 'var(--ui-purple)' : data.pressure >= 60 ? 'var(--ui-orange)' : 'var(--ui-text-secondary)'
  const lines = sleeping
    ? [`Asleep: ${STAGE_LABEL[data.night && data.night.stage] || '...'}`
       + (data.night && data.night.cycle ? `, cycle ${data.night.cycle}` : '')
       + (data.night && data.night.progress ? `, step ${data.night.progress.step}/${data.night.progress.steps}` : '')]
    : [`Sleep pressure ${Math.round(data.pressure)}%`]
  lines.push(data.brain
    ? `Brain ${brain}, ${data.habits.consolidated || 0} of ${data.habits.total} habits in the weights`
    : 'No brain yet: every turn goes to your normal model')
  if (data.last_turn) {
    lines.push(data.last_turn.answered_by === 'brain'
      ? `Last reply: brain v${data.last_turn.brain_version}, ${compact(data.last_turn.tokens_saved)} tokens saved`
      : 'Last reply: your normal model')
  }
  const label = h('span', null, ...lines.flatMap((l, i) => (i ? [h('br', { key: `b${i}` }), l] : [l])))
  return h(Tip, { label },
    h('button', {
      type: 'button',
      onClick: onOpen,
      'aria-label': `Hypnos: ${lines.join('. ')}`,
      style: {
        display: 'inline-flex', alignItems: 'center', gap: 5, height: '100%', padding: '0 6px', border: 0,
        background: 'transparent', color, cursor: 'pointer', font: 'inherit', fontSize: '0.6875rem', fontVariantNumeric: 'tabular-nums'
      }
    },
    h(MoonGlyph, { fill: sleeping ? 1 : (data.pressure || 0) / 100, sleeping }),
    h('span', null, sleeping ? 'zzz' : `${Math.round(data.pressure)}%`),
    h('span', { style: { color: 'var(--ui-text-tertiary)' } }, brain))
  )
}

/** Pet speech lines and the "report ready" notice, driven by status changes. */
function usePetAndNotices(ctx, data) {
  const last = useRef({})
  useEffect(() => {
    if (!data) return
    const prev = last.current
    const pet = ctx.pet
    const petOn = pet && (!pet.visible || typeof pet.visible.get !== 'function' || pet.visible.get())
    if (petOn && typeof pet.say === 'function') {
      if (data.sleeping && data.night && prev.stage !== data.night.stage) {
        pet.say(`zzz... ${STAGE_LABEL[data.night.stage] || ''}${data.night.cycle ? `, cycle ${data.night.cycle}` : ''}`,
          { id: 'hypnos-state', tone: 'wait', ttlMs: 25000 })
      } else if (!data.sleeping && data.pressure >= 60 && (prev.pressureBand || 0) < 60) {
        pet.say(`*yawn* sleep pressure ${Math.round(data.pressure)}%. Time for /sleep?`,
          { id: 'hypnos-state', tone: 'info', ttlMs: 15000 })
      }
    }
    const reportId = data.latest_report
    if (prev.sleeping && !data.sleeping && reportId) {
      const open = () => host.navigate('/hypnos')
      if (typeof host.notify === 'function') {
        host.notify({ kind: 'success', title: 'Hypnos woke up', message: 'The morning report is ready.',
          action: { label: 'Open', onClick: open } })
      }
      if (ctx.os && typeof ctx.os.notify === 'function') {
        ctx.os.notify({ title: 'Hypnos woke up', body: 'The morning report is ready.', activate: '/hypnos' })
      }
      if (petOn && typeof pet.say === 'function') {
        pet.say('Good morning. I learned something overnight: report is ready.', { id: 'hypnos-state', tone: 'info', ttlMs: 20000 })
      }
    }
    last.current = { sleeping: data.sleeping, stage: data.night && data.night.stage, pressureBand: data.pressure }
  }, [data, ctx])
}

/* ---------------------------------------------------------------- hypnogram */

function useWidth(ref, fallback = 640) {
  const [width, setWidth] = useState(fallback)
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver !== 'function') return undefined
    const ro = new ResizeObserver(entries => {
      const w = entries[0] && entries[0].contentRect.width
      if (w) setWidth(Math.max(280, Math.round(w)))
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref])
  return width
}

function HypnogramChart({ payload, onSelect, selected }) {
  const wrap = useRef(null)
  const width = useWidth(wrap)
  const segs = (payload && payload.segments) || []
  if (!segs.length) {
    return h('div', { ref: wrap, style: { padding: 16, color: 'var(--ui-text-tertiary)', fontSize: 13 } },
      'No nights yet. Run /sleep now, or press Sleep now above.')
  }
  const padL = 44, padR = 12, padT = 10, rowH = 26
  const base = padT + rowH * STAGES.length
  const height = base + 34
  const t0 = segs[0].start
  const t1 = Math.max(...segs.map(s => s.end || s.start), t0 + 1)
  const x = t => padL + ((t - t0) / (t1 - t0)) * (width - padL - padR)
  const y = st => padT + STAGES.indexOf(st) * rowH + rowH / 2
  let path = ''
  segs.forEach((s, i) => {
    path += i === 0 ? `M${x(s.start)},${y(s.stage)}` : ` V${y(s.stage)}`
    path += ` H${x(s.end || s.start)}`
  })
  const cycleStarts = []
  let lastCycle = null
  segs.forEach(s => {
    if (s.cycle && s.cycle !== lastCycle && s.stage === 'N3') {
      cycleStarts.push({ t: s.start, c: s.cycle })
      lastCycle = s.cycle
    }
  })
  const markers = []
  segs.forEach(s => (s.events || []).forEach(ev => {
    if (ev.event === 'nightmare') markers.push({ t: ev.ts, stage: s.stage, kind: 'nightmare', ev })
    if (ev.event === 'nightmare_resolved') markers.push({ t: ev.ts, stage: s.stage, kind: 'resolved', ev })
  }))
  const children = []
  STAGES.forEach(st => {
    children.push(h('text', { key: `l${st}`, x: 6, y: y(st) + 4, style: { fill: 'var(--ui-text-tertiary)', fontSize: 11 } },
      STAGE_LABEL[st]))
    children.push(h('line', { key: `g${st}`, x1: padL, x2: width - padR, y1: y(st), y2: y(st),
      style: { stroke: 'var(--ui-stroke-quaternary, var(--ui-stroke-tertiary))', strokeWidth: 1 } }))
  })
  cycleStarts.forEach(({ t, c }) => {
    children.push(h('line', { key: `c${c}`, x1: x(t), x2: x(t), y1: padT, y2: padT + rowH * STAGES.length,
      style: { stroke: 'var(--ui-stroke-secondary)', strokeDasharray: '3 3' } }))
    children.push(h('text', { key: `ct${c}`, x: x(t) + 3, y: base + 12,
      style: { fill: 'var(--ui-text-tertiary)', fontSize: 10 } }, `cycle ${c}`))
  })
  children.push(h('path', { key: 'step', d: path, style: { fill: 'none', stroke: 'var(--ui-text-quaternary)', strokeWidth: 1.2 } }))
  segs.forEach((s, i) => {
    const x0 = x(s.start), x1 = Math.max(x0 + 2, x(s.end || s.start))
    const isSel = selected === i
    children.push(h('rect', {
      key: `s${i}`, x: x0, y: y(s.stage) - 6, width: x1 - x0, height: 12, rx: 3,
      role: 'button', tabIndex: 0, 'aria-label': `${STAGE_LABEL[s.stage]}, ${minutes((s.end || s.start) - s.start)}`,
      onClick: () => onSelect(i), onKeyDown: e => { if (e.key === 'Enter') onSelect(i) },
      style: { fill: STAGE_COLOR[s.stage], opacity: isSel ? 1 : 0.85, cursor: 'pointer', font: 'inherit',
        stroke: isSel ? 'var(--ui-text-primary)' : 'none', strokeWidth: isSel ? 1.5 : 0 }
    }))
  })
  markers.forEach((m, i) => {
    const cx = x(m.t), cy = y(m.stage) - 12
    children.push(m.kind === 'nightmare'
      ? h('path', { key: `m${i}`, d: `M${cx},${cy - 4} L${cx + 4},${cy} L${cx},${cy + 4} L${cx - 4},${cy} Z`,
        style: { fill: 'var(--ui-red)' } }, h('title', null, `Nightmare: ${m.ev.request || ''}`))
      : h('circle', { key: `m${i}`, cx, cy, r: 3.5, style: { fill: 'var(--ui-green)' } }, h('title', null, 'Nightmare resolved')))
  })
  children.push(h('text', { key: 't0', x: padL, y: height - 4, style: { fill: 'var(--ui-text-tertiary)', fontSize: 10 } }, '0 min'))
  children.push(h('text', { key: 't1', x: width - padR, y: height - 4, textAnchor: 'end',
    style: { fill: 'var(--ui-text-tertiary)', fontSize: 10 } }, minutes(t1 - t0)))
  return h('div', { ref: wrap, style: { width: '100%' } },
    h('svg', { width, height, viewBox: `0 0 ${width} ${height}`, role: 'img',
      'aria-label': `Hypnogram of night ${payload.night.id}` }, children))
}

function SegmentDetails({ seg }) {
  if (!seg) {
    return h('div', { style: { color: 'var(--ui-text-tertiary)', fontSize: 12 } }, 'Click a segment for details.')
  }
  const progress = (seg.events || []).filter(e => e.event === 'progress')
  const losses = progress.map(e => e.loss).filter(v => typeof v === 'number')
  const notable = (seg.events || []).filter(e => !['progress', 'phase_start', 'phase_end'].includes(e.event)).slice(-6)
  return h('div', { style: { fontSize: 12, color: 'var(--ui-text-secondary)', display: 'grid', gap: 4 } },
    h('div', { style: { color: 'var(--ui-text-primary)', fontWeight: 600 } },
      `${STAGE_LABEL[seg.stage]}${seg.cycle ? `, cycle ${seg.cycle}` : ''}, ${minutes((seg.end || seg.start) - seg.start)}`),
    h('div', null, STAGE_HINT[seg.stage]),
    losses.length ? h('div', null, `Loss ${losses[0]} to ${losses[losses.length - 1]} over ${progress[progress.length - 1].steps || losses.length} steps`) : null,
    ...notable.map((e, i) => h('div', { key: `e${i}`, style: { color: 'var(--ui-text-tertiary)' } },
      e.event.replace(/_/g, ' ') + (e.request ? `: "${e.request}"` : '') + (e.brain_did ? ` (brain did: ${e.brain_did})` : '')))
  )
}

function Hypnogram({ ctx, nightId }) {
  const path = nightId ? `/night?id=${nightId}` : '/night'
  const { data } = useHypnos(ctx, path, { refetchInterval: q => (q.state.data && q.state.data.running ? 2000 : false) })
  const [selected, setSelected] = useState(null)
  const segs = (data && data.segments) || []
  const night = data && data.night
  return h('div', { style: { display: 'grid', gap: 10, padding: 12 } },
    h('div', { style: { display: 'flex', alignItems: 'baseline', gap: 8, fontSize: 12, color: 'var(--ui-text-secondary)' } },
      h('span', { style: { color: 'var(--ui-text-primary)', fontWeight: 600, fontSize: 13 } },
        night ? `Night #${night.id}` : 'Hypnogram'),
      night ? h('span', null, night.status === 'running' ? `asleep, now ${STAGE_LABEL[data.current_stage] || ''}` : night.status) : null,
      data && data.progress && data.running ? h('span', null, `step ${data.progress.step}/${data.progress.steps}, loss ${data.progress.loss}`) : null),
    h(HypnogramChart, { payload: data, selected, onSelect: setSelected }),
    h(SegmentDetails, { seg: selected !== null ? segs[selected] : null }))
}

/* ---------------------------------------------------------------- report page */

const card = { background: 'var(--ui-bg-card, var(--ui-bg-secondary))', border: '1px solid var(--ui-stroke-tertiary)',
  borderRadius: 10, padding: 14 }

function Stat({ label, value, hint }) {
  return h('div', { style: { display: 'grid', gap: 2, minWidth: 120 } },
    h('div', { style: { fontSize: 11, color: 'var(--ui-text-tertiary)', textTransform: 'uppercase', letterSpacing: 0.4 } }, label),
    h('div', { style: { fontSize: 18, color: 'var(--ui-text-primary)', fontVariantNumeric: 'tabular-nums' } }, value),
    hint ? h('div', { style: { fontSize: 11, color: 'var(--ui-text-tertiary)' } }, hint) : null)
}

function Delta({ before, after }) {
  if (after === undefined || after === null) return h('span', null, '-')
  const d = before === undefined || before === null ? null : after - before
  const color = d === null || Math.abs(d) < 0.005 ? 'var(--ui-text-tertiary)' : d > 0 ? 'var(--ui-green)' : 'var(--ui-red)'
  return h('span', { style: { fontVariantNumeric: 'tabular-nums' } }, `${pct(before)} to ${pct(after)} `,
    d === null ? null : h('span', { style: { color } }, `(${d >= 0 ? '+' : ''}${Math.round(d * 100)} pts)`))
}

function TokensChart({ days }) {
  const wrap = useRef(null)
  const width = useWidth(wrap, 560)
  if (!days || !days.length) return null
  const height = 170, padL = 48, padR = 40, padT = 12, padB = 30
  const maxT = Math.max(1, ...days.map(d => d.avg_prompt_tokens))
  const bw = Math.max(8, Math.min(48, (width - padL - padR) / days.length - 10))
  const xc = i => padL + ((i + 0.5) * (width - padL - padR)) / days.length
  const yT = v => padT + (1 - v / maxT) * (height - padT - padB)
  const yS = v => padT + (1 - v) * (height - padT - padB)
  const line = days.map((d, i) => `${i ? 'L' : 'M'}${xc(i)},${yS(d.success)}`).join(' ')
  return h('div', { ref: wrap, style: { width: '100%' } },
    h('svg', { width, height, role: 'img', 'aria-label': 'Prompt tokens per turn and task success by day' },
      days.map((d, i) => h('g', { key: `d${i}` },
        h('rect', { x: xc(i) - bw / 2, y: yT(d.avg_prompt_tokens), width: bw, height: Math.max(1, height - padB - yT(d.avg_prompt_tokens)),
          rx: 3, style: { fill: 'var(--ui-blue)', opacity: 0.35 + 0.55 * (1 - d.brain_share) } },
          h('title', null, `${d.day}: ${compact(d.avg_prompt_tokens)} prompt tokens per turn, ${pct(d.brain_share)} answered by the brain`)),
        h('rect', { x: xc(i) - bw / 2, y: height - padB - (height - padT - padB) * d.brain_share * 0.06, width: bw,
          height: (height - padT - padB) * d.brain_share * 0.06, style: { fill: 'var(--ui-accent)' } }),
        h('text', { x: xc(i), y: height - 12, textAnchor: 'middle', style: { fill: 'var(--ui-text-tertiary)', fontSize: 10 } },
          `day ${i + 1}`))),
      h('path', { d: line, style: { fill: 'none', stroke: 'var(--ui-green)', strokeWidth: 2 } }),
      days.map((d, i) => h('circle', { key: `p${i}`, cx: xc(i), cy: yS(d.success), r: 3, style: { fill: 'var(--ui-green)' } },
        h('title', null, `${d.day}: ${pct(d.success)} of turns needed no correction`))),
      h('text', { x: 4, y: padT + 8, style: { fill: 'var(--ui-text-tertiary)', fontSize: 10 } }, compact(maxT)),
      h('text', { x: width - 4, y: padT + 8, textAnchor: 'end', style: { fill: 'var(--ui-green)', fontSize: 10 } }, 'success'),
      h('text', { x: 4, y: height - padB, style: { fill: 'var(--ui-text-tertiary)', fontSize: 10 } }, '0')))
}

function Report({ ctx }) {
  const [nightId, setNightId] = useState(null)
  const status = useHypnos(ctx, '/status', { refetchInterval: q => (q.state.data && q.state.data.sleeping ? 3000 : 20000) })
  const nights = useHypnos(ctx, '/nights')
  const rep = useHypnos(ctx, nightId ? `/report?id=${nightId}` : '/report')
  const metrics = useHypnos(ctx, '/metrics')
  const [busy, setBusy] = useState(false)
  const st = status.data
  const r = rep.data && rep.data.report
  const sleepNow = async () => {
    setBusy(true)
    try {
      await ctx.rest('/sleep', { method: 'POST' })
      queryClient.invalidateQueries({ queryKey: KEY })
    } catch (err) {
      if (typeof host.notifyError === 'function') host.notifyError(err, 'Hypnos could not fall asleep')
    } finally {
      setBusy(false)
    }
  }
  const btn = { padding: '5px 12px', borderRadius: 7, border: '1px solid var(--ui-stroke-secondary)',
    background: 'var(--ui-bg-elevated, var(--ui-bg-secondary))', color: 'var(--ui-text-primary)', cursor: 'pointer', font: 'inherit', fontSize: 12 }
  const exam = r && r.exam ? r.exam : { candidate: {}, baseline: {} }
  return h('div', { style: { height: '100%', overflow: 'auto' } },
    h('div', { style: { maxWidth: 920, margin: '0 auto', padding: '20px 20px 40px', display: 'grid', gap: 14 } },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: 12 } },
        h('div', { style: { color: 'var(--ui-purple)' } }, h(MoonGlyph, { fill: st ? (st.pressure || 0) / 100 : 0, sleeping: st && st.sleeping, size: 28 })),
        h('div', { style: { flex: 1 } },
          h('div', { style: { fontSize: 18, fontWeight: 600, color: 'var(--ui-text-primary)' } }, 'Hypnos'),
          h('div', { style: { fontSize: 12, color: 'var(--ui-text-tertiary)' } }, 'Hermes learns in its sleep. Habits go into the weights; facts stay in memory.')),
        h('button', { type: 'button', style: btn, disabled: busy || (st && st.sleeping), onClick: sleepNow },
          st && st.sleeping ? 'Sleeping...' : 'Sleep now'),
        st && st.sleeping ? h('button', { type: 'button', style: btn, onClick: () => ctx.rest('/sleep/cancel', { method: 'POST' }) }, 'Wake up') : null),
      st ? h('div', { style: { ...card, display: 'flex', flexWrap: 'wrap', gap: 24 } },
        h(Stat, { label: 'Sleep pressure', value: `${Math.round(st.pressure)}%`, hint: 'unconsolidated corrections, denials, failures' }),
        h(Stat, { label: 'Brain', value: st.brain ? `v${st.brain.version}` : 'none', hint: st.brain ? st.brain.base_model : 'trained in the first night' }),
        h(Stat, { label: 'Habits in the weights', value: `${(st.habits && st.habits.consolidated) || 0} / ${(st.habits && st.habits.total) || 0}` }),
        st.brain ? h(Stat, { label: 'Families', value: Object.keys(st.brain.families || {}).length,
          hint: Object.entries(st.brain.families || {}).map(([f, s]) => `${f} ${s.state}`).join(', ') }) : null) : null,
      h('div', { style: card },
        h('div', { style: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 } },
          h('div', { style: { fontWeight: 600, color: 'var(--ui-text-primary)', fontSize: 13, flex: 1 } }, 'Hypnogram'),
          nights.data && nights.data.length > 1 ? h('select', {
            value: nightId || '', onChange: e => setNightId(e.target.value ? Number(e.target.value) : null),
            style: { fontSize: 12, background: 'var(--ui-bg-input, var(--ui-bg-secondary))', color: 'var(--ui-text-primary)',
              border: '1px solid var(--ui-stroke-tertiary)', borderRadius: 6, padding: '2px 6px' }
          }, h('option', { value: '' }, 'Latest night'),
          nights.data.map(n => h('option', { key: `n${n.id}`, value: n.id }, `Night #${n.id} (${n.status})`))) : null),
        h(Hypnogram, { ctx, nightId })),
      r ? h('div', { style: { ...card, display: 'grid', gap: 12 } },
        h('div', { style: { display: 'flex', gap: 8, alignItems: 'baseline' } },
          h('div', { style: { fontWeight: 600, fontSize: 14, color: 'var(--ui-text-primary)' } }, `Sleep study, night #${r.night_id}`),
          h('div', { style: { fontSize: 12, color: 'var(--ui-text-tertiary)' } }, r.date),
          r.synthetic ? h('div', { style: { fontSize: 11, color: 'var(--ui-orange)', border: '1px solid var(--ui-orange)', borderRadius: 999, padding: '0 8px' } }, 'synthetic demo data') : null,
          h('div', { style: { flex: 1 } }),
          h('div', { style: { fontSize: 12, fontWeight: 600, color: r.promoted ? 'var(--ui-green)' : 'var(--ui-orange)' } },
            r.promoted ? `Promoted brain v${r.version}` : 'Bad night, kept the previous brain')),
        h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 24 } },
          h(Stat, { label: 'Total sleep', value: `${r.total_sleep_min.toFixed(1)} min`, hint: `${r.cycles} cycles` }),
          h(Stat, { label: 'N3 / REM', value: `${pct(r.n3_share)} / ${pct(r.rem_share)}`, hint: 'share of training time' }),
          h(Stat, { label: 'Sleep efficiency', value: pct(r.sleep_efficiency), hint: 'training time / total' }),
          h(Stat, { label: 'Nightmares', value: r.nightmares ? `${r.nightmares_resolved} / ${r.nightmares}` : '0', hint: 'resolved / found' }),
          h(Stat, { label: 'Tokens saved', value: compact(r.tokens_saved_per_turn), hint: `per routed turn (brain ${compact(r.brain_prompt_tokens)})` })),
        h('div', { style: { fontSize: 12, color: 'var(--ui-text-secondary)', display: 'grid', gridTemplateColumns: 'max-content 1fr', gap: '4px 16px' } },
          h('div', null, 'Held-out next action'), h(Delta, { before: exam.baseline.heldout_acc, after: exam.candidate.heldout_acc }),
          h('div', null, 'Habit probes'), h(Delta, { before: exam.baseline.probe_pass, after: exam.candidate.probe_pass }),
          exam.candidate.dream_n ? h('div', null, 'Dream verifiers') : null,
          exam.candidate.dream_n ? h(Delta, { before: exam.baseline.dream_pass, after: exam.candidate.dream_pass }) : null,
          h('div', null, 'Valid outputs'), h(Delta, { before: exam.baseline.validity, after: exam.candidate.validity }),
          h('div', null, 'REM'), h('div', null, r.rem_mode === 'sandbox' ? 'Docker sandbox, no network' : `lucid, no execution${r.rem_mode_reason ? ` (${r.rem_mode_reason})` : ''}`)),
        (r.habits_consolidated || []).length ? h('div', { style: { display: 'grid', gap: 4 } },
          h('div', { style: { fontSize: 12, color: 'var(--ui-text-tertiary)' } }, r.promoted || !r.kept_version
            ? 'Consolidated into the weights, pruned from the brain prompt'
            : `Still in the weights of v${r.kept_version}, pruned from its prompt`),
          r.habits_consolidated.map(x => h('div', { key: `hc${x.id}`, style: { fontSize: 13, color: 'var(--ui-text-primary)' } },
            h('span', { style: { color: 'var(--ui-green)', marginRight: 6 } }, '+'), x.statement))) : null,
        (r.habits_awake || []).length ? h('div', { style: { display: 'grid', gap: 4 } },
          h('div', { style: { fontSize: 12, color: 'var(--ui-text-tertiary)' } }, 'Still learning, kept as text'),
          r.habits_awake.map(x => h('div', { key: `ha${x.id}`, style: { fontSize: 13, color: 'var(--ui-text-secondary)' } },
            h('span', { style: { marginRight: 6 } }, '.'), x.statement))) : null,
        (r.reasons || []).length ? h('div', { style: { fontSize: 12, color: 'var(--ui-text-tertiary)' } },
          r.reasons.map((x, i) => h('div', { key: `r${i}` }, x))) : null) : null,
      metrics.data && metrics.data.days && metrics.data.days.length >= 2 ? h('div', { style: card },
        h('div', { style: { fontWeight: 600, color: 'var(--ui-text-primary)', fontSize: 13 } }, 'Prompt tokens per turn, by day'),
        h('div', { style: { fontSize: 12, color: 'var(--ui-text-tertiary)', marginBottom: 8 } },
          'Bars: average prompt tokens per turn (lighter when more turns went to the brain). Line: turns that needed no correction.'),
        h(TokensChart, { days: metrics.data.days })) : null))
}

/* ---------------------------------------------------------------- badges */

function ReplyBadge({ ctx, attrs, streaming }) {
  const turnId = attrs && (attrs.t || attrs.turn)
  const { data } = useHypnos(ctx, `/turns/${encodeURIComponent(turnId || '')}`, {
    enabled: !!turnId && !streaming,
    refetchInterval: q => (q.state.data && q.state.data.calls ? false : 1500)
  })
  if (!turnId) return null
  const brain = data && data.answered_by === 'brain'
  const text = !data ? 'hypnos' : brain
    ? `brain v${data.brain_version} · ${compact(data.tokens_saved)} tokens saved`
    : data.answered_by === 'mixed' ? `brain v${data.brain_version} + frontier` : data.shadow ? 'frontier · brain in shadow' : 'frontier'
  return h('span', {
    style: { display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, padding: '1px 8px', borderRadius: 999,
      border: `1px solid ${brain ? 'var(--ui-purple)' : 'var(--ui-stroke-tertiary)'}`,
      color: brain ? 'var(--ui-purple)' : 'var(--ui-text-tertiary)', fontVariantNumeric: 'tabular-nums' }
  }, h(MoonGlyph, { fill: brain ? 1 : 0, sleeping: false, size: 10 }), text)
}

function RowBadge({ ctx, sessionId }) {
  const { data } = useHypnos(ctx, '/sessions', { staleTime: 30000, refetchInterval: 60000 })
  const s = data && sessionId ? data[sessionId] : null
  if (!s) return null
  return h(Tip, { label: `Brain v${s.version} answered ${s.turns} turn${s.turns === 1 ? '' : 's'} here, ${compact(s.saved)} tokens saved` },
    h('span', { style: { fontSize: 10, color: 'var(--ui-purple)', padding: '0 4px' } }, `v${s.version}`))
}

/* ---------------------------------------------------------------- registration */

export default {
  id: ID,
  name: 'Hypnos',
  description: 'Hermes learns in its sleep: sleep pressure, hypnogram, morning reports and brain badges.',
  register(ctx) {
    let closeHypnogram = null
    const openHypnogram = () => {
      if (typeof host.openWorkspace !== 'function') return host.navigate('/hypnos')
      if (closeHypnogram) return undefined
      closeHypnogram = host.openWorkspace('hypnos-hypnogram', {
        title: 'Hypnogram',
        render: () => h(Hypnogram, { ctx }),
        dock: { pane: 'workspace', pos: 'bottom' },
        onClose: () => { closeHypnogram = null }
      })
      return undefined
    }
    ctx.onDispose(() => {
      if (closeHypnogram) closeHypnogram()
      queryClient.removeQueries({ queryKey: KEY })
    })
    for (const ev of ['night.stage', 'night.done', 'pressure.changed', 'turn.tagged', 'settings.changed']) {
      ctx.onEvent(`plugin.${ID}.${ev}`, () => queryClient.invalidateQueries({ queryKey: KEY }))
    }
    ctx.onEvent('gateway.ready', () => queryClient.invalidateQueries({ queryKey: KEY }))
    const sleepNow = () => ctx.rest('/sleep', { method: 'POST' }).then(() => queryClient.invalidateQueries({ queryKey: KEY }))
    const contributions = []
    if (sdk.STATUSBAR_AREAS) {
      contributions.push({ id: 'moon', area: sdk.STATUSBAR_AREAS.right, order: 125, render: () => h(Moon, { ctx, onOpen: openHypnogram }) })
    }
    if (sdk.ROUTES_AREA) contributions.push({ id: 'page', area: sdk.ROUTES_AREA, data: { path: '/hypnos' }, render: () => h(Report, { ctx }) })
    if (sdk.SIDEBAR_NAV_AREA) contributions.push({ id: 'nav', area: sdk.SIDEBAR_NAV_AREA, data: { path: '/hypnos', label: 'Hypnos', codicon: 'eye-closed' } })
    if (sdk.PALETTE_AREA) {
      contributions.push({ id: 'cmd-hypnogram', area: sdk.PALETTE_AREA, data: { id: 'hypnos.hypnogram', label: 'Hypnos: open the hypnogram', run: openHypnogram, keywords: ['sleep', 'brain'] } })
      contributions.push({ id: 'cmd-report', area: sdk.PALETTE_AREA, data: { id: 'hypnos.report', label: 'Hypnos: morning report', run: () => host.navigate('/hypnos'), keywords: ['sleep', 'brain'] } })
      contributions.push({ id: 'cmd-sleep', area: sdk.PALETTE_AREA, data: { id: 'hypnos.sleep', label: 'Hypnos: sleep now', run: sleepNow, keywords: ['train', 'night'] } })
    }
    if (sdk.SESSION_ROW_AREAS) {
      contributions.push({ id: 'row', area: sdk.SESSION_ROW_AREAS.trailing, data: { render: ({ sessionId }) => h(RowBadge, { ctx, sessionId }) } })
    }
    if (typeof sdk.TRANSCRIPT_DIRECTIVE_AREA === 'string') {
      contributions.push({ id: 'badge', area: sdk.TRANSCRIPT_DIRECTIVE_AREA, data: { name: 'hypnos', render: props => h(ReplyBadge, { ctx, ...props }) } })
    }
    ctx.registerMany(contributions)
    if (typeof ctx.registerSettingsPage === 'function') {
      ctx.registerSettingsPage({
        id: 'settings',
        title: 'Hypnos',
        render: () => h('div', { style: { display: 'grid', gap: 10, padding: 4, fontSize: 13, color: 'var(--ui-text-secondary)' } },
          h('div', null, 'Hypnos captures what you approve, deny and correct, and trains a small LoRA brain while Hermes sleeps. Settings for capture, routing, the brain and dreams are under Agent settings.'),
          h('div', null, h('button', { type: 'button', onClick: () => host.navigate('/hypnos'),
            style: { padding: '5px 12px', borderRadius: 7, border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', color: 'var(--ui-text-primary)', cursor: 'pointer', font: 'inherit' } },
          'Open the morning report')))
      })
    }
  }
}
