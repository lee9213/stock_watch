import { afterEach, describe, expect, it } from 'vitest'

import {
  clampSidebarWidth,
  isSidebarCollapsed,
  persistSidebarWidth,
  readStoredSidebarWidth,
  SIDEBAR_DEFAULT_WIDTH,
  SIDEBAR_MAX_WIDTH,
  SIDEBAR_MIN_WIDTH,
  SIDEBAR_STORAGE_KEY,
  toggleSidebarWidth,
} from '@/layout/sidebar-layout'

describe('sidebar-layout', () => {
  afterEach(() => {
    window.localStorage.removeItem(SIDEBAR_STORAGE_KEY)
  })

  it('clamps width into the allowed range', () => {
    expect(clampSidebarWidth(12)).toBe(SIDEBAR_MIN_WIDTH)
    expect(clampSidebarWidth(999)).toBe(SIDEBAR_MAX_WIDTH)
    expect(clampSidebarWidth(Number.NaN)).toBe(SIDEBAR_DEFAULT_WIDTH)
  })

  it('treats icon-rail widths as collapsed', () => {
    expect(isSidebarCollapsed(64)).toBe(true)
    expect(isSidebarCollapsed(256)).toBe(false)
  })

  it('toggles between the rail and the last expanded width', () => {
    expect(toggleSidebarWidth(256, 320)).toBe(SIDEBAR_MIN_WIDTH)
    expect(toggleSidebarWidth(64, 320)).toBe(320)
  })

  it('persists and restores the sidebar width', () => {
    persistSidebarWidth(300)
    expect(window.localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe('300')
    expect(readStoredSidebarWidth()).toBe(300)
  })
})
