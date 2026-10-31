import { auth as enAuth } from './locales/en-US/auth'
import { common as enCommon } from './locales/en-US/common'
import { configuration as enConfiguration } from './locales/en-US/configuration'
import { navigation as enNavigation } from './locales/en-US/navigation'
import { newsFlow as enNewsFlow } from './locales/en-US/news-flow'
import { settings as enSettings } from './locales/en-US/settings'
import { tokenUsage as enTokenUsage } from './locales/en-US/token-usage'
import { bizUi as enBizUi } from './locales/en-US/biz-ui'
import { auth as zhAuth } from './locales/zh-CN/auth'
import { common as zhCommon } from './locales/zh-CN/common'
import { configuration as zhConfiguration } from './locales/zh-CN/configuration'
import { navigation as zhNavigation } from './locales/zh-CN/navigation'
import { newsFlow as zhNewsFlow } from './locales/zh-CN/news-flow'
import { settings as zhSettings } from './locales/zh-CN/settings'
import { tokenUsage as zhTokenUsage } from './locales/zh-CN/token-usage'
import { bizUi as zhBizUi } from './locales/zh-CN/biz-ui'
import type { TranslationShape } from './resource-types'

export const zhCN = {
  common: zhCommon,
  configuration: zhConfiguration,
  auth: zhAuth,
  navigation: zhNavigation,
  newsFlow: zhNewsFlow,
  settings: zhSettings,
  tokenUsage: zhTokenUsage,
  bizUi: zhBizUi,
} as const

export const enUS = {
  common: enCommon,
  configuration: enConfiguration,
  auth: enAuth,
  navigation: enNavigation,
  newsFlow: enNewsFlow,
  settings: enSettings,
  tokenUsage: enTokenUsage,
  bizUi: enBizUi,
} as const satisfies TranslationShape<typeof zhCN>

export const resources = {
  'zh-CN': zhCN,
  'en-US': enUS,
} as const

export type NavigationItemKey = keyof typeof zhCN.navigation.items
