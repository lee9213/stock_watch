import {
  Activity,
  BellRing,
  Bot,
  CalendarClock,
  ClipboardCheck,
  Clock,
  Compass,
  Database,
  LayoutDashboard,
  List,
  MessageCircle,
  Newspaper,
  PieChart,
  ScrollText,
  Settings,
  Sparkles,
  type LucideIcon,
} from 'lucide-react'

import type { NavigationItemKey } from '@/i18n/resources'

export type NavGroupKey = 'monitor' | 'invest' | 'intelligence' | 'system'

export type NavItemConfig = {
  to: string
  icon: LucideIcon
  labelKey: NavigationItemKey
}

export type NavGroupConfig = {
  key: NavGroupKey
  items: NavItemConfig[]
}

/** 与 AlphaLom 侧栏一致：分组标题 + 条目，顺序按业务域排。 */
export const NAV_GROUPS: NavGroupConfig[] = [
  {
    key: 'monitor',
    items: [
      { to: '/', icon: LayoutDashboard, labelKey: 'home' },
      { to: '/news-flow', icon: Newspaper, labelKey: 'newsFlow' },
      { to: '/opportunities', icon: Sparkles, labelKey: 'opportunities' },
    ],
  },
  {
    key: 'invest',
    items: [
      { to: '/portfolio', icon: List, labelKey: 'portfolio' },
      { to: '/paper-trading', icon: Activity, labelKey: 'paperTrading' },
      { to: '/alerts', icon: BellRing, labelKey: 'alerts' },
    ],
  },
  {
    key: 'intelligence',
    items: [
      { to: '/assistant', icon: MessageCircle, labelKey: 'assistant' },
      { to: '/agents', icon: Bot, labelKey: 'agents' },
      { to: '/evaluations', icon: ClipboardCheck, labelKey: 'evaluations' },
    ],
  },
  {
    key: 'system',
    items: [
      { to: '/history', icon: Clock, labelKey: 'history' },
      { to: '/scheduler-history', icon: CalendarClock, labelKey: 'schedulerHistory' },
      { to: '/logs', icon: ScrollText, labelKey: 'logs' },
      { to: '/token-usage', icon: PieChart, labelKey: 'tokenUsage' },
      { to: '/datasources', icon: Database, labelKey: 'dataSources' },
      { to: '/settings', icon: Settings, labelKey: 'settings' },
      { to: '/getting-started', icon: Compass, labelKey: 'gettingStarted' },
    ],
  },
]

export function isNavItemActive(pathname: string, to: string): boolean {
  if (to === '/') return pathname === '/'
  return pathname === to || pathname.startsWith(`${to}/`)
}
