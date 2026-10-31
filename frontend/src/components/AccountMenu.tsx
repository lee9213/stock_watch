import { useState, useEffect, useRef } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { Moon, Sun, Monitor, Check, LogOut, Stethoscope, Languages, type LucideIcon } from 'lucide-react'
import { isAuthenticated, logout } from '@panwatch/api'
import type { ThemeMode } from '@/hooks/use-theme'
import { useAvatar } from '@/hooks/use-avatar'
import { useTranslation } from 'react-i18next'
import { changeLocale, normalizeLocale, type SupportedLocale } from '@/i18n'
import { useInterfaceLanguage } from '@/i18n/interface-language'
import { UserAvatar } from '@/components/UserAvatar'

export interface AccountNavItem {
  to: string
  icon: LucideIcon
  label: string
}

const THEME_OPTIONS: { value: ThemeMode; icon: LucideIcon; labelKey: 'light' | 'dark' | 'system' }[] = [
  { value: 'light', icon: Sun, labelKey: 'light' },
  { value: 'dark', icon: Moon, labelKey: 'dark' },
  { value: 'system', icon: Monitor, labelKey: 'system' },
]

const LANGUAGE_OPTIONS: { value: SupportedLocale; labelKey: 'simplifiedChinese' | 'english' }[] = [
  { value: 'zh-CN', labelKey: 'simplifiedChinese' },
  { value: 'en-US', labelKey: 'english' },
]

interface AccountMenuProps {
  /** 侧栏已展示全部路由时可不传；仅用于兼容旧入口里的补充导航。 */
  navItems?: AccountNavItem[]
  mode: ThemeMode
  onSetMode: (m: ThemeMode) => void
  /** 打开「系统自检」弹窗(状态由上层 App 托管,避免桌面/移动两个实例重复)。 */
  onOpenSelfCheck: () => void
  /** 头像尺寸:桌面 md,移动端 sm。 */
  size?: 'sm' | 'md'
  /** 侧栏底部打开时菜单向右展开，避免被侧栏裁切。 */
  menuPlacement?: 'toolbar' | 'sidebar'
}

/**
 * 头像入口 + 下拉菜单：主题、语言、自检、退出。
 * 主导航已改到左侧栏，不再把页面路由塞进头像菜单。
 */
export default function AccountMenu({
  navItems = [],
  mode,
  onSetMode,
  onOpenSelfCheck,
  size = 'md',
  menuPlacement = 'toolbar',
}: AccountMenuProps) {
  const { t, i18n: i18nInstance } = useTranslation('settings')
  const currentLocale = normalizeLocale(i18nInstance.resolvedLanguage || i18nInstance.language)
  useInterfaceLanguage()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement | null>(null)
  const location = useLocation()
  const avatar = useAvatar()
  // 仅在支持 hover 的设备(PC)启用悬停展开;触屏维持点击
  const [canHover] = useState(
    () => typeof window !== 'undefined' && window.matchMedia('(hover: hover)').matches,
  )

  // 点击外部关闭
  useEffect(() => {
    const onPointerDown = (e: PointerEvent) => {
      if (open && ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [open])

  // 路由变化时关闭
  useEffect(() => {
    setOpen(false)
  }, [location.pathname])

  const avatarSize = size === 'sm' ? 'w-6 h-6' : 'w-7 h-7'

  return (
    <div
      className="relative"
      ref={ref}
      onMouseEnter={canHover ? () => setOpen(true) : undefined}
      onMouseLeave={canHover ? () => setOpen(false) : undefined}
    >
      <button
        onClick={() => setOpen(v => !v)}
        className={`${avatarSize} rounded-full overflow-hidden bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center shadow-sm ring-1 transition-all ${
          open ? 'ring-primary/50' : 'ring-border/40 hover:ring-primary/40'
        }`}
        title={t('account.menuTitle')}
        aria-label={t('account.menuTitle')}
      >
        <UserAvatar src={avatar} alt={t('account.avatarAlt')} />
      </button>

      {open && (
        // top-full + pt-2:用透明内边距桥接头像与菜单,hover 移入不断开
        <div className={menuPlacement === 'sidebar' ? 'absolute bottom-0 left-full z-50 pl-2' : 'absolute right-0 top-full z-50 pt-2'}>
          <div className="w-56 rounded-xl border border-border/60 bg-card/95 backdrop-blur p-1.5 shadow-xl">
          {navItems.length > 0 ? (
            <>
              {navItems.map(({ to, icon: Icon, label }) => {
                const isActive = location.pathname.startsWith(to)
                return (
                  <NavLink
                    key={to}
                    to={to}
                    onClick={() => setOpen(false)}
                    className={`flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-[12px] transition-colors ${
                      isActive
                        ? 'bg-primary/10 text-primary'
                        : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
                    }`}
                  >
                    <Icon className="w-3.5 h-3.5" />
                    {label}
                  </NavLink>
                )
              })}
              <div className="my-1 h-px bg-border/50" />
            </>
          ) : null}

          {/* 主题色:亮 / 暗 / 跟随系统 */}
          <div className="px-2.5 pt-0.5 pb-1 text-[11px] text-muted-foreground">{t('account.themeTitle')}</div>
          {THEME_OPTIONS.map(({ value, icon: Icon, labelKey }) => {
            const active = mode === value
            return (
              <button
                key={value}
                onClick={() => onSetMode(value)}
                className={`flex w-full items-center gap-2.5 px-2.5 py-2 rounded-lg text-[12px] transition-colors ${
                  active
                    ? 'text-foreground bg-accent/40'
                    : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
                }`}
              >
                <Icon className="w-3.5 h-3.5" />
                {t(`account.theme.${labelKey}`)}
                {active && <Check className="w-3.5 h-3.5 ml-auto text-primary" />}
              </button>
            )
          })}

          <div className="my-1 h-px bg-border/50" />
          <div className="px-2.5 pt-0.5 pb-1 text-[11px] text-muted-foreground">{t('language.title')}</div>
          {LANGUAGE_OPTIONS.map(({ value, labelKey }) => {
            const active = currentLocale === value
            return (
              <button
                key={value}
                onClick={() => void changeLocale(value)}
                className={`flex w-full items-center gap-2.5 px-2.5 py-2 rounded-lg text-[12px] transition-colors ${
                  active
                    ? 'text-foreground bg-accent/40'
                    : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
                }`}
              >
                <Languages className="w-3.5 h-3.5" />
                {t(`language.${labelKey}`)}
                {active && <Check className="w-3.5 h-3.5 ml-auto text-primary" />}
              </button>
            )
          })}

          <div className="my-1 h-px bg-border/50" />
          {/* 系统自检:打开弹窗(逐项检查数据源/AI/通知连通性) */}
          <button
            onClick={() => {
              setOpen(false)
              onOpenSelfCheck()
            }}
            className="flex w-full items-center gap-2.5 px-2.5 py-2 rounded-lg text-[12px] text-muted-foreground hover:text-foreground hover:bg-accent/60 transition-colors"
          >
            <Stethoscope className="w-3.5 h-3.5" />
            {t('account.selfCheck')}
          </button>

          {isAuthenticated() && (
            <>
              <div className="my-1 h-px bg-border/50" />
              <button
                onClick={logout}
                className="flex w-full items-center gap-2.5 px-2.5 py-2 rounded-lg text-[12px] text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors"
              >
                <LogOut className="w-3.5 h-3.5" />
                {t('account.signOut')}
              </button>
            </>
          )}
          </div>
        </div>
      )}
    </div>
  )
}
