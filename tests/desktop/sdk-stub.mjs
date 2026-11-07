import React from 'react'
import { atom } from 'nanostores'
import { useStore } from '@nanostores/react'
import { QueryClient, useQuery as rqUseQuery } from '@tanstack/react-query'
export const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
export const useQuery = opts => rqUseQuery(opts, queryClient)
export const useValue = useStore
export const host = { state: { connectionId: atom('c1'), profile: atom('default') }, navigate: () => {}, openWorkspace: () => () => {}, notify: () => {} }
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const PALETTE_AREA = 'palette'
export const SESSION_ROW_AREAS = { leading: 'sessionRow.leading', trailing: 'sessionRow.trailing' }
export const TRANSCRIPT_DIRECTIVE_AREA = 'transcript.directives'
export function Tip({ label, children }) { return React.createElement('span', { 'data-tip': 'x' }, children) }
export const compactNumber = n => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n))
