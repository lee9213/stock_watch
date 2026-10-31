import type { TranslationShape } from '../../resource-types'
import { navigation as zhNavigation } from '../zh-CN/navigation'

export const navigation = {
  mainNav: 'Main navigation',
  collapseSidebar: 'Collapse sidebar',
  expandSidebar: 'Expand sidebar',
  resizeSidebar: 'Drag to resize sidebar',
  groups: {
    monitor: 'Monitor',
    invest: 'Invest',
    intelligence: 'Intelligence',
    system: 'System',
  },
  items: {
    gettingStarted: 'Guide center',
    home: 'Home',
    newsFlow: 'Event-driven',
    portfolio: 'Portfolio',
    opportunities: 'Opportunities',
    paperTrading: 'Paper trading',
    assistant: 'Assistant',
    alerts: 'Alerts',
    agents: 'Agents',
    evaluations: 'Evaluation',
    history: 'History',
    schedulerHistory: 'Schedulers',
    logs: 'Logs',
    tokenUsage: 'Token usage',
    dataSources: 'Data sources',
    settings: 'Settings',
  },
} as const satisfies TranslationShape<typeof zhNavigation>
