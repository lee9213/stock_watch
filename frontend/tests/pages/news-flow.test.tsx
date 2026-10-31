import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import NewsFlowPage from '@/pages/NewsFlow'

const { toast } = vi.hoisted(() => ({ toast: vi.fn() }))
const { newsFlowApi } = vi.hoisted(() => ({
  newsFlowApi: {
    topics: vi.fn(),
    search: vi.fn(),
    digest: vi.fn(),
    refreshDigest: vi.fn(),
    ingest: vi.fn(),
  },
}))

vi.mock('@panwatch/base-ui/components/ui/toast', () => ({ useToast: () => ({ toast }) }))
vi.mock('@panwatch/api', () => ({ newsFlowApi }))

describe('NewsFlowPage', () => {
  beforeEach(() => {
    toast.mockReset()
    newsFlowApi.ingest.mockReset()
    newsFlowApi.topics.mockResolvedValue({
      items: [
        { key: '全部新闻', label: '全部新闻' },
        { key: '半导体', label: '半导体' },
      ],
    })
    newsFlowApi.search.mockResolvedValue({
      items: [{
        id: '1',
        digest: '半导体设备招标扩大',
        news_time: new Date().toISOString(),
        url: 'https://example.com/news',
        news_type: 'news',
        tags: ['财联社'],
        relations_stocks: [{ code: '688981', name: '中芯国际' }],
        analyzed: 1,
        analysis_status: 'done',
        sources: 'ths',
        bullish_level: 4,
        bullish_level_reason: '招标扩大利好设备商',
        relation_reason: '点名中芯国际产业链',
      }],
      page: 1,
      page_size: 20,
      has_more: false,
      total: 1,
    })
    newsFlowApi.digest.mockResolvedValue({
      digest: {
        title: '新闻流速览',
        headlines: [{ topic: '半导体', keywords: ['半导体'], summary: '设备招标扩大' }],
        highlights: [],
        news_count: 1,
        generated_at: '2026-10-26 19:00:00',
        trigger_type: 'live',
      },
      next_refresh_at: '2026-10-26 19:05:00',
    })
  })

  it('loads topics and news, then filters by a digest keyword', async () => {
    render(<NewsFlowPage />)

    expect(await screen.findByText('话题列表')).toBeTruthy()
    expect(await screen.findByText('半导体设备招标扩大')).toBeTruthy()
    expect(screen.getByText('同花顺')).toBeTruthy()
    expect(screen.getByText('AI 推荐')).toBeTruthy()

    await userEvent.click(screen.getAllByRole('button', { name: '半导体' })[0])
    await waitFor(() => {
      expect(newsFlowApi.search).toHaveBeenCalledWith({ keyword: '半导体', page: 1, page_size: 20 })
    })
  })

  it('shows the absolute publish time instead of a relative one', async () => {
    const published = new Date(2026, 9, 30, 9, 5)
    newsFlowApi.search.mockResolvedValue({
      items: [{
        id: '6',
        digest: '定时发布的快讯',
        news_time: published.toISOString(),
        news_type: 'news',
        tags: [],
        relations_stocks: [],
        analyzed: 1,
        analysis_status: 'done',
      }],
      page: 1,
      page_size: 20,
      has_more: false,
      total: 1,
    })
    render(<NewsFlowPage />)

    expect(await screen.findByText('定时发布的快讯')).toBeTruthy()
    const year = published.getFullYear() === new Date().getFullYear() ? '' : `${published.getFullYear()}-`
    expect(screen.getByText(`${year}10-30 09:05`)).toBeTruthy()
    expect(screen.queryByText(/分钟前|小时前|天前|刚刚/)).toBeNull()
  })

  it('marks unanalyzed news with a pending badge', async () => {
    newsFlowApi.search.mockResolvedValue({
      items: [{
        id: '2',
        digest: '宏观快讯等待模型补齐',
        news_time: new Date().toISOString(),
        news_type: 'news',
        tags: [],
        relations_stocks: [],
        analyzed: 0,
        analysis_status: 'pending',
      }],
      page: 1,
      page_size: 20,
      has_more: false,
      total: 1,
    })

    render(<NewsFlowPage />)

    expect(await screen.findByText('待分析')).toBeTruthy()
    expect(screen.getByText('宏观快讯等待模型补齐')).toBeTruthy()
    expect(screen.getByText(/原文已入库/)).toBeTruthy()
  })

  it('shows a failed badge instead of pending when retries are exhausted', async () => {
    newsFlowApi.search.mockResolvedValue({
      items: [{
        id: '3',
        digest: '模型多次解析失败的快讯',
        news_time: new Date().toISOString(),
        news_type: 'news',
        tags: [],
        relations_stocks: [],
        analyzed: 0,
        analysis_status: 'failed',
        analyze_fail_count: 3,
      }],
      page: 1,
      page_size: 20,
      has_more: false,
      total: 1,
    })

    render(<NewsFlowPage />)

    expect(await screen.findByText('分析失败')).toBeTruthy()
    expect(screen.queryByText('待分析')).toBeNull()
    expect(screen.getByText(/已停止重试/)).toBeTruthy()
  })

  it('fetches news on demand and reloads the list', async () => {
    newsFlowApi.ingest.mockResolvedValue({
      status: 'ok', ths: 3, wallstreet: 0, stock_star: 0, created: 3, enqueued: 3,
    })
    render(<NewsFlowPage />)
    expect(await screen.findByText('半导体设备招标扩大')).toBeTruthy()
    const callsBefore = newsFlowApi.search.mock.calls.length

    await userEvent.click(screen.getByRole('button', { name: '立即采集' }))

    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith('新增 3 条，已送分析', 'success')
    })
    expect(newsFlowApi.ingest).toHaveBeenCalledTimes(1)
    expect(newsFlowApi.search.mock.calls.length).toBeGreaterThan(callsBefore)
  })

  it('surfaces the cooldown message when fetching too often', async () => {
    newsFlowApi.ingest.mockRejectedValue(new Error('采集过于频繁，请 42 秒后再试'))
    render(<NewsFlowPage />)
    expect(await screen.findByText('半导体设备招标扩大')).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: '立即采集' }))

    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith('采集过于频繁，请 42 秒后再试', 'error')
    })
  })

  it('polls silently while pending items are on the page', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      newsFlowApi.search.mockResolvedValue({
        items: [{
          id: '4',
          digest: '等待模型补齐的快讯',
          news_time: new Date().toISOString(),
          news_type: 'news',
          tags: [],
          relations_stocks: [],
          analyzed: 0,
          analysis_status: 'pending',
        }],
        page: 1,
        page_size: 20,
        has_more: false,
        total: 1,
      })
      render(<NewsFlowPage />)
      expect(await screen.findByText('等待模型补齐的快讯')).toBeTruthy()
      const callsBefore = newsFlowApi.search.mock.calls.length

      await vi.advanceTimersByTimeAsync(30_000)

      await waitFor(() => {
        expect(newsFlowApi.search.mock.calls.length).toBeGreaterThan(callsBefore)
      })
      expect(toast).not.toHaveBeenCalledWith('新闻数据获取失败，请稍后重试', 'error')
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not poll when every item is analyzed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      render(<NewsFlowPage />)
      expect(await screen.findByText('半导体设备招标扩大')).toBeTruthy()
      const callsBefore = newsFlowApi.search.mock.calls.length

      await vi.advanceTimersByTimeAsync(30_000)

      expect(newsFlowApi.search.mock.calls.length).toBe(callsBefore)
    } finally {
      vi.useRealTimers()
    }
  })

  it('builds market-specific stock links for A, HK and US codes', async () => {
    newsFlowApi.search.mockResolvedValue({
      items: [{
        id: '5',
        digest: '跨市场科技股异动',
        news_time: new Date().toISOString(),
        news_type: 'news',
        tags: [],
        relations_stocks: [
          { code: '688981', name: '中芯国际' },
          { code: '00700', name: '腾讯控股' },
          { code: 'AAPL', name: '苹果' },
        ],
        analyzed: 1,
        analysis_status: 'done',
      }],
      page: 1,
      page_size: 20,
      has_more: false,
      total: 1,
    })
    render(<NewsFlowPage />)

    const aShare = await screen.findByRole('link', { name: '688981(中芯国际)' })
    expect(aShare.getAttribute('href')).toBe('https://gushitong.baidu.com/stock/ab-688981')
    expect(screen.getByRole('link', { name: '00700(腾讯控股)' }).getAttribute('href'))
      .toBe('https://gushitong.baidu.com/stock/hk-00700')
    expect(screen.getByRole('link', { name: 'AAPL(苹果)' }).getAttribute('href'))
      .toBe('https://gushitong.baidu.com/stock/us-AAPL')
  })

  it('shows reasons directly and expands original text on demand', async () => {
    newsFlowApi.search.mockResolvedValue({
      items: [{
        id: '1',
        digest: '半导体设备招标扩大',
        raw_text: '中芯国际。公司公告扩大设备采购招标规模，涉及刻蚀与薄膜沉积',
        news_time: new Date().toISOString(),
        news_type: 'news',
        tags: [],
        relations_stocks: [{ code: '688981', name: '中芯国际' }],
        analyzed: 1,
        analysis_status: 'done',
        bullish_level: 4,
        bullish_level_reason: '招标扩大利好设备商',
        relation_reason: '点名中芯国际产业链',
      }],
      page: 1,
      page_size: 20,
      has_more: false,
      total: 1,
    })
    render(<NewsFlowPage />)

    expect(await screen.findByText('招标扩大利好设备商')).toBeTruthy()
    expect(screen.getByText('点名中芯国际产业链')).toBeTruthy()
    expect(screen.queryByText(/公司公告扩大设备采购招标规模/)).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: '查看原文' }))
    expect(screen.getByText(/公司公告扩大设备采购招标规模/)).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: '收起' }))
    expect(screen.queryByText(/公司公告扩大设备采购招标规模/)).toBeNull()
    expect(screen.getByText('招标扩大利好设备商')).toBeTruthy()
  })

  it('recovers a newer digest after refresh timeout', async () => {
    newsFlowApi.refreshDigest.mockRejectedValue(new Error('请求超时'))
    newsFlowApi.digest
      .mockResolvedValueOnce({
        digest: {
          title: '新闻流速览',
          headlines: [{ topic: '半导体', keywords: ['半导体'], summary: '设备招标扩大' }],
          highlights: [],
          news_count: 1,
          generated_at: '2026-10-26 19:00:00',
          trigger_type: 'live',
        },
        next_refresh_at: '2026-10-26 19:30:00',
      })
      .mockResolvedValueOnce({
        digest: {
          title: '10月26日晚间新闻流速览',
          headlines: [{ topic: '半导体', keywords: ['半导体'], summary: '设备招标扩大' }],
          highlights: [],
          news_count: 50,
          generated_at: '2026-10-26 19:48:00',
          trigger_type: 'manual',
        },
        next_refresh_at: '2026-10-26 20:30:00',
      })

    render(<NewsFlowPage />)
    expect(await screen.findByText('话题列表')).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: '刷新' }))
    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith('速览已更新', 'success')
    })
    expect(await screen.findByText(/10月26日晚间新闻流速览/)).toBeTruthy()
  })
})
