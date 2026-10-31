import { describe, expect, it } from 'vitest'

import { pageLoaders, resolveRouteKey } from '@/router/page-loaders'

describe('route page loaders', () => {
  it('registers every route page exactly once', () => {
    expect(Object.keys(pageLoaders)).toEqual([
      'login',
      'dashboard',
      'gettingStarted',
      'stocks',
      'opportunities',
      'newsFlow',
      'paperTrading',
      'assistant',
      'alerts',
      'agents',
      'evaluations',
      'history',
      'schedulerHistory',
      'logs',
      'tokenUsage',
      'dataSources',
      'settings',
      'analysisDetail',
    ])
  })

  it('resolves both assistant routes to the same page loader', () => {
    expect(resolveRouteKey('/assistant')).toBe('assistant')
    expect(resolveRouteKey('/assistant/123')).toBe('assistant')
  })

  it('does not preload an unknown route', () => {
    expect(resolveRouteKey('/missing')).toBeNull()
  })

  it('resolves the recoverable setup page', () => {
    expect(resolveRouteKey('/getting-started')).toBe('gettingStarted')
  })

  it('resolves event-driven news flow and logs pages', () => {
    expect(resolveRouteKey('/news-flow')).toBe('newsFlow')
    expect(resolveRouteKey('/scheduler-history')).toBe('schedulerHistory')
    expect(resolveRouteKey('/logs')).toBe('logs')
    expect(resolveRouteKey('/token-usage')).toBe('tokenUsage')
  })
})
