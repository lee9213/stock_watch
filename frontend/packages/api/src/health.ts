import { fetchAPI } from './client'

export interface SelfCheckItem {
  category: 'system' | 'datasource' | 'ai' | 'notify'
  key: string
  name: string
  status: 'ok' | 'slow' | 'fail'
  latency_ms: number
  error: string | null
  /** 中文修复提示(仅 fail 时非空)。 */
  hint: string
  hint_code?: string
  /** 例如通知"仅校验配置未真发"。 */
  note: string | null
  note_code?: string | null
  note_params?: Record<string, string | number>
  /** 二级分组(AI 类目=服务商名);其余类目为 null。 */
  group: string | null
}

export interface SelfCheckResult {
  items: SelfCheckItem[]
  summary: {
    total: number
    ok: number
    slow: number
    fail: number
  }
  notify_send: boolean
}

export interface SchedulerJobInfo {
  scheduler_name: string
  job_id: string
  running?: boolean
  next_run_time: string | null
  trigger?: string
  label?: string
  kind_label?: string
  scheduler_label?: string
  description?: string
  schedule?: string
  default_schedule?: string
  enabled?: boolean
  editable?: boolean
  kind?: 'system' | 'agent' | 'runtime' | string
  paused?: boolean
  overridden?: boolean
}

export interface SchedulerJobRun {
  id: number
  scheduler_name: string
  job_id: string
  trigger_source: string
  status: 'success' | 'failed' | 'idle' | string
  summary: Record<string, unknown>
  error: string
  duration_ms: number
  started_at: string | null
  finished_at: string | null
  created_at: string | null
}

export interface SchedulerPreview {
  schedule: string
  timezone: string
  runs: string[]
}

export const healthApi = {
  /** 系统自检(数据源/AI/通知连通性)。notifySend=true 会真实发送测试通知。 */
  selfcheck: (notifySend = false) =>
    fetchAPI<SelfCheckResult>('/health/selfcheck?notify_send=' + notifySend, { timeoutMs: 60000 }),

  /** 只取可自检项清单(不探测,秒回),用于先渲染再逐项检查。 */
  selfcheckList: () =>
    fetchAPI<{ items: Array<{ category: string; key: string; name: string; group: string | null }> }>(
      '/health/selfcheck?list=1',
    ),

  /** 只探测指定 key 的若干项(用于逐项/小并发自检)。notifySend 仅影响 notify 类目。 */
  selfcheckKeys: (keys: string[], notifySend = false) =>
    fetchAPI<SelfCheckResult>(
      '/health/selfcheck?keys=' +
        encodeURIComponent(keys.join(',')) +
        '&notify_send=' +
        notifySend,
      { timeoutMs: 30000 },
    ),

  /** 可管理的调度任务列表（系统 + Agent）。 */
  schedulerJobs: (params?: { managed?: boolean; include_agents?: boolean }) => {
    const q = new URLSearchParams()
    if (params?.managed === false) q.set('managed', 'false')
    if (params?.include_agents === false) q.set('include_agents', 'false')
    const qs = q.toString()
    return fetchAPI<{ items: SchedulerJobInfo[] }>(
      '/health/scheduler-jobs' + (qs ? `?${qs}` : ''),
    )
  },

  /** 更新系统/Agent 调度。 */
  updateSchedulerJob: (
    scheduler: string,
    jobId: string,
    body: { schedule?: string; enabled?: boolean },
  ) =>
    fetchAPI<SchedulerJobInfo>(
      `/health/scheduler-jobs/${encodeURIComponent(scheduler)}/${encodeURIComponent(jobId)}`,
      { method: 'PATCH', body: JSON.stringify(body) },
    ),

  /** 预览调度表达式。 */
  previewSchedulerSchedule: (schedule: string, count = 5) =>
    fetchAPI<SchedulerPreview>(
      `/health/scheduler-jobs/preview?schedule=${encodeURIComponent(schedule)}&count=${count}`,
    ),

  /** 系统调度执行历史。 */
  schedulerRuns: (params?: {
    scheduler?: string
    job_id?: string
    status?: string
    include_idle?: boolean
    limit?: number
    offset?: number
  }) => {
    const q = new URLSearchParams()
    if (params?.scheduler) q.set('scheduler', params.scheduler)
    if (params?.job_id) q.set('job_id', params.job_id)
    if (params?.status) q.set('status', params.status)
    if (params?.include_idle) q.set('include_idle', 'true')
    if (params?.limit != null) q.set('limit', String(params.limit))
    if (params?.offset != null) q.set('offset', String(params.offset))
    const qs = q.toString()
    return fetchAPI<{ items: SchedulerJobRun[]; limit: number; offset: number }>(
      '/health/scheduler-runs' + (qs ? `?${qs}` : ''),
    )
  },
}
