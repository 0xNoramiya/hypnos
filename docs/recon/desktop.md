# Hermes Desktop plugin recon (v0.23.0), for Hypnos

Read from disk only. Path prefixes used below:

- `DOC` = `ref/hermes-agent/website/docs/developer-guide/desktop-plugin-sdk.md`
- `PIDX` = `ref/hermes-agent/website/docs/developer-guide/plugins/index.md`
- `DASH` = `ref/hermes-agent/website/docs/user-guide/features/extending-the-dashboard.md`
- `S` = `ref/hermes-agent/apps/desktop/src`
- `H` = `ref/hermes-agent` (Python side)
- `OP` = `ref/hermes-official-plugins`

"Source" means verified in code. "Doc" means stated in the docs only. "Inferred" means my reading, not stated anywhere.

---

## 0. TL;DR

1. **Ship it as one folder**: `plugins/hypnos/{plugin.yaml,__init__.py,dashboard/{manifest.json,plugin_api.py},desktop/plugin.js}`. Electron copies `desktop/` to `desktop-plugins/hypnos/` with a `.hermes-package.json` marker. The copy is **opt-in** (`defaultEnabled` forced false, `S/contrib/runtime-loader.ts:931`). Keep `plugin.yaml name` = dashboard `manifest.json name` = desktop `id` = folder = `hypnos`.
2. **Data comes over REST, pushes come as events.** Desktop calls `ctx.rest('/hypnogram')`, which reaches `/api/plugins/hypnos/hypnogram` from `plugin_api.py`. Python pushes with `broadcast_plugin_event("hypnos", "night.stage", {...})`. Desktop subscribes with `ctx.onEvent('plugin.hypnos.night.stage', …)` and then `queryClient.invalidateQueries`. Always keep a `refetchInterval` polling fallback.
3. **There is no per-message footer or badge slot.** The only per-reply surface a plugin can reach is `TRANSCRIPT_DIRECTIVE_AREA`, a `::name{k="v"}` directive in the reply text. The Python side can append one with a `transform_llm_output` hook gated on `platform == "desktop"`. The cost: the directive is persisted and replayed to the model.
4. **The pet API is speech-bubble only**: `ctx.pet.say/clear/visible`. Plugins cannot change the pet's animation state, so "drowsy" and "asleep" are not possible. The pet also has no sleep state at all (`PetState`, `S/store/pet.ts:16`).
5. **A single-pane plugin is disabled entirely when the user closes its pane** (`S/components/pane-shell/tree/store.ts:1033-1050`). Put the hypnogram in `host.openWorkspace(...)` or in the route page, not in a lone `panes` contribution.
6. **There are no chart components.** Draw the step chart in SVG yourself with `jsx('svg')`, colored with `var(--ui-*)` tokens. Light and dark come for free because the tokens are redefined under `:root.dark`.
7. **Settings come for free.** A `config_schema` in `plugin.yaml` auto-renders under Settings → Plugins. If the desktop half also calls `ctx.registerSettingsPage`, the schema form folds in as its "Agent settings" sub-page.
8. **Imports**: only `@hermes/plugin-sdk`, `react`, `react/jsx-runtime` (and `react/jsx-dev-runtime`). No JSX syntax. Named imports of SDK exports that don't exist break the load, so feature-detect through `import * as sdk`.

---

## 1. Packaging (unified package)

**Layout** (DOC:2141-2149, source `H/hermes_cli/desktop_plugin_halves.py:1-15`):

```
~/.hermes/plugins/hypnos/
├── plugin.yaml            # agent half (name: hypnos, config_schema, provides_hooks/tools)
├── __init__.py            # register(ctx)
├── dashboard/
│   ├── manifest.json      # {"name":"hypnos","label":"Hypnos","tab":{"hidden":true},"api":"plugin_api.py"}
│   └── plugin_api.py      # router = APIRouter()  -> /api/plugins/hypnos/*
└── desktop/
    └── plugin.js          # plain ESM, default export HermesPlugin {id:'hypnos',...}
```

- A manifest with no `entry` and `tab.hidden: true` is valid. That is the backend-only shape `OP/hermes-security/dashboard/manifest.json` uses. Routes mount under the manifest `name`, which is the `/api/plugins/<name>/` prefix (`H/hermes_cli/web_server_dashboard.py:1136`).

**How it is installed and loaded**

- Electron's `reconcileUnifiedDesktopHalves` copies the package's `desktop/` (or the whole root, if `plugin.js` sits at the root) to `<appRoot>/desktop-plugins/<packageName>/` and writes a `.hermes-package.json` marker. `packageName = path.basename(packageDir)` (`S/../electron/desktop-plugins-root.ts:274-300`).
  - The tree must contain no symlinks and stay under 64 MB (`desktop-plugins-root.ts:369-371`, DOC:2153-2157).
  - The root `plugin.js` wins over `desktop/plugin.js` (DOC:2153).
- The renderer only scans `desktop-plugins/`. Each `plugin.js` and the root are fs-watched (`S/contrib/runtime-loader.ts:553-571`).
- A copy that carries the marker loads with `defaultEnabled: false` (`runtime-loader.ts:931`, cap applied at `:528-530`). The user must flip it on under Capabilities → Plugins (DOC:2200-2206).
- The Python half is gated separately. `plugin_api.py` is imported only if `hypnos` is in `plugins.enabled` in `config.yaml` (DOC:2268-2275, `web_server_dashboard.py:1060-1087`).
  - Enable with `hermes plugins enable hypnos`.
  - Routes hot-mount on the first request if they were not mounted at startup (`web_server_dashboard.py:1090-1130`). Edits to `plugin_api.py` after it is mounted need a gateway restart (DASH:932-936).
- `plugins.isolation` defaults to `in_process` (`H/hermes_cli/config_defaults.py:1719`), so `plugin_api.py` runs inside the gateway process.
- Install or share options:
  - Git URL install.
  - `hermes://plugin/install?repo=owner/repo&enable=1` (DOC:2219-2232). This opens a confirmation dialog and never auto-installs.
  - Against a **remote** backend, the desktop half must be cloned separately (DOC:2208-2217).

**Id and folder naming rules**

- Plugin ids match `^[a-z0-9_-]{1,64}$`, with no dots (`H/hermes_cli/plugin_catalog.py:56`, `H/hermes_cli/plugin_events.py:44`).
- Doc: "The folder name must equal the plugin `id`" (DOC:93).
  - Source: the loader itself does **not** reject a mismatch; it only uses the folder name for error rows (`runtime-loader.ts:737-741`).
  - Matching still matters for three reasons:
    - The settings-page fold pairs `row.name === packageOf(pluginId) || row.name === pluginId || row.key === pluginId` (`S/contrib/settings-pages.ts:226-241`).
    - `broadcast_plugin_event(plugin_id, …)` uses the catalog name.
    - Tool-media ownership is per package.
- Duplicate id across folders: first one loaded wins, and the later one errors with `duplicate id` (`runtime-loader.ts:453-461`, DOC:2476-2478).
- Contribution ids are auto-namespaced to `hypnos:<localId>`, with source `plugin:hypnos` (`S/contrib/plugin.ts:379-380`).

**Hot reload**

- Standalone disk plugin: saving `plugin.js` reloads it in place. The old registrations are disposed first (`runtime-loader.ts:1-15`, DOC:145-148). Manual fallback: ⌘K → "Reload desktop plugins".
- Unified package, **installed** (copied): editing `~/.hermes/plugins/hypnos/desktop/plugin.js` does nothing until the source mtime is newer **and** something triggers a refresh: Rescan in Capabilities → Plugins, an install or update, or a restart (DOC:2174-2181).
- Unified package, **linked dev checkout** (recommended): `ln -s ~/src/hypnos ~/.hermes/plugins/hypnos`. The marker gets `"linked": true`. The app watches the checkout's `desktop/plugin.js`, re-syncs **by content**, and hot-reloads (DOC:2183-2190, `desktop-plugins-root.ts:341-349`).
- Module evaluation has a 10 s deadline (`runtime-loader.ts:70`).
- Older shells read at most 512 KiB of `plugin.js` (`runtime-loader.ts:682-702`). Newer shells read up to 16 MB (`desktop_plugin_halves.py:35`).

**`hermes plugins doctor <path|id>`** (`H/hermes_cli/plugin_dev.py:438-501`)

- Loads the Python half through the real scanner and `register()`.
- Errors:
  - Unknown hooks.
  - Hook callbacks without `**kwargs`.
  - `provides_hooks` / `provides_tools` entries that are not strings.
- Warns on drift between declared and registered tools or hooks.
- Desktop-specific: `_check_desktop_half_copy` (`plugin_dev.py:400-435`) only warns when `desktop-plugins/<x>/plugin.js` byte-differs from your source ("Desktop runs a stale copy of desktop/plugin.js …"). That is the **only** desktop check doctor runs.

**`hermes plugins validate`** (`H/hermes_cli/plugin_validate.py:560,690`) runs the desktop lint `check_desktop_surface` (`H/hermes_cli/plugin_validate_desktop.py:175-189`).

- **Fails** on these patterns (`:20-46`):
  - `eval(` / `new Function(`
  - Dynamic `import()` of anything except `@hermes/plugin-sdk` or `react*`
  - URL-scheme imports
  - `createElement('script')` / `<script`
  - `document.querySelector*` / `getElementBy*` with `[data-slot|tour|sidebar|testid`
  - `MutationObserver.observe(document.body,{childList|subtree})`
  - Grabbing a `webview`
- **Warns** on the raw `window.hermesDesktop` and on `[data-slot…]` / `[data-tree-*]` selectors (`:54-59`).
- `_check_loadable` accepts `desktop/plugin.js` as an entry point (`plugin_validate.py:578-593`).

---

## 2. SDK exports and registration areas

The canonical barrel is `S/sdk/index.ts` (exports at `:127-130` and `:1540-1991`). The runtime import map is in `S/sdk/runtime.ts:59-68`.

**Plugin contract** (`S/contrib/plugin.ts:195-212`, `:115-193`)

```ts
interface HermesPlugin { id: string; name?: string; description?: string; defaultEnabled?: boolean;
  capabilities?: readonly string[] /* only 'preview.executeJavaScript' */; register: (ctx: PluginContext) => void }
interface PluginContext {
  readonly source: string                                   // 'plugin:hypnos'
  register(c: PluginContribution): () => void               // PluginContribution = Omit<Contribution,'source'|'id'> & {id}
  registerMany(cs: PluginContribution[]): () => void
  registerSettingsPage(page: PluginSettingsPage): () => void
  onDispose(fn: () => void): void
  onEvent(type: string, listener: (e: GatewayEvent) => void): () => void   // tracked
  setTimeout(fn, ms): () => void; setInterval(fn, ms): () => void           // tracked
  addEventListener(target, type, listener, options?): () => void          // tracked
  rest<T>(path: string, opts?: PluginRestOptions): Promise<T>               // /api/plugins/<id><path>
  socket(path: string, onMessage: (data: unknown) => void): () => void      // WS, JSON frames, auto-reconnect
  os: PluginOs; pet: PluginPet; runAction(id): PluginRunActionResult; listActions(): PluginAppActionInfo[]
  preview: PluginPreview; storage: PluginStorage; files: PluginFiles; i18n: PluginI18n }
interface Contribution { id; area; source?; title?; order?; when?: () => boolean /* NOT reactive, S/contrib/types.ts:36-41 */;
  enabled?: boolean; render?: () => ReactNode; data?: unknown }
```

**Areas.** All are exported constants. Payloads are taken from source.

| Surface | Constant = value | Shape |
|---|---|---|
| Pane | `PANES_AREA='panes'` (`S/sdk/areas.ts:5`) | `title`, `render`, `data:{placement:'main'\|'left'\|'right'\|'top'\|'bottom'\|'floating', dock?:{pane,pos}, width?, height?, anchor?, tabTitle?, collapsible?, hideOnly?, uncloseable?}` (DOC:257-295, `S/components/pane-shell/tree/store.ts:1540-1600`) |
| Full page | `ROUTES_AREA='routes'` (`S/app/routes.ts:88`) | `data:{path:'/hypnos-report'}` (one segment, no params) + `render` (`routes.ts:91-94`) |
| Sidebar nav row | `SIDEBAR_NAV_AREA='sidebar.nav'` (`routes.ts:128`) | `data:{path,label,codicon,tier?:'advanced'}` (`routes.ts:131-139`) |
| Sidebar nav hide/order | `SIDEBAR_NAV_PREFS_AREA='sidebarNav.prefs'` | `data:{hide?:string[],order?:string[]}` (DOC:1315-1377) |
| Profile-group header | `SIDEBAR_PROFILE_GROUP_HEADER_AREA` (`routes.ts:148`) | `data:{render:(route:{connectionId,profile})=>ReactNode}` |
| Status bar | `STATUSBAR_AREAS={left:'statusBar.left',right:'statusBar.right'}` (`areas.ts:6`) | `render`, or `data` as `StatusbarItem{id,render?,label?,detail?,icon?,menuItems?,menuContent?,onSelect?,title?,to?,variant?,toggleLabel?,…}` (`S/app/shell/statusbar-controls.tsx:58-90`; consumer `S/app/contrib/panes.tsx:165-187`) |
| Title bar | `TITLEBAR_AREAS={left,center,right}` → `'titleBar.*'` (`areas.ts:11`) | `render` slots. Note: `TitlebarTool` *data* is read from `'titleBar.tools.<side>'`, not `TITLEBAR_AREAS` (`panes.tsx:189-193`), which is a doc/code mismatch (DOC:340) |
| Page header | `WORKSPACE_PAGE_HEADER_AREA='workspace.pageHeader'` | use `<WorkspacePageHeaderControl id>` inside your page (DOC:1294-1313) |
| ⌘K palette | `PALETTE_AREA='palette'` | `data:{id,label,run,keywords?,icon?,action?,detail?:()=>string,detailVariant?,keepOpen?}` (`S/app/command-palette/contrib.ts:13-31`) |
| Keybind | `KEYBINDS_AREA='keybinds'` | `data:{id,label,category,defaults:['mod+shift+r'],run}` (DOC:375-385) |
| Theme | `THEMES_AREA='themes'` | `data: DesktopTheme` |
| Composer | `COMPOSER_AREAS={top,bottom,underside,leading,actions,middleware,attachments,microActions,atCompletions,modelPill,contextMenu}` (`S/app/chat/composer/contrib.ts:32-44`) | render slots / providers (DOC:486-801) |
| Model menu row | `MODEL_MENU_ROW_AREA='modelMenu.row'` | `data:{decorate:({provider,model,label})=>{icon?,badge?}\|null}` |
| Kanban badges | `KANBAN_CARD_AREAS.badges` | `data:{decorate(card,board)=>badge\|badge[]\|null}` (24-char label, 2 per plugin per card) |
| Appearance | `APPEARANCE_AREAS.extra='appearance.extra'` | `render` |
| Settings → Plugins | `SETTINGS_PLUGINS_AREA='settings.plugins'` (`S/contrib/settings-pages.ts:33`) | use `ctx.registerSettingsPage({id,title,render,icon?,order?,children?:[{id,title,render}]})` (`settings-pages.ts:39-60`) |
| Transcript directive | `TRANSCRIPT_DIRECTIVE_AREA='transcript.directives'` (`S/lib/transcript-directives.ts:23`) | `data:{name:'hypnos-badge', render:({attrs,source,streaming,sessionId})=>ReactNode}` (`:26-50`) |
| Tool media | `TOOL_MEDIA_RENDERER_AREA='transcript.toolMedia'` (`S/lib/tool-media-renderers.ts:20`) | `data:{tools:['own_tool' \| 'mcp__x__*'], render:({file,toolName,toolCallId,args,result,sessionId,fallback})=>ReactNode}` (`:22-60`) |
| Session row | `SESSION_ROW_AREAS={leading:'sessionRow.leading',trailing:'sessionRow.trailing'}` (`S/lib/session-row-slots.ts:19-22`) | `data:{render:({sessionId /* durable/lineage-root id */})=>ReactNode\|null}` |
| Empty chat | `CHAT_EMPTY_AREA='chat.empty'` (`S/lib/chat-empty.ts:22`) | `data:{render:({sessionId})=>ReactNode\|null}` |
| Layout presets | `LAYOUTS_AREA='layouts'` (not exported; core only) | n/a |

**Non-area doors**

- **Toasts**:
  - `host.notify({kind?:'error'|'warning'|'info'|'success', message, title?, detail?, icon?, accentColor?, meta?, action?:{label,onClick}, secondaryAction?, durationMs?, persistent?, placement?, id?}) → id` (`S/store/notifications.ts:10,41-55`)
  - `host.notifyError(err, fallback)`
- **OS notification**: `ctx.os.notify({title, body?, icon?, activate?:'/hypnos-report', onActivate?, actions?})`. It only fires while the app is unfocused.
- **Navigation**: `host.navigate('/hypnos-report')` (`S/sdk/index.ts:643-654`); `pluginSettingsHref('hypnos','page')` (`settings-pages.ts:328-330`).
- **Workspace tab**: `host.openWorkspace(id, {render, title?, minWidth?, onClose?, dock?:{pane,pos,before?}, uncloseable?, headerVeto?}) → close()` (`index.ts:1175-1225`). `dock` and `uncloseable` exist in source but are not documented.
- `host.paneVisibility('<pluginId>:<paneId>') → ReadableAtom<boolean>` (`index.ts:1299`).
- **State atoms** (`index.ts:558-605`): `activeSessionId`, `awaitingResponse`, `busy`, `busyBySession`, `connectionId`, `cwd`, `focusedSessionId` (runtime), `focusedSessionOwner`, `focusedSessionProfile`, `focusedStoredSessionId` (durable), `focusedUsage` (`UsageStats`), `gateway`, `model`, `profile`, `viewport`.
- **Gateway**:
  - `host.request<T>(method, params?, timeoutMs?)` (`index.ts:1506-1514`)
  - `host.onEvent(type, fn)` (`index.ts:1337`); prefer the tracked `ctx.onEvent`
  - `host.requestProfile(route, method, params?, timeoutMs?, {spawnPriority?})`
- **Sessions**: `host.openSession(id,{profile?,intent?})`, `host.newChat(profile?)`, `host.sessions.{pin,reorder,reorderPinned,setColor}`.
- **Composer**: `host.composer.{getDraft,setDraft,insertText,submit,focus}` (DOC:621-628).
- `ctx.runAction('nav.settings' | …)`. Only view and navigation actions are allowed (DOC:1782-1800).
- **Storage**: `ctx.storage.{get(key,fallback),set,remove}` (localStorage under `hermes.plugin.<id>.`); `ctx.files.*` (own `'data'` folder plus folders the user grants).
- **React and state**: `useValue` (nanostores `useStore`), `atom`, `computed`, `useQuery`, `useMutation`, `useQueryClient`, `queryClient` (the app's one QueryClient), `Contribute`, `WorkspacePageHeaderControl`.
- **UI kit** (`index.ts:1612-1769`):
  - Basic controls: `Button`, `Input`, `Textarea`, `Select*`, `Switch`, `Checkbox`, `SegmentedControl{options:[{id,label}],value,onChange,sizing?}`, `Tabs*`.
  - Overlays and menus: `Dialog*`, `ConfirmDialog`, `DropdownMenu*`, `ContextMenu*`, `Popover*`, `Tip{label,children}`, `Tooltip*`.
  - Display: `Badge`, `Kbd*`, `SearchField`, `ScrollArea`, `FadeScroll`, `Separator`, `Skeleton`, `GlyphSpinner`, `Loader`, `EmptyState`, `ErrorState`, `CopyButton`, `StatusDot`, `SessionStatusDot`, `LogView`, `Codicon{name}`, `DecodeText`, `SandboxedFrame`, `RowButton`, `DisclosureCaret`.
  - Panels and rows: `Panel*` (master-detail parts), `ListRow{title,description?,hint?,action?,below?,wide?}`, `ToggleRow{checked,label,description?,onChange,disabled?}` (`S/app/settings/primitives.tsx:153-228`), `ColorSwatches`, `MessageTextContent`, `Streamdown`, `Blobatar`.
- **Helpers**:
  - Styling and identity: `cn`, `icons.*` (lucide), `haptic`, `profileColor`, `PROFILE_SWATCHES`.
  - Formatting: `relativeTime`, `fmtDateTime`, `fmtDayTime`, `coarseElapsed`, `formatAgo`, `compactNumber` (1230 → "1.2k", `index.ts:1957`).
  - Animation and caching: `createBudgetedLoop(draw,{fps?,idleWhen?,pauseWhenUnfocused?})→{dispose,wake,isDormant}` (`S/lib/budgeted-loop.ts:38-64`), `LruCache`, `randomUuid`.
  - i18n: `useI18n`, `usePluginI18n`, `translateNow`.
  - Theming: `useTheme()→{theme,themeName,availableThemes,resolvedMode,setTheme,setMode,previewTheme}`, `requestTheme`, `setAccentOverride`, plus OKLCH math and `contrastRatio`.

---

## 3. Data channel (desktop ↔ Python)

| Door | Direction | Exact API | Notes |
|---|---|---|---|
| Plugin REST | desktop → py | `ctx.rest<T>(path, {method?, body?, upload?:{filename,contentType?,bytes}, timeoutMs?})` (`S/api/plugins.ts:45-88`) | path is relative to `/api/plugins/hypnos`; `..` is refused; profile-aware. Rejects with `"<status>: <body>"` (`OP/elevenlabs-music/src/audio.ts:258`). Python: `plugin_api.py` exports `router = APIRouter()` (DOC:2247-2262). Auth is the dashboard gate (DASH:750). |
| Plugin WS | py → desktop | `ctx.socket('/events', data => …)` → disposer (`api/plugins.ts:138-285`); Python `@router.websocket('/events')` | No-op on OAuth remotes and in the browser-hosted Webapp (`api/plugins.ts:192-197`). HTTP 501 under the plugin host (DOC:2352-2355). Stops after 5 refusals. Treat it as an accelerator only. |
| Global plugin event | py → desktop | Python: `from hermes_cli.plugin_events import broadcast_plugin_event`; `broadcast_plugin_event(plugin_id: str, event: str, payload: dict\|None=None) -> None` (`H/hermes_cli/plugin_events.py:66`). Desktop: `ctx.onEvent('plugin.hypnos.<event>', ({payload}) => …)` | Wire name `plugin.<id>.<event>`. `event` is dotted `[A-Za-z0-9_-]` segments of at most 128 chars; anything else raises `ValueError` (`:41-62`). Fire-and-forget to every connected window (`H/tui_gateway/server.py:632-648`). **Only delivered if called inside the process Desktop is attached to** (`hermes serve`: plugin_api routers, slash commands, agent-turn tools and hooks). From `hermes chat` or cron it is a logged no-op (DOC:2306-2311). |
| Settings change | py → desktop | auto event `plugin.hypnos.settings.changed` with payload `{written:[keys]}` (PIDX:1050-1055, `H/tui_gateway/methods_tools.py:1875`) | Read values in `plugin_api.py` via `load_config_readonly()` → `plugins.entries.hypnos.settings`. |
| Gateway JSON-RPC | desktop → gateway | `host.request(method, params)`, e.g. `plugins.manage {action:'list'\|'settings', key, values, profile?}` (`S/store/agent-plugins.ts:167,724`) | No API lets a Python plugin register custom RPC methods (Python `register_*` list: `H/hermes_cli/plugins.py:437-959`). Use REST. |
| Gateway events | gateway → desktop | `ctx.onEvent(type,fn)` / `'*'`; envelope `GatewayEvent{type, session_id?, payload?, seq?, profile?, connectionId?, …}` (`apps/shared/src/gateway-events.ts:57-87`) | Useful: `message.start{turn_id}`, `message.complete{text,usage,status,persisted_turn{final_assistant_row_id,…}}` (`gateway-contract.generated.ts:5745-5846`), `tool.start/complete/progress`, `session.usage`, `gateway.ready`. |
| Py → user notice | py → desktop toast | `ctx.post_message(...)`. On Desktop and the TUI this is a keyed toast or status line (`H/website/docs/user-guide/features/plugins.md:1043-1051`) | Not available under `plugins.isolation: host`. |

**There is no `ctx.api.fetch`.** The dashboard's `SDK.fetchJSON` belongs to the separate web-dashboard SDK and does not exist in Desktop (DOC:23-33).

**Live-update pattern.** This is the one the official plugins use: hermes-security (`OP/hermes-security/desktop/plugin.js:129-146,1803-1805`; Python `dashboard/plugin_api.py:112-117`) and memory-wiki (`OP/memory-wiki/desktop/plugin.js:116-155`).

```js
// desktop
const KEY = ['hypnos']
ctx.onEvent('plugin.hypnos.night.stage', () => queryClient.invalidateQueries({ queryKey: KEY }))
ctx.onEvent('plugin.hypnos.night.done',  () => queryClient.invalidateQueries({ queryKey: KEY }))
ctx.onEvent('gateway.ready', () => queryClient.removeQueries({ queryKey: KEY }))
ctx.onDispose(() => queryClient.removeQueries({ queryKey: KEY }))
// in a component; scope keys by connection+profile like memory-wiki:
useQuery({ queryKey: [...KEY, conn, profile, 'night'], queryFn: () => ctx.rest('/night'),
           refetchInterval: q => (q.state.data?.running ? 3000 : false), retry: false })
```

```python
# dashboard/plugin_api.py (runs in-process in the gateway, default isolation)
def _emit(event, payload):
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event("hypnos", event, payload)
    except Exception:
        pass
```

**Storage on the Python side**:

- `from plugins.plugin_storage import plugin_db, plugin_data_dir` gives a SQLite DB at `<HERMES_HOME>/plugin-data/hypnos/data.db` (WAL, profile-scoped) (PIDX:1153-1173; `H/plugins/plugin_storage.py:27,36`).
- `ctx.state.get/set` gives JSON state up to 10 MiB (PIDX:957-988).
- `ctx.get_config/set_config` work on `plugins.entries.hypnos.settings.*`.

**Implication for the "night" job.** Run it in the gateway process, for example as a thread or task started by a `POST /night/start` route or from a hook. `broadcast_plugin_event` then reaches the desktop. If it runs from cron or an external process, the event is dropped and only polling works. Don't block the event loop: use `async def` or `asyncio.to_thread` (DASH:752-761).

---

## 4. Per-reply metadata ("brain v7 · 1.2k tokens saved" / "frontier")

**Surfaces checked**

- **Message footer / per-message slot: none.** `AssistantMessage` renders `MESSAGE_PARTS`, `StoppedNotice`, `AssistantStatusSlot`, `AssistantFooter`, `SettledChangedFiles` and `StreamingMarker`. None of them consults the registry (`S/components/assistant-ui/thread/assistant-message.tsx:320-352`). The only registry consumers in the transcript are the directive, tool-media and chat-empty slots.
- **`TRANSCRIPT_DIRECTIVE_AREA`: the only per-reply render surface.**
  - The model or text emits `::hypnos-badge{turn="…" v="7" saved="1200"}` and the plugin renders it inline.
  - Attrs are untrusted strings; the brace body is capped at 2048 chars (`S/lib/transcript-directives.ts:64-71`).
  - The name must be lowercase `[a-z0-9-]`, and the first registration wins on a name collision.
  - An unclaimed directive (plugin off) renders as plain text.
  - Position rule: the doc says the directive must be the whole paragraph (DOC:1142-1143). Source now recognizes it wherever it starts a word (`transcript-directives.ts:13-18`). Emitting it as its own trailing paragraph satisfies both.
- **`TOOL_MEDIA_RENDERER_AREA`: not applicable.** It only replaces the player for `MEDIA:` files produced by the package's own registered tools (DOC:1241-1257).
- **`SESSION_ROW_AREAS.trailing`**: works per session, not per reply. Good for "brain v7" on each sidebar row.
- **`host.state.focusedUsage` / `message.complete.usage`**: give live token usage for the focused chat; usable for a status-bar "last reply" chip.

**Attaching metadata on the Python side.** Python cannot attach side-channel metadata to a reply that Desktop reads per message. The supported options:

1. **Append a directive via `transform_llm_output`** (PIDX:1515-1524; `H/website/docs/user-guide/features/hooks.md:1756-1790`).
   - Signature: `def cb(response_text: str, session_id: str, model: str, platform: str, **kwargs) -> str | None`. `kwargs` includes `turn_id` (`H/agent/turn_finalizer.py:535-536`).
   - Desktop chat sessions report `platform == "desktop"` (`H/tui_gateway/agent_factory.py:29-33`; also `ctx.session_surface()`). Gate on that and return `response_text + '\n\n::hypnos-badge{turn="<turn_id>"}'`.
   - Costs:
     - The rewrite is what gets persisted and replayed to the model next turn ("the replacement is what the session stores … what the next turn replays").
     - Raw text shows up if the chat is reopened in the TUI or CLI.
     - "First non-empty string wins" among plugins (`turn_finalizer.py:541-543`).
     - Without `hold_stream`, Desktop shows the streamed reply and then swaps in the rewrite at completion.
   - Keep the attrs to a `turn` key and have the badge component fetch `ctx.rest('/turns/<turn_id>')` for the details. That keeps the replayed tokens tiny.
2. **Side table plus event (no transcript change).**
   - In `post_llm_call(session_id, task_id, turn_id, assistant_response, model, platform, **kw)` (`turn_finalizer.py:494-505`), write `{turn_id, session_id, brain_version, tokens_saved, mode}` to SQLite and call `broadcast_plugin_event("hypnos","turn.tagged",{...})`.
   - On Desktop, show it as a status-bar chip for the focused session and as session-row badges. It is not inline with the message.
   - Match `session_id` against `host.state.focusedStoredSessionId`. Inferred: compression rotates ids, and row slots hand the lineage-root id.

---

## 5. Pet API

- `ctx.pet` exists (`S/contrib/plugin.ts:95-107`, `:292-307`; DOC:1680-1742). Signatures:

```ts
interface PluginPet {
  say(text: string, options?: { id?: string; tone?: 'info' | 'wait' | 'error'; ttlMs?: number }): () => void
  clear(id?: string): void
  visible: ReadableAtom<boolean>   // $petActive: pet enabled AND spritesheet loaded (S/store/pet.ts:143)
}
```

- Limits (`S/store/pet-plugin-messages.ts:49-56`):
  - Text only, capped at 120 chars and collapsed to one line.
  - TTL defaults to 6 s and is clamped to 1–30 s.
  - At most 3 live lines per plugin.
  - 10 `say` calls per 10 s; extras are dropped.
  - Core error and waiting states take priority over plugin lines.
  - Each line is labelled with the plugin name.
  - Lines are cleared when the plugin unloads.
- **Plugins cannot control the animation state.** `PetState = 'idle'|'wave'|'run'|'failed'|'review'|'jump'|'waiting'` (`S/store/pet.ts:16`) is derived from chat activity (`:107-136`). There is no sleep state, and `setPetActivity` is not exported from the SDK. Pointer input on the pet belongs to the host (DOC:1740-1742).
- `host.request('pet.select', {slug})` exists on the wire (`gateway-contract.generated.ts:6419`) and could swap the user's pet. That would override a user choice, so don't.

---

## 6. Settings

**Automatic form from `config_schema`** (PIDX:990-1055; source `H/hermes_cli/plugins_settings.py`, `S/store/agent-plugins.ts:66-86`)

- The form appears under Settings → Plugins. The gear on the Capabilities row also opens it, at `/settings?tab=plugins&agent=hypnos`.
- The backend `plugins.manage list` returns `settings_schema`, which includes the current values.
- Saves go through `plugins.manage {action:'settings', key, values}`. This uses the same writer as `ctx.set_config`, writing `plugins.entries.hypnos.settings.<key>`. Saves are validated against `type` and `choices`, and each save broadcasts `plugin.hypnos.settings.changed`.
- Manifest `type` to field mapping (`plugins_settings.py:24-30,79-86`):

| Manifest `type` | Renders as |
|---|---|
| `str` | text input; `choices:`/`enum:` turns it into a dropdown |
| `int` / `float` | number input |
| `bool` | switch |
| `list` / `dict` | JSON editor |
| `secret` | masked input, stored in `.env` under `env:` or `HYPNOS_<KEY>` |

- Every field also takes `label`/`title`, `description`, `default` and `required`. There are no `min`/`max`, step, slider or sections.

```yaml
config_schema:
  pressure_threshold: {type: float, default: 0.8, label: "Sleep pressure threshold", description: "0–1; night starts above this"}
  night_mode:         {type: str, choices: [auto, manual], default: auto}
  show_reply_badge:   {type: bool, default: true, label: "Badge replies in Desktop"}
```

**Custom page** (DOC:994-1036; `S/contrib/settings-pages.ts`)

- Register with `ctx.registerSettingsPage?.({id:'settings', title:'Hypnos', icon:'moon'?, order?, render, children:[{id,title,render}]})`. Build it from `ListRow`, `ToggleRow`, `SegmentedControl` and `Select*`. Persist desktop-only prefs with `ctx.storage`.
- In a unified package, when the desktop half registers a page, the schema form folds in as that entry's **"Agent settings"** sub-page. The fold only happens when `row.name === packageName || row.name === pluginId || row.key === pluginId` (`settings-pages.ts:221-241`), so keep the names identical.
- To edit agent config from a custom page, call `host.request('plugins.manage', {action:'settings', key:'hypnos', values:{...}})`. For secrets, use the form; secrets go through `PUT /api/env`.
- Deep link: `host.navigate(pluginSettingsHref('hypnos'))` gives `/settings?tab=plugins&plugin=hypnos`.

---

## 7. Charting and theme tokens

**No chart components in the SDK.**

- The barrel contains nothing chart-related (`S/sdk/index.ts`). `d3-force` is an app dependency but cannot be imported (import allowlist).
- Draw the hypnogram yourself: `jsx('svg', {viewBox, children:[jsx('path'|'rect', …)]})`.
- Size it from a `ResizeObserver` inside `useEffect`, with cleanup (DOC:2464-2465).
- Animate only through `createBudgetedLoop`. Raw rAF loops are discouraged (`index.ts:1813-1817`).
- For click-for-details, use `Popover`/`Tip` or local state plus a side panel.
- Color through `style={{fill:'var(--ui-blue)'}}` or `currentColor`. Inferred: `var()` inside SVG presentation *attributes* is less reliable than in `style`.

**Tokens** (defined on `:root` = light and overridden on `:root.dark`; `S/styles.css:203-257,364-367,600-613`)

- Text: `--ui-text-primary`, `--ui-text-secondary`, `--ui-text-tertiary`, `--ui-text-quaternary`.
- Accent: `--ui-accent`, `--ui-accent-secondary`, `--ui-warm`, `--ui-base`.
- Hues (light and dark values differ): `--ui-red`, `--ui-orange`, `--ui-yellow`, `--ui-green`, `--ui-cyan`, `--ui-blue`, `--ui-purple`.
- Strokes: `--ui-stroke-primary`, `--ui-stroke-secondary`, `--ui-stroke-tertiary`, `--ui-stroke-quaternary`.
- Backgrounds: `--ui-bg-primary`, `--ui-bg-secondary`, `--ui-bg-tertiary`, `--ui-bg-quaternary`, `--ui-bg-quinary`, `--ui-bg-card`, `--ui-bg-elevated`, `--ui-bg-input`, `--ui-bg-chrome`, `--ui-bg-editor`, `--ui-bg-sidebar`.
- Rows and controls: `--ui-row-hover-background`, `--ui-row-active-background`, `--ui-control-hover-background`, `--ui-control-active-background`, `--ui-selection-background`.
- Surfaces: `--ui-surface-background`, `--ui-widget-surface-background`.
- Diff: `--ui-diff-add-*`, `--ui-diff-remove-*`.
- Tailwind color aliases: `--color-{background,foreground,card,muted,muted-foreground,border,ring,primary,accent,destructive,…}` (`styles.css:127-160`).
- Documented contract: leave the pane background alone and use `var(--ui-*)` for everything else (DOC:2116-2122).
- For a canvas: `getComputedStyle(el).getPropertyValue('--ui-accent')`.
- Theme mode: `useTheme().resolvedMode`.
- Suggested stage palette: wake `--ui-yellow`, REM `--ui-purple`, N1 `--ui-cyan`, N2 `--ui-blue`, N3 `--ui-accent` or `color-mix(in srgb, var(--ui-blue) 60%, var(--ui-base))`. All of these follow light and dark.

**Tailwind caveat (inferred).** The app compiles Tailwind v4 at build time from its own sources: `styles.css:1` has `@import 'tailwindcss'`, there is no `@source` for plugin dirs, and the vite plugin is at `vite.config.ts:140`. A utility class that only a disk plugin uses (for example `h-[37px]`) will probably have no CSS. Use common utilities the app already uses, or inline `style`.

---

## 8. Rules and restrictions

- **Imports.**
  - Only `@hermes/plugin-sdk`, `react`, `react/jsx-runtime` and `react/jsx-dev-runtime` are allowed (`S/sdk/runtime.ts:59-68`). Anything else, relative paths and URL imports included, fails with `unsupported import` (`runtime-loader.ts:337-399`).
  - So everything goes in **one file**, with no JSX syntax.
  - A named import of a missing SDK export fails module link. Feature-detect via `import * as sdk` (DOC:1309-1313, `OP/elevenlabs-music/src/plugin.tsx:18-20,109`).
- **Build step.** None is needed. memory-wiki and hermes-security are hand-written ESM. elevenlabs-music bundles TSX with esbuild and marks the SDK and React external (`OP/elevenlabs-music/package.json` `build`).
- **No DOM reach into the app, no `eval` or `new Function`, no script injection, no `webview`.** These are enforced at validate or catalog admission (`H/hermes_cli/plugin_validate_desktop.py:20-46`), not at runtime.
  - Prototype patching is not linted, but catalog review checks it by hand (`:17-19`).
  - The documented contract is to use slots and SDK hooks only (DOC:476-479, "catalog rule 8").
- **Timers and listeners.**
  - Use `ctx.setTimeout/setInterval/addEventListener` and `ctx.onEvent`, or `useEffect` cleanup. Wire everything else to `ctx.onDispose`.
  - Bare globals survive disable and hot reload, and ES modules can't be unloaded, so module-scope state is yours to reset (DOC:2468-2472).
  - `host.onEvent` calls made during `register()` are tracked; calls made later are not (`S/contrib/events.ts:16-47`).
- **Disposal.** `host.openWorkspace` registers with source `core` (`index.ts:1195`). Call its disposer in `ctx.onDispose` (elevenlabs does).
- **Polling.** Don't poll faster than every few seconds; let React Query dedupe (DOC:2466-2467).
- **`when()` is not reactive.** It is re-evaluated only on registry mutation in that area (`S/contrib/types.ts:36-41`). To show or hide dynamically, re-register the same `id` (that replaces it) or return `null` from render.
- **Network.** There is no fetch allowlist for the renderer in the SDK. The intended door is `ctx.rest` to your own backend. Do external HTTP in Python.
  - The security model is error isolation only, with full app authority (DOC:2419-2447). A CSP and sandbox are explicitly *not* present for disk plugins.
  - `SandboxedFrame` exists for external content (DOC:1038-1093).
- **Colors.** No hardcoded colors (DOC:2456-2457).
- **Profiles and remotes.**
  - `ctx.rest` follows the active profile and connection. Scope query keys by `host.state.connectionId` and `host.state.profile`, and refuse stale results (memory-wiki pattern, `OP/memory-wiki/desktop/plugin.js:122-142`).
  - `ctx.socket` is unavailable on OAuth remotes and in the Webapp.

---

## 9. Conflicts with the wanted features, and the closest supported alternative

| Wanted | Conflict | Closest supported alternative |
|---|---|---|
| Pet drowsy, asleep, awake | No plugin control of pet animation; no sleep state (`S/store/pet.ts:16,107-136`) | `ctx.pet.say` lines keyed by id, re-said at most every ~25 s: "yawn… pressure 72%" (`tone:'info'`), "zzz — consolidating (N2)" (`tone:'wait'`) during the night, and "Morning report ready" at wake. Gate on `ctx.pet.visible`. Carry the real state in the status-bar moon. |
| Per-reply badge | No message footer slot | `transform_llm_output` appends `::hypnos-badge{turn="…"}` (desktop-only via `platform`). Note the transcript replay cost and the first-transform-wins conflict. Fallback: status-bar "last reply" chip plus `SESSION_ROW_AREAS.trailing` badge, fed by a `turn.tagged` event and REST. |
| Hypnogram pane | Closing a single-pane plugin's only pane disables the **whole** plugin, status bar included (`store.ts:1033-1050`, DOC:291-295) | `host.openWorkspace('hypnos-hypnogram', {title:'Hypnogram', dock:{pane:'workspace',pos:'bottom'}, render, onClose})`, opened from the status-bar moon or ⌘K. Closing it only fires `onClose`. Also embed the chart in the `/hypnos` route page. |
| Live updates during the night | `broadcast_plugin_event` is a no-op outside the gateway process; `ctx.socket` is a no-op on OAuth remotes, 501 under `plugins.isolation: host` | Run the night job in-process (started by a plugin_api route or a hook), broadcast `plugin.hypnos.night.*`, and poll `refetchInterval: 3000` while `running`. |
| Settings page for the schema | Schema form has no sliders, ranges, sections or custom validation | Keep `config_schema` (it renders automatically). Add `ctx.registerSettingsPage` for rich controls; the schema form folds in as "Agent settings". Write agent keys with `host.request('plugins.manage',{action:'settings',key:'hypnos',values})`. |
| Report page plus sidebar nav | None | `ROUTES_AREA {path:'/hypnos'}` + `SIDEBAR_NAV_AREA {path:'/hypnos',label:'Hypnos',codicon:'…'}` + `PALETTE_AREA`. Use `ctx.os.notify({activate:'/hypnos'})` for the morning ping when the app is backgrounded, and `host.notify({action:{label:'Open', onClick:()=>host.navigate('/hypnos')}})` in-app. |
| Status-bar meter | None | `STATUSBAR_AREAS.right` with `render`: an SVG moon whose fill is `var(--ui-accent)` with a clipPath for pressure, a `Tip` for detail, and `onClick` opening the hypnogram. Use `toggleLabel` (as a `StatusbarItem`) if it should be hideable. |
| SDK-only, no DOM query, theme tokens | None | All of the above are SDK doors. |

---

## 10. Minimal `desktop/plugin.js` skeleton (SDK-only, no build)

```js
import * as sdk from '@hermes/plugin-sdk'
import { host, useValue, useQuery, queryClient, Tip, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA,
  STATUSBAR_AREAS, SESSION_ROW_AREAS, ListRow, ToggleRow, compactNumber } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hypnos', KEY = [ID]
export default {
  id: ID, name: 'Hypnos', description: 'Sleep cycles for your agent brain',
  register(ctx) {
    let closeHypnogram = null
    const openHypnogram = () => {
      if (typeof host.openWorkspace !== 'function') return host.navigate('/hypnos')
      closeHypnogram = host.openWorkspace('hypnos-hypnogram', { title: 'Hypnogram',
        dock: { pane: 'workspace', pos: 'bottom' }, render: () => jsx(Hypnogram, { ctx }),
        onClose: () => { closeHypnogram = null } })
    }
    ctx.onDispose(() => { closeHypnogram?.(); queryClient.removeQueries({ queryKey: KEY }) })
    for (const ev of ['night.stage', 'night.started', 'night.done', 'pressure.changed', 'turn.tagged', 'settings.changed'])
      ctx.onEvent(`plugin.${ID}.${ev}`, () => queryClient.invalidateQueries({ queryKey: KEY }))
    ctx.registerMany([
      { id: 'moon', area: STATUSBAR_AREAS.right, order: 125, render: () => jsx(Moon, { ctx, onOpen: openHypnogram }) },
      { id: 'page', area: ROUTES_AREA, data: { path: '/hypnos' }, render: () => jsx(Report, { ctx }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/hypnos', label: 'Hypnos', codicon: 'eye-closed' } },
      { id: 'open', area: PALETTE_AREA, data: { id: 'hypnos.hypnogram', label: 'Hypnos: open hypnogram', run: openHypnogram } },
      { id: 'row', area: SESSION_ROW_AREAS.trailing, data: { render: ({ sessionId }) => jsx(RowBadge, { ctx, sessionId }) } },
    ])
    if (typeof sdk.TRANSCRIPT_DIRECTIVE_AREA === 'string')
      ctx.register({ id: 'badge', area: sdk.TRANSCRIPT_DIRECTIVE_AREA,
        data: { name: 'hypnos-badge', render: p => jsx(ReplyBadge, { ctx, ...p }) } })
    ctx.registerSettingsPage?.({ id: 'settings', title: 'Hypnos', icon: 'eye-closed', render: () => jsx(Prefs, { ctx }) })
  }
}
// Moon / Hypnogram / Report / RowBadge / ReplyBadge / Prefs: function components using
// useQuery({queryKey:[...KEY, conn, profile, path], queryFn:()=>ctx.rest(path), refetchInterval})
// and inline-SVG with style:{fill:'var(--ui-…)'}; pet lines via ctx.pet.say(text,{id:'state',tone,ttlMs:25000}).
```

Codicon names must be valid VS Code codicons. Verify `eye-closed` or pick `moon`-like alternatives such as `circle-large-filled`.
