import { fetchAPI } from './client'

export type NewsFlowTopic = {
  key: string
  label: string
}

export type NewsFlowStock = {
  code: string
  name: string
}

export type NewsFlowItem = {
  id: string
  /** 分析后为模型摘要；未分析时即原文 */
  digest: string
  /** 来源原文（分析不改写；旧数据可能为空） */
  raw_text?: string
  content?: string
  news_time: string
  url?: string
  news_type?: string
  tags?: string[]
  relations_stocks?: NewsFlowStock[]
  bullish_level?: number
  bullish_level_reason?: string
  relation_reason?: string
  /** 0 = 待分析，1 = 已分析 */
  analyzed?: number
  /** pending | done | failed（失败次数用尽，不再重试） */
  analysis_status?: 'pending' | 'done' | 'failed' | string
  analyze_fail_count?: number
  source?: string
  sources?: string
  importance?: number
}

export type NewsFlowPage = {
  items: NewsFlowItem[]
  page: number
  page_size: number
  has_more: boolean
  total: number
}

export type NewsFlowHeadline = {
  topic: string
  keywords: string[]
  summary: string
}

export type NewsFlowDigest = {
  title?: string
  headlines?: NewsFlowHeadline[]
  highlights?: string[]
  news_count?: number
  generated_at?: string
  model?: string | null
  trigger_type?: string
}

export type NewsFlowIngestResult = {
  status: string
  ths: number
  wallstreet: number
  stock_star: number
  created: number
  enqueued: number
}

export type NewsFlowDigestResponse = {
  digest: NewsFlowDigest | null
  next_refresh_at?: string
  timezone?: string
}

export const newsFlowApi = {
  topics: () => fetchAPI<{ items: NewsFlowTopic[] }>('/news/flow/topics'),
  search: (params: { keyword?: string; page?: number; page_size?: number }) => {
    const query = new URLSearchParams()
    if (params.keyword) query.set('keyword', params.keyword)
    if (params.page) query.set('page', String(params.page))
    if (params.page_size) query.set('page_size', String(params.page_size))
    const suffix = query.toString()
    return fetchAPI<NewsFlowPage>(suffix ? `/news/flow?${suffix}` : '/news/flow')
  },
  digest: () => fetchAPI<NewsFlowDigestResponse>('/news/flow/digest'),
  refreshDigest: () => fetchAPI<NewsFlowDigestResponse>('/news/flow/digest/refresh', {
    method: 'POST',
    body: '{}',
    timeoutMs: 180000,
  }),
  ingest: () => fetchAPI<NewsFlowIngestResult>('/news/flow/ingest', {
    method: 'POST',
    body: '{}',
    timeoutMs: 120000,
  }),
}
