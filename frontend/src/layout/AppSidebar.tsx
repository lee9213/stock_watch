import { type ReactNode } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { PanelLeftClose, PanelLeftOpen, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { preloadRoute } from '@/router/page-loaders'
import { isNavItemActive, NAV_GROUPS } from './nav-config'

type AppSidebarProps = {
  collapsed?: boolean
  onNavigate?: () => void
  onToggleCollapse?: () => void
  onCloseMobile?: () => void
  footer?: ReactNode
}

export default function AppSidebar({
  collapsed = false,
  onNavigate,
  onToggleCollapse,
  onCloseMobile,
  footer,
}: AppSidebarProps) {
  const { t } = useTranslation(['navigation', 'common'])
  const location = useLocation()

  return (
    <div className="flex h-full min-h-0 flex-col">
      {onToggleCollapse || onCloseMobile ? (
        <div className={`flex shrink-0 items-center ${collapsed ? 'justify-center px-1 py-2' : 'justify-end px-3 py-2'}`}>
          {onToggleCollapse ? (
            <button
              type="button"
              className="hidden h-8 w-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground md:inline-flex"
              aria-label={collapsed ? t('navigation:expandSidebar') : t('navigation:collapseSidebar')}
              title={collapsed ? t('navigation:expandSidebar') : t('navigation:collapseSidebar')}
              onClick={onToggleCollapse}
            >
              {collapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
            </button>
          ) : null}
          {onCloseMobile ? (
            <button
              type="button"
              className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground md:hidden"
              aria-label={t('common:actions.close')}
              onClick={onCloseMobile}
            >
              <X className="h-4 w-4" />
            </button>
          ) : null}
        </div>
      ) : null}

      <nav className={`scrollbar min-h-0 flex-1 overflow-y-auto pb-4 ${collapsed ? 'px-1.5' : 'px-3'}`} aria-label={t('navigation:mainNav')}>
        {NAV_GROUPS.map((group) => (
          <div key={group.key} className="mb-3">
            {collapsed ? (
              <div className="mx-auto mb-1 h-px w-6 bg-border/70" />
            ) : (
              <div className="px-3 py-2 text-[11px] font-bold uppercase tracking-wide text-foreground/80">
                {t(`navigation:groups.${group.key}`)}
              </div>
            )}
            <ul className="space-y-0.5">
              {group.items.map(({ to, icon: Icon, labelKey }) => {
                const label = t(`navigation:items.${labelKey}`)
                const active = isNavItemActive(location.pathname, to)
                return (
                  <li key={to}>
                    <NavLink
                      to={to}
                      aria-label={label}
                      title={label}
                      onClick={onNavigate}
                      onMouseEnter={() => preloadRoute(to)}
                      onFocus={() => preloadRoute(to)}
                      className={`flex items-center rounded-lg text-[12px] transition-colors ${
                        collapsed ? 'justify-center px-0 py-2.5' : 'gap-2 px-3 py-2.5'
                      } ${
                        active
                          ? 'bg-primary/10 font-medium text-primary'
                          : 'text-muted-foreground hover:bg-accent hover:text-foreground'
                      }`}
                    >
                      <Icon className="h-4 w-4 shrink-0" />
                      {collapsed ? null : <span className="truncate">{label}</span>}
                    </NavLink>
                  </li>
                )
              })}
            </ul>
          </div>
        ))}
      </nav>
      {footer ? (
        <div
          className={`shrink-0 border-t border-border/60 ${
            collapsed ? 'flex flex-col items-center gap-2 px-1.5 py-3' : 'flex items-center justify-between gap-2 px-3 py-3'
          }`}
        >
          {footer}
        </div>
      ) : null}
    </div>
  )
}
