export const SIDEBAR_STORAGE_KEY = 'panwatch_sidebar_width'
export const SIDEBAR_DEFAULT_WIDTH = 256
export const SIDEBAR_MIN_WIDTH = 64
export const SIDEBAR_MAX_WIDTH = 400
export const SIDEBAR_COLLAPSE_WIDTH = 72

export function clampSidebarWidth(width: number): number {
  if (!Number.isFinite(width)) return SIDEBAR_DEFAULT_WIDTH
  return Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, Math.round(width)))
}

export function isSidebarCollapsed(width: number): boolean {
  return clampSidebarWidth(width) <= SIDEBAR_COLLAPSE_WIDTH
}

export function readStoredSidebarWidth(): number {
  if (typeof window === 'undefined') return SIDEBAR_DEFAULT_WIDTH
  const raw = window.localStorage.getItem(SIDEBAR_STORAGE_KEY)
  if (!raw) return SIDEBAR_DEFAULT_WIDTH
  return clampSidebarWidth(Number(raw))
}

export function persistSidebarWidth(width: number): void {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(SIDEBAR_STORAGE_KEY, String(clampSidebarWidth(width)))
}

export function toggleSidebarWidth(width: number, expandedWidth = SIDEBAR_DEFAULT_WIDTH): number {
  if (isSidebarCollapsed(width)) {
    return clampSidebarWidth(Math.max(expandedWidth, SIDEBAR_COLLAPSE_WIDTH + 1))
  }
  return SIDEBAR_MIN_WIDTH
}
