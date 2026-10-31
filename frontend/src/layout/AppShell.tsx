import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { Menu } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useTheme } from '@/hooks/use-theme'
import AccountMenu from '@/components/AccountMenu'
import AmbientBackground from '@panwatch/biz-ui/components/AmbientBackground'
import { NotificationBell } from '@/components/notifications/NotificationProvider'
import AppSidebar from './AppSidebar'
import {
  clampSidebarWidth,
  isSidebarCollapsed,
  persistSidebarWidth,
  readStoredSidebarWidth,
  SIDEBAR_COLLAPSE_WIDTH,
  SIDEBAR_DEFAULT_WIDTH,
  SIDEBAR_MIN_WIDTH,
  toggleSidebarWidth,
} from './sidebar-layout'

type AppShellProps = {
  fillHeight?: boolean
  onOpenSelfCheck: () => void
  children: ReactNode
}

export default function AppShell({
  fillHeight = false,
  onOpenSelfCheck,
  children,
}: AppShellProps) {
  const { t } = useTranslation(['common', 'navigation'])
  const { mode, setMode } = useTheme()
  const location = useLocation()
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const [sidebarWidth, setSidebarWidth] = useState(SIDEBAR_DEFAULT_WIDTH)
  const [resizing, setResizing] = useState(false)
  const [isDesktop, setIsDesktop] = useState(() =>
    typeof window !== 'undefined' ? window.matchMedia('(min-width: 768px)').matches : true,
  )
  const expandedWidthRef = useRef(SIDEBAR_DEFAULT_WIDTH)
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null)

  useEffect(() => {
    const stored = readStoredSidebarWidth()
    setSidebarWidth(stored)
    if (!isSidebarCollapsed(stored)) expandedWidthRef.current = stored
  }, [])

  useEffect(() => {
    const media = window.matchMedia('(min-width: 768px)')
    const onChange = () => setIsDesktop(media.matches)
    onChange()
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [])

  useEffect(() => {
    setMobileNavOpen(false)
  }, [location.pathname])

  useEffect(() => {
    if (!mobileNavOpen) return undefined
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMobileNavOpen(false)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [mobileNavOpen])

  useEffect(() => {
    if (!resizing) return undefined
    const previousCursor = document.body.style.cursor
    const previousUserSelect = document.body.style.userSelect
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    return () => {
      document.body.style.cursor = previousCursor
      document.body.style.userSelect = previousUserSelect
    }
  }, [resizing])

  const applyWidth = useCallback((next: number, persist = true) => {
    const width = clampSidebarWidth(next)
    setSidebarWidth(width)
    if (!isSidebarCollapsed(width)) expandedWidthRef.current = width
    if (persist) persistSidebarWidth(width)
  }, [])

  const collapsed = isDesktop && isSidebarCollapsed(sidebarWidth)

  const onToggleCollapse = useCallback(() => {
    applyWidth(toggleSidebarWidth(sidebarWidth, expandedWidthRef.current))
  }, [applyWidth, sidebarWidth])

  const onResizePointerDown = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return
    event.preventDefault()
    dragRef.current = { startX: event.clientX, startWidth: sidebarWidth }
    setResizing(true)
    event.currentTarget.setPointerCapture(event.pointerId)
  }, [sidebarWidth])

  const onResizePointerMove = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    if (!dragRef.current) return
    applyWidth(dragRef.current.startWidth + event.clientX - dragRef.current.startX, false)
  }, [applyWidth])

  const finishResize = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    if (!dragRef.current) return
    dragRef.current = null
    setResizing(false)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    setSidebarWidth((current) => {
      const snapped = current < SIDEBAR_COLLAPSE_WIDTH + 24 ? SIDEBAR_MIN_WIDTH : current
      persistSidebarWidth(snapped)
      if (!isSidebarCollapsed(snapped)) expandedWidthRef.current = snapped
      return snapped
    })
  }, [])

  return (
    <div
      className={
        fillHeight
          ? 'relative flex h-dvh overflow-hidden bg-background'
          : 'relative flex min-h-screen bg-background'
      }
    >
      <AmbientBackground />

      {mobileNavOpen ? (
        <button
          type="button"
          className="fixed inset-0 z-40 bg-black/40 md:hidden"
          aria-label={t('common:actions.close')}
          onClick={() => setMobileNavOpen(false)}
        />
      ) : null}

      <aside
        className={`fixed inset-y-0 left-0 z-50 flex h-dvh w-64 shrink-0 flex-col border-r border-border bg-card transition-transform duration-200 md:sticky md:z-30 md:h-screen md:w-auto md:translate-x-0 ${
          mobileNavOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
        style={{ ['--pw-sidebar-width' as string]: `${sidebarWidth}px` }}
        aria-label={t('navigation:mainNav')}
      >
        <div
          className={`relative flex h-full min-h-0 w-full flex-col md:w-[var(--pw-sidebar-width)] ${
            resizing ? '' : 'md:transition-[width] md:duration-200'
          }`}
        >
          <AppSidebar
            collapsed={collapsed}
            onNavigate={() => setMobileNavOpen(false)}
            onToggleCollapse={onToggleCollapse}
            onCloseMobile={() => setMobileNavOpen(false)}
            footer={
              <>
                <div className={`flex items-center ${collapsed ? 'flex-col gap-2' : 'gap-1'}`}>
                  <NotificationBell
                    mobile={!isDesktop || collapsed}
                    previewSide="right"
                    previewAlign="end"
                  />
                </div>
                <AccountMenu
                  mode={mode}
                  onSetMode={setMode}
                  onOpenSelfCheck={onOpenSelfCheck}
                  menuPlacement="sidebar"
                />
              </>
            }
          />
          <button
            type="button"
            className={`absolute inset-y-0 -right-1 hidden w-2 cursor-col-resize touch-none md:block ${
              resizing ? 'bg-primary/20' : 'hover:bg-primary/15'
            }`}
            aria-label={t('navigation:resizeSidebar')}
            title={t('navigation:resizeSidebar')}
            onPointerDown={onResizePointerDown}
            onPointerMove={onResizePointerMove}
            onPointerUp={finishResize}
            onPointerCancel={finishResize}
            onDoubleClick={onToggleCollapse}
          />
        </div>
      </aside>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <button
          type="button"
          className="fixed left-3 top-3 z-40 inline-flex h-9 w-9 items-center justify-center rounded-lg border border-border/60 bg-card/95 text-muted-foreground shadow-sm backdrop-blur hover:bg-accent hover:text-foreground md:hidden"
          aria-label={t('navigation:mainNav')}
          onClick={() => setMobileNavOpen(true)}
        >
          <Menu className="h-5 w-5" />
        </button>

        <main
          className={
            fillHeight
              ? 'flex min-h-0 flex-1 flex-col overflow-hidden px-4 pb-4 pt-14 md:px-6 md:pt-4'
              : 'w-full flex-1 px-4 pb-4 pt-14 md:px-6 md:py-6'
          }
        >
          {children}
        </main>
      </div>
    </div>
  )
}
