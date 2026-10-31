import { fetchAPI } from './client'

export type TokenUsageModelBucket = {
  model: string
  calls: number
  total_tokens: number
  prompt_tokens: number
  completion_tokens: number
}

export type TokenUsageSceneBucket = {
  scene: string
  calls: number
  total_tokens: number
}

export type TokenUsageDailyBucket = {
  date: string
  calls: number
  total_tokens: number
  prompt_tokens: number
  completion_tokens: number
}

export type TokenUsageSummary = {
  total_calls: number
  total_prompt_tokens: number
  total_completion_tokens: number
  total_tokens: number
  by_model: TokenUsageModelBucket[]
  by_scene: TokenUsageSceneBucket[]
  daily: TokenUsageDailyBucket[]
}

export type TokenUsageRecord = {
  id: number
  user_id?: number | null
  scene?: string | null
  platform?: string | null
  model: string
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  input_text?: string | null
  output_text?: string | null
  created_at?: string | null
}

export type TokenUsageRecordsPage = {
  total: number
  page: number
  page_size: number
  records: TokenUsageRecord[]
}

export type TokenUsageQuery = {
  model?: string
  scene?: string
  start_date?: string
  end_date?: string
  page?: number
  page_size?: number
}

function toQuery(params: TokenUsageQuery = {}): string {
  const search = new URLSearchParams()
  if (params.model) search.set('model', params.model)
  if (params.scene) search.set('scene', params.scene)
  if (params.start_date) search.set('start_date', params.start_date)
  if (params.end_date) search.set('end_date', params.end_date)
  if (params.page) search.set('page', String(params.page))
  if (params.page_size) search.set('page_size', String(params.page_size))
  const suffix = search.toString()
  return suffix ? `?${suffix}` : ''
}

export const tokenUsageApi = {
  summary: (params: TokenUsageQuery = {}) =>
    fetchAPI<TokenUsageSummary>(`/token-usage/summary${toQuery(params)}`),
  records: (params: TokenUsageQuery = {}) =>
    fetchAPI<TokenUsageRecordsPage>(`/token-usage/records${toQuery(params)}`),
}
