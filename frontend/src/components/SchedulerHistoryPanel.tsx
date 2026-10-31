import { useCallback, useEffect, useMemo, useState } from 'react'
import { Pause, Pencil, Play, RefreshCw } from 'lucide-react'
import {
  healthApi,
  type SchedulerJobInfo,
  type SchedulerJobRun,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@panwatch/base-ui/components/ui/dialog'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'

const SCHEDULER_FILTERS = [
  { value: '', label: '全部' },
  { value: 'system', label: '系统任务' },
  { value: 'agent', label: '智能体' },
  { value: 'news_flow', label: '新闻流' },
  { value: 'context', label: '机会/复盘' },
  { value: 'paper_trading', label: '模拟盘' },
  { value: 'price_alert', label: '价格提醒' },
]

function jobTitle(job: SchedulerJobInfo) {
  return job.label || job.job_id
}

function jobKindText(job: SchedulerJobInfo) {
  return job.kind_label || ({ system: '系统', agent: '智能体', runtime: '运行时' }[job.kind || ''] || job.kind || '系统')
}

function jobGroupText(job: SchedulerJobInfo) {
  return job.scheduler_label || job.scheduler_name
}

function statusBadge(status: string) {
  if (status === 'success') return <Badge variant="success">成功</Badge>
  if (status === 'failed') return <Badge variant="destructive">失败</Badge>
  if (status === 'idle') return <Badge variant="secondary">空跑</Badge>
  return <Badge variant="outline">{status}</Badge>
}

function formatTime(value: string | null | undefined) {
  if (!value) return '--'
  try {
    return new Date(value).toLocaleString()
  } catch {
    return value
  }
}

function summarize(run: SchedulerJobRun) {
  const s = run.summary || {}
  const parts: string[] = []
  if (s.count != null) parts.push(`机会 ${s.count}`)
  if (s.opened != null || s.closed != null) parts.push(`开${s.opened ?? 0}/平${s.closed ?? 0}`)
  if (s.analyzed != null) parts.push(`分析 ${s.analyzed}`)
  if (s.ths != null || s.wallstreet != null) {
    parts.push(`入库 ths=${s.ths ?? 0} ws=${s.wallstreet ?? 0}`)
  }
  if (s.strategy_outcomes && typeof s.strategy_outcomes === 'object') {
    const so = s.strategy_outcomes as { evaluated?: number }
    if (so.evaluated != null) parts.push(`策略评估 ${so.evaluated}`)
  }
  if (run.error) parts.push(run.error.slice(0, 80))
  return parts.join(' · ') || '—'
}

export default function SchedulerHistoryPanel() {
  const [filter, setFilter] = useState('')
  const [jobs, setJobs] = useState<SchedulerJobInfo[]>([])
  const [runs, setRuns] = useState<SchedulerJobRun[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [editJob, setEditJob] = useState<SchedulerJobInfo | null>(null)
  const [editSchedule, setEditSchedule] = useState('')
  const [previewRuns, setPreviewRuns] = useState<string[]>([])
  const [previewError, setPreviewError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const schedulerParam =
        filter && !['system', 'agent'].includes(filter) ? filter : undefined
      const [jobsRes, runsRes] = await Promise.all([
        healthApi.schedulerJobs({ managed: true, include_agents: true }),
        healthApi.schedulerRuns({
          scheduler: schedulerParam,
          limit: 40,
        }),
      ])
      setJobs(jobsRes.items || [])
      setRuns(runsRes.items || [])
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [filter])

  useEffect(() => {
    void load()
  }, [load])

  const filteredJobs = useMemo(() => {
    if (!filter) return jobs
    if (filter === 'system') return jobs.filter((j) => j.kind === 'system' || j.kind === 'runtime')
    if (filter === 'agent') return jobs.filter((j) => j.kind === 'agent')
    return jobs.filter((j) => j.scheduler_name === filter)
  }, [jobs, filter])

  const jobLabelByKey = useMemo(() => {
    const map = new Map<string, string>()
    for (const job of jobs) {
      map.set(`${job.scheduler_name}/${job.job_id}`, jobTitle(job))
    }
    return map
  }, [jobs])

  const openEdit = (job: SchedulerJobInfo) => {
    setEditJob(job)
    setEditSchedule(job.schedule || job.default_schedule || '')
    setPreviewRuns([])
    setPreviewError('')
  }

  const runPreview = async () => {
    setPreviewError('')
    try {
      const res = await healthApi.previewSchedulerSchedule(editSchedule.trim(), 5)
      setPreviewRuns(res.runs || [])
    } catch (e) {
      setPreviewRuns([])
      setPreviewError(e instanceof Error ? e.message : '预览失败')
    }
  }

  const saveEdit = async () => {
    if (!editJob) return
    setSaving(true)
    setError('')
    try {
      await healthApi.updateSchedulerJob(editJob.scheduler_name, editJob.job_id, {
        schedule: editSchedule.trim(),
      })
      setEditJob(null)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const toggleEnabled = async (job: SchedulerJobInfo) => {
    if (!job.editable) return
    setSaving(true)
    setError('')
    try {
      await healthApi.updateSchedulerJob(job.scheduler_name, job.job_id, {
        enabled: !(job.enabled !== false),
      })
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : '更新失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-[20px] md:text-[22px] font-bold text-foreground tracking-tight">调度管理</h1>
          <p className="text-[12px] md:text-[13px] text-muted-foreground mt-0.5 md:mt-1">
            管理系统任务与智能体的触发时间 / 启停；支持 cron 或 interval:5m
          </p>
        </div>
        <Button
          variant="secondary"
          size="sm"
          className="h-8 shrink-0 self-start sm:self-auto"
          onClick={() => void load()}
          disabled={loading}
        >
          {loading ? (
            <span className="w-3.5 h-3.5 border-2 border-current/30 border-t-current rounded-full animate-spin" />
          ) : (
            <>
              <RefreshCw className="h-3.5 w-3.5 mr-1.5" />
              <span className="text-[12px]">刷新</span>
            </>
          )}
        </Button>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {SCHEDULER_FILTERS.map((f) => {
          const active = filter === f.value
          return (
            <button
              key={f.value || 'all'}
              type="button"
              onClick={() => setFilter(f.value)}
              className={`h-8 px-3 rounded-full text-[12px] border transition-colors ${
                active
                  ? 'bg-primary text-primary-foreground border-primary'
                  : 'bg-background text-muted-foreground border-border hover:bg-accent hover:text-foreground'
              }`}
            >
              {f.label}
            </button>
          )
        })}
      </div>

      <div className="card p-4 space-y-3">
        {error && <div className="text-[12px] text-destructive">{error}</div>}

        <div className="space-y-2 max-h-[28rem] overflow-auto">
          {filteredJobs.length === 0 && !loading && (
            <div className="text-[12px] text-muted-foreground py-6 text-center">暂无调度任务</div>
          )}
          {filteredJobs.map((job) => {
            const enabled = job.enabled !== false
            return (
              <div
                key={`${job.scheduler_name}:${job.job_id}`}
                className="rounded-xl border border-border/60 bg-accent/10 px-3 py-2.5 text-[12px]"
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[13px] font-semibold text-foreground">{jobTitle(job)}</span>
                      <Badge variant="outline">{jobKindText(job)}</Badge>
                      <Badge variant="secondary">{jobGroupText(job)}</Badge>
                      {enabled ? (
                        <Badge variant="success">启用</Badge>
                      ) : (
                        <Badge variant="secondary">已暂停</Badge>
                      )}
                      {job.overridden && <Badge variant="outline">已自定义</Badge>}
                    </div>
                    <div className="text-muted-foreground">
                      调度 <span className="font-mono text-foreground">{job.schedule || '--'}</span>
                      {' · '}下次 {formatTime(job.next_run_time)}
                    </div>
                    {job.description && (
                      <div className="text-[11px] text-muted-foreground">{job.description}</div>
                    )}
                  </div>
                  {job.editable && (
                    <div className="flex items-center gap-1.5 shrink-0">
                      <Button
                        size="sm"
                        variant="secondary"
                        className="h-8 px-2.5"
                        disabled={saving}
                        onClick={() => void toggleEnabled(job)}
                        title={enabled ? '暂停' : '启用'}
                      >
                        {enabled ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                      </Button>
                      <Button
                        size="sm"
                        variant="secondary"
                        className="h-8 px-2.5"
                        onClick={() => openEdit(job)}
                      >
                        <Pencil className="h-3.5 w-3.5 mr-1" />
                        <span className="text-[12px]">编辑</span>
                      </Button>
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>

      <div className="card p-4 space-y-3">
        <div className="text-[15px] font-semibold">执行历史</div>
        <div className="text-[11px] text-muted-foreground">空跑默认不展示</div>
        <div className="space-y-2 max-h-72 overflow-auto">
          {runs.length === 0 && !loading && (
            <div className="text-[12px] text-muted-foreground py-4 text-center">暂无执行记录</div>
          )}
          {runs.map((run) => {
            const key = `${run.scheduler_name}/${run.job_id}`
            const title = jobLabelByKey.get(key) || run.job_id
            return (
              <div
                key={run.id}
                className="flex flex-wrap items-start justify-between gap-2 rounded-md border px-3 py-2 text-[12px]"
              >
                <div className="min-w-0 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    {statusBadge(run.status)}
                    <span className="font-medium text-foreground">{title}</span>
                    <span className="text-muted-foreground">{run.duration_ms}ms</span>
                  </div>
                  <div className="text-muted-foreground break-all">{summarize(run)}</div>
                </div>
                <div className="text-[11px] text-muted-foreground whitespace-nowrap">
                  {formatTime(run.finished_at || run.created_at)}
                </div>
              </div>
            )
          })}
        </div>
      </div>

      <Dialog open={!!editJob} onOpenChange={(open) => !open && setEditJob(null)}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>编辑调度 · {editJob ? jobTitle(editJob) : ''}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 py-2">
            <div className="text-[11px] text-muted-foreground">
              {editJob ? `${jobGroupText(editJob)} · ${jobKindText(editJob)}` : ''}
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="schedule-expr">调度表达式</Label>
              <Input
                id="schedule-expr"
                value={editSchedule}
                onChange={(e) => setEditSchedule(e.target.value)}
                placeholder="如 30 15 * * 1-5 或 interval:5m"
              />
              <div className="text-[11px] text-muted-foreground">
                cron：分 时 日 月 周（周 1-5=周一到周五）；间隔：interval:30s / interval:5m / interval:1h
                {editJob?.default_schedule ? `；默认 ${editJob.default_schedule}` : ''}
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Button type="button" size="sm" variant="outline" onClick={() => void runPreview()}>
                预览下次触发
              </Button>
            </div>
            {previewError && <div className="text-[12px] text-destructive">{previewError}</div>}
            {previewRuns.length > 0 && (
              <ul className="text-[12px] text-muted-foreground space-y-0.5 list-disc pl-4">
                {previewRuns.map((t) => (
                  <li key={t}>{formatTime(t)}</li>
                ))}
              </ul>
            )}
          </div>
          <div className="mt-4 flex justify-end gap-2">
            <Button variant="outline" onClick={() => setEditJob(null)} disabled={saving}>
              取消
            </Button>
            <Button onClick={() => void saveEdit()} disabled={saving || !editSchedule.trim()}>
              {saving ? '保存中…' : '保存'}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
