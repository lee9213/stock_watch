import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import AppSidebar from '@/layout/AppSidebar'
import { isNavItemActive } from '@/layout/nav-config'

describe('AppSidebar', () => {
  it('renders AlphaLom-style grouped navigation', () => {
    render(
      <MemoryRouter initialEntries={['/portfolio']}>
        <AppSidebar />
      </MemoryRouter>,
    )

    expect(screen.getByRole('navigation', { name: '主导航' })).toBeTruthy()
    expect(screen.getByText('监控')).toBeTruthy()
    expect(screen.getByText('投资')).toBeTruthy()
    expect(screen.getByText('智能')).toBeTruthy()
    expect(screen.getByText('系统')).toBeTruthy()
    expect(screen.getByRole('link', { name: '首页' }).getAttribute('href')).toBe('/')
    expect(screen.getByRole('link', { name: '事件驱动' }).getAttribute('href')).toBe('/news-flow')
    expect(screen.getByRole('link', { name: '持仓' }).getAttribute('href')).toBe('/portfolio')
    expect(screen.getByRole('link', { name: '助手' }).getAttribute('href')).toBe('/assistant')
    expect(screen.getByRole('link', { name: '设置' }).getAttribute('href')).toBe('/settings')
    expect(screen.getByRole('link', { name: '调度管理' }).getAttribute('href')).toBe('/scheduler-history')
    expect(screen.getByRole('link', { name: '日志' }).getAttribute('href')).toBe('/logs')
    expect(screen.getByRole('link', { name: 'Token 统计' }).getAttribute('href')).toBe('/token-usage')
    expect(screen.queryByText('PanWatch')).toBeNull()
  })

  it('hides group titles and labels when collapsed', () => {
    render(
      <MemoryRouter initialEntries={['/portfolio']}>
        <AppSidebar collapsed onToggleCollapse={() => undefined} />
      </MemoryRouter>,
    )

    expect(screen.queryByText('监控')).toBeNull()
    expect(screen.queryByText('持仓')).toBeNull()
    expect(screen.getByRole('link', { name: '持仓' }).getAttribute('href')).toBe('/portfolio')
    expect(screen.getByRole('button', { name: '展开侧栏' })).toBeTruthy()
  })

  it('renders account controls at the bottom', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <AppSidebar footer={<button type="button">账户与设置</button>} />
      </MemoryRouter>,
    )

    expect(screen.getByRole('button', { name: '账户与设置' })).toBeTruthy()
  })
})

describe('isNavItemActive', () => {
  it('treats only the dashboard as the root route', () => {
    expect(isNavItemActive('/', '/')).toBe(true)
    expect(isNavItemActive('/portfolio', '/')).toBe(false)
  })

  it('matches nested assistant conversations', () => {
    expect(isNavItemActive('/assistant/12', '/assistant')).toBe(true)
    expect(isNavItemActive('/alerts', '/assistant')).toBe(false)
  })
})
