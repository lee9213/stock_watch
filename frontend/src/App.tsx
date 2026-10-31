import { Suspense, useState, useEffect, useRef } from 'react'
import { Routes, Route, useLocation, Navigate } from 'react-router-dom'
import { appApi } from '@panwatch/api/app'
import { fetchAPI, isAuthenticated } from '@panwatch/api/client'
import { GuidedTourProvider } from '@/components/onboarding/GuidedTour'
import AssistantOpenBridge from '@/components/AssistantOpenBridge'
import SelfCheckModal from '@/components/SelfCheckModal'
import { RouteErrorBoundary, RouteLoadingFallback } from '@/components/RouteBoundary'
import { routePages } from '@/router/page-loaders'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { useTranslation } from 'react-i18next'
import { NotificationProvider } from '@/components/notifications/NotificationProvider'
import AppShell from '@/layout/AppShell'

const {
  LoginPage,
  DashboardPage,
  GettingStartedPage,
  OpportunitiesPage,
  NewsFlowPage,
  StocksPage,
  AgentsPage,
  SettingsPage,
  DataSourcesPage,
  HistoryPage,
  SchedulerHistoryPage,
  LogsPage,
  TokenUsagePage,
  AnalysisDetailPage,
  PriceAlertsPage,
  PaperTradingPage,
  EvaluationsPage,
  AssistantPage,
} = routePages

function RequireAuth({ children }: { children: React.ReactNode }) {
  const [authState, setAuthState] = useState<'checking' | 'authenticated' | 'unauthenticated'>('checking')
  const location = useLocation()

  useEffect(() => {
    if (isAuthenticated()) {
      setAuthState('authenticated')
      return
    }

    setAuthState('unauthenticated')
  }, [])

  if (authState === 'checking') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <span className="w-6 h-6 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
      </div>
    )
  }

  if (authState === 'unauthenticated') {
    return <Navigate to="/login" state={{ from: location }} replace />
  }

  return <>{children}</>
}

function App() {
  const { t } = useTranslation('common')
  const location = useLocation()
  const isAssistantRoute = location.pathname === '/assistant' || location.pathname.startsWith('/assistant/')
  const fillHeight = isAssistantRoute || location.pathname === '/logs' || location.pathname === '/news-flow'
  const [version, setVersion] = useState('')
  const [selfCheckOpen, setSelfCheckOpen] = useState(false)
  const [upgradeOpen, setUpgradeOpen] = useState(false)
  const [upgradeInfo, setUpgradeInfo] = useState<{ latest: string; url: string } | null>(null)
  const checkedUpdateRef = useRef(false)

  useEffect(() => {
    appApi.version()
      .then(data => setVersion(data?.version || ''))
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (checkedUpdateRef.current) return
    if (!isAuthenticated()) return
    const current = String(version || '').trim()
    if (!current || current === 'dev') return
    checkedUpdateRef.current = true

    fetchAPI<any>('/settings/update-check')
      .then((res) => {
        const latest = String(res?.latest_version || '').trim()
        const shouldOpen = !!res?.update_available && !!latest
        if (!shouldOpen) return
        const dismissed = localStorage.getItem('panwatch_upgrade_dismissed_version') || ''
        if (dismissed === latest) return
        setUpgradeInfo({ latest, url: String(res?.release_url || 'https://github.com/sunxiao0721/PanWatch/releases') })
        setUpgradeOpen(true)
      })
      .catch(() => {})
  }, [version])

  if (location.pathname === '/login') {
    return (
      <RouteErrorBoundary>
        <Suspense fallback={<RouteLoadingFallback />}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
          </Routes>
        </Suspense>
      </RouteErrorBoundary>
    )
  }

  return (
    <RequireAuth>
    <GuidedTourProvider>
    <NotificationProvider>
    <AppShell
      fillHeight={fillHeight}
      onOpenSelfCheck={() => setSelfCheckOpen(true)}
    >
      <AssistantOpenBridge />
      <RouteErrorBoundary>
        <Suspense fallback={<RouteLoadingFallback />}>
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/getting-started" element={<GettingStartedPage />} />
            <Route path="/opportunities" element={<OpportunitiesPage />} />
            <Route path="/news-flow" element={<NewsFlowPage />} />
            <Route path="/portfolio" element={<StocksPage />} />
            <Route path="/agents" element={<AgentsPage />} />
            <Route path="/evaluations" element={<EvaluationsPage />} />
            <Route path="/history" element={<HistoryPage />} />
            <Route path="/scheduler-history" element={<SchedulerHistoryPage />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/token-usage" element={<TokenUsagePage />} />
            <Route path="/paper-trading" element={<PaperTradingPage />} />
            <Route path="/alerts" element={<PriceAlertsPage />} />
            <Route path="/assistant" element={<AssistantPage />} />
            <Route path="/assistant/:conversationId" element={<AssistantPage />} />
            <Route path="/datasources" element={<DataSourcesPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/analysis/:symbol/:date" element={<AnalysisDetailPage />} />
          </Routes>
        </Suspense>
      </RouteErrorBoundary>
    </AppShell>
    <SelfCheckModal open={selfCheckOpen} onClose={() => setSelfCheckOpen(false)} />
    <Dialog open={upgradeOpen} onOpenChange={setUpgradeOpen}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t('update.title')}</DialogTitle>
          <DialogDescription>
            {t('update.description', { current: version, latest: upgradeInfo?.latest ?? '' })}
          </DialogDescription>
        </DialogHeader>
        <div className="text-[12px] text-muted-foreground">
          {t('update.recommendation')}
        </div>
        <div className="flex items-center justify-end gap-2">
          <Button
            variant="secondary"
            onClick={() => {
              if (upgradeInfo?.latest) localStorage.setItem('panwatch_upgrade_dismissed_version', upgradeInfo.latest)
              setUpgradeOpen(false)
            }}
          >
            {t('actions.remindLater')}
          </Button>
          <Button
            onClick={() => {
              const url = upgradeInfo?.url || 'https://github.com/sunxiao0721/PanWatch/releases'
              window.open(url, '_blank', 'noopener,noreferrer')
            }}
          >
            {t('actions.upgrade')}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
    </NotificationProvider>
    </GuidedTourProvider>
    </RequireAuth>
  )
}

export default App
