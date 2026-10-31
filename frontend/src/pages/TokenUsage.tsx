import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as echarts from 'echarts'
import { Filter, FilterX, RefreshCw } from 'lucide-react'
import {
  tokenUsageApi,
  type TokenUsageRecord,
  type TokenUsageSummary,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@panwatch/base-ui/components/ui/select'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import {
  COLORS,
  FONT,
  PALETTE,
  PIE_PALETTE,
  axisLabel,
  splitLine,
  tooltipBase,
} from '@/lib/echarts-theme'

const SCENE_LABELS: Record<string, string> = {
  news_analysis: '新闻分析',
  news_digest: '新闻速览',
  chat_stream: '对话流式',
  '(未分类)': '未分类',
}

function sceneLabel(scene?: string | null): string {
  const key = (scene || '').trim()
  if (!key) return '-'
  return SCENE_LABELS[key] || key
}

function fmtCompact(value?: number): string {
  const n = Number(value || 0)
  if (!Number.isFinite(n)) return '0'
  const abs = Math.abs(n)
  if (abs >= 1e8) return `${(n / 1e8).toFixed(2).replace(/\.?0+$/, '')}亿`
  if (abs >= 1e4) return `${(n / 1e4).toFixed(1).replace(/\.0$/, '')}万`
  return String(n)
}

function fmtThousand(value?: number): string {
  const n = Number(value || 0)
  if (!Number.isFinite(n)) return '0'
  return n.toLocaleString('en-US')
}

function fmtM(value?: number): string {
  const n = Number(value || 0)
  if (!Number.isFinite(n) || n === 0) return '0.00M'
  return `${(n / 1e6).toFixed(2).replace(/\.?0+$/, '')}M`
}

function ensureChart(
  chart: echarts.ECharts | null,
  el: HTMLDivElement | null,
): echarts.ECharts | null {
  if (!el) return null
  if (chart && chart.getDom() !== el) {
    chart.dispose()
    chart = null
  }
  if (!chart) {
    if (!el.clientWidth || !el.clientHeight) return null
    chart = echarts.init(el)
  }
  return chart
}

export default function TokenUsagePage() {
  const { toast } = useToast()
  const [summary, setSummary] = useState<TokenUsageSummary | null>(null)
  const [records, setRecords] = useState<TokenUsageRecord[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [loading, setLoading] = useState(false)
  const [model, setModel] = useState('all')
  const [scene, setScene] = useState('all')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [applied, setApplied] = useState({ model: 'all', scene: 'all', startDate: '', endDate: '' })
  const [expandedId, setExpandedId] = useState<number | null>(null)

  const trendRef = useRef<HTMLDivElement | null>(null)
  const modelPieRef = useRef<HTMLDivElement | null>(null)
  const scenePieRef = useRef<HTMLDivElement | null>(null)
  const trendChart = useRef<echarts.ECharts | null>(null)
  const modelPie = useRef<echarts.ECharts | null>(null)
  const scenePie = useRef<echarts.ECharts | null>(null)

  const query = useMemo(
    () => ({
      model: applied.model === 'all' ? undefined : applied.model,
      scene: applied.scene === 'all' ? undefined : applied.scene,
      start_date: applied.startDate || undefined,
      end_date: applied.endDate || undefined,
    }),
    [applied],
  )

  const renderCharts = useCallback((data: TokenUsageSummary | null) => {
    const daily = data?.daily || []
    const byModel = data?.by_model || []
    const byScene = (data?.by_scene || []).map((item) => ({
      ...item,
      scene: sceneLabel(item.scene),
    }))

    trendChart.current = ensureChart(trendChart.current, trendRef.current)
    if (trendChart.current) {
      if (!daily.length) {
        trendChart.current.clear()
      } else {
        trendChart.current.setOption({
          color: PALETTE.slice(0, 3),
          tooltip: tooltipBase({
            axisPointer: { type: 'cross' },
            formatter: (params: Array<{ axisValue: string; seriesName: string; marker: string; value: number }>) => {
              if (!params?.length) return ''
              const rows = params.map((p) => {
                const value = p.seriesName === '调用次数' ? fmtThousand(p.value) : fmtM(p.value)
                return `${p.marker}${p.seriesName}：<b>${value}</b>`
              }).join('<br/>')
              return `${params[0].axisValue}<br/>${rows}`
            },
          }),
          legend: {
            data: ['输入 Token', '输出 Token', '调用次数'],
            bottom: 0,
            textStyle: { fontSize: FONT.legend, color: COLORS.secondary },
          },
          grid: { left: 56, right: 56, top: 24, bottom: 44 },
          xAxis: {
            type: 'category',
            boundaryGap: false,
            data: daily.map((d) => d.date),
            axisLine: { lineStyle: { color: COLORS.axisLine } },
            axisLabel: axisLabel(),
          },
          yAxis: [
            {
              type: 'value',
              name: 'Token (M)',
              axisLabel: axisLabel({ formatter: (v: number) => fmtM(v) }),
              splitLine: splitLine(),
            },
            {
              type: 'value',
              name: '次数',
              axisLabel: axisLabel({ formatter: (v: number) => fmtCompact(v) }),
              splitLine: splitLine({ show: false }),
            },
          ],
          series: [
            {
              name: '输入 Token',
              type: 'line',
              stack: 'usage',
              symbol: 'none',
              lineStyle: { width: 0 },
              itemStyle: { color: '#3b82f6' },
              areaStyle: { color: 'rgba(59,130,246,0.35)' },
              data: daily.map((d) => d.prompt_tokens || 0),
            },
            {
              name: '输出 Token',
              type: 'line',
              stack: 'usage',
              symbol: 'none',
              lineStyle: { width: 0 },
              itemStyle: { color: '#10b981' },
              areaStyle: { color: 'rgba(16,185,129,0.35)' },
              data: daily.map((d) => d.completion_tokens || 0),
            },
            {
              name: '调用次数',
              type: 'line',
              yAxisIndex: 1,
              symbol: 'circle',
              symbolSize: 4,
              itemStyle: { color: '#f59e0b' },
              lineStyle: { color: '#f59e0b' },
              data: daily.map((d) => d.calls || 0),
            },
          ],
        }, true)
      }
    }

    modelPie.current = ensureChart(modelPie.current, modelPieRef.current)
    if (modelPie.current) {
      if (!byModel.length) {
        modelPie.current.clear()
      } else {
        const totalTokens = byModel.reduce((sum, item) => sum + (item.total_tokens || 0), 0)
        modelPie.current.setOption({
          color: PIE_PALETTE,
          tooltip: {
            trigger: 'item',
            formatter: (p: { name: string; value: number }) => {
              const pct = totalTokens ? ((p.value / totalTokens) * 100).toFixed(2) : '0'
              const matched = byModel.find((item) => item.model === p.name)
              return `<div style="font-size:12px"><b>${p.name}</b><br/>Token：${fmtM(p.value)}<br/>占比：<b>${pct}%</b><br/>调用：${fmtThousand(matched?.calls)} 次</div>`
            },
          },
          legend: {
            type: 'scroll',
            bottom: 0,
            left: 'center',
            textStyle: { fontSize: FONT.legend, color: COLORS.secondary },
          },
          series: [{
            name: '按模型',
            type: 'pie',
            radius: ['40%', '65%'],
            center: ['50%', '45%'],
            avoidLabelOverlap: true,
            itemStyle: { borderColor: '#fff', borderWidth: 2 },
            label: { show: false },
            labelLine: { show: false },
            data: byModel.map((item) => ({ name: item.model, value: item.total_tokens || 0 })),
          }],
        }, true)
      }
    }

    scenePie.current = ensureChart(scenePie.current, scenePieRef.current)
    if (scenePie.current) {
      if (!byScene.length) {
        scenePie.current.clear()
      } else {
        const totalTokens = byScene.reduce((sum, item) => sum + (item.total_tokens || 0), 0)
        scenePie.current.setOption({
          color: PIE_PALETTE,
          tooltip: {
            trigger: 'item',
            formatter: (p: { name: string; value: number }) => {
              const pct = totalTokens ? ((p.value / totalTokens) * 100).toFixed(2) : '0'
              const matched = byScene.find((item) => item.scene === p.name)
              return `<div style="font-size:12px"><b>${p.name}</b><br/>Token：${fmtM(p.value)}<br/>占比：<b>${pct}%</b><br/>调用：${fmtThousand(matched?.calls)} 次</div>`
            },
          },
          legend: {
            type: 'scroll',
            bottom: 0,
            left: 'center',
            textStyle: { fontSize: FONT.legend, color: COLORS.secondary },
          },
          series: [{
            name: '按场景',
            type: 'pie',
            radius: ['40%', '65%'],
            center: ['50%', '45%'],
            avoidLabelOverlap: true,
            itemStyle: { borderColor: '#fff', borderWidth: 2 },
            label: { show: false },
            labelLine: { show: false },
            data: byScene.map((item) => ({ name: item.scene, value: item.total_tokens || 0 })),
          }],
        }, true)
      }
    }
  }, [])

  const load = useCallback(async (nextPage = 1, nextPageSize = pageSize) => {
    setLoading(true)
    try {
      const [summaryData, recordsData] = await Promise.all([
        tokenUsageApi.summary(query),
        tokenUsageApi.records({ ...query, page: nextPage, page_size: nextPageSize }),
      ])
      setSummary(summaryData)
      setRecords(recordsData.records || [])
      setTotal(recordsData.total || 0)
      setPage(recordsData.page || nextPage)
      setPageSize(recordsData.page_size || nextPageSize)
      requestAnimationFrame(() => renderCharts(summaryData))
    } catch (error) {
      toast(error instanceof Error ? error.message : '加载失败，请稍后重试', 'error')
    } finally {
      setLoading(false)
    }
  }, [pageSize, query, renderCharts, toast])

  useEffect(() => {
    void load(1)
  }, [query]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onResize = () => {
      trendChart.current?.resize()
      modelPie.current?.resize()
      scenePie.current?.resize()
    }
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      trendChart.current?.dispose()
      modelPie.current?.dispose()
      scenePie.current?.dispose()
      trendChart.current = null
      modelPie.current = null
      scenePie.current = null
    }
  }, [])

  const onFilter = () => {
    setPage(1)
    setApplied({ model, scene, startDate, endDate })
  }

  const onReset = () => {
    setModel('all')
    setScene('all')
    setStartDate('')
    setEndDate('')
    setPage(1)
    setApplied({ model: 'all', scene: 'all', startDate: '', endDate: '' })
  }

  const lastPage = Math.max(1, Math.ceil(total / pageSize))
  const modelOptions = summary?.by_model || []
  const sceneOptions = summary?.by_scene || []

  return (
    <div className="h-full min-h-0 overflow-auto bg-muted/20 p-4">
      <div className="rounded-xl border bg-card p-4 shadow-sm">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
          <h1 className="text-xl font-semibold text-slate-900">Token 使用量统计</h1>
          <Button size="sm" variant="outline" onClick={() => void load(page)} disabled={loading}>
            <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </Button>
        </div>

        <div className="mb-6 flex flex-nowrap gap-4 overflow-x-auto">
          {[
            ['总调用次数', fmtCompact(summary?.total_calls)],
            ['总 Token', fmtM(summary?.total_tokens)],
            ['输入 Token', fmtM(summary?.total_prompt_tokens)],
            ['输出 Token', fmtM(summary?.total_completion_tokens)],
          ].map(([label, value]) => (
            <div key={String(label)} className="min-w-[140px] flex-1 rounded-lg border bg-white p-4">
              <div className="truncate text-[0.95rem] font-semibold text-slate-600">{label}</div>
              <div className="mt-1.5 truncate text-[1.7rem] font-bold leading-none tabular-nums text-slate-900">
                {value}
              </div>
            </div>
          ))}
        </div>

        <div className="mb-4 flex flex-wrap items-center gap-2">
          <Select value={model} onValueChange={setModel}>
            <SelectTrigger className="h-8 w-[160px]"><SelectValue placeholder="模型" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部模型</SelectItem>
              {modelOptions.map((item) => (
                <SelectItem key={item.model} value={item.model}>{item.model}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={scene} onValueChange={setScene}>
            <SelectTrigger className="h-8 w-[160px]"><SelectValue placeholder="场景" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部场景</SelectItem>
              {sceneOptions.map((item) => (
                <SelectItem key={item.scene} value={item.scene}>{sceneLabel(item.scene)}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Input type="date" className="h-8 w-[150px]" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          <Input type="date" className="h-8 w-[150px]" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          <Button size="sm" onClick={onFilter}><Filter className="mr-1 h-3.5 w-3.5" />筛选</Button>
          <Button size="sm" variant="outline" onClick={onReset}><FilterX className="mr-1 h-3.5 w-3.5" />重置</Button>
        </div>

        <div className="mb-2 text-sm font-semibold">每日用量走势</div>
        <div ref={trendRef} className="mb-2 h-[320px] w-full" />
        <div className="mb-6 overflow-auto rounded border">
          <table className="w-full text-left text-[11px]">
            <thead className="bg-slate-50 text-slate-600">
              <tr>
                <th className="px-2 py-2 font-medium">日期</th>
                <th className="px-2 py-2 font-medium">调用次数</th>
                <th className="px-2 py-2 font-medium">输入 Token</th>
                <th className="px-2 py-2 font-medium">输出 Token</th>
                <th className="px-2 py-2 font-medium">总计 Token</th>
              </tr>
            </thead>
            <tbody>
              {(summary?.daily || []).map((item) => (
                <tr key={item.date} className="border-t">
                  <td className="px-2 py-1.5">{item.date}</td>
                  <td className="px-2 py-1.5 tabular-nums">{fmtThousand(item.calls)}</td>
                  <td className="px-2 py-1.5 tabular-nums">{fmtM(item.prompt_tokens)}</td>
                  <td className="px-2 py-1.5 tabular-nums">{fmtM(item.completion_tokens)}</td>
                  <td className="px-2 py-1.5 tabular-nums">{fmtM(item.total_tokens)}</td>
                </tr>
              ))}
              {!summary?.daily?.length ? (
                <tr><td colSpan={5} className="px-2 py-8 text-center text-muted-foreground">暂无数据</td></tr>
              ) : null}
            </tbody>
          </table>
        </div>

        <div className="mb-6 grid gap-4 md:grid-cols-2">
          <div>
            <div className="mb-2 text-sm font-semibold">按模型（Token 占比）</div>
            <div ref={modelPieRef} className="h-[300px] w-full" />
            <div className="mt-2 overflow-auto rounded border">
              <table className="w-full text-left text-[11px]">
                <thead className="bg-slate-50 text-slate-600">
                  <tr>
                    <th className="px-2 py-2 font-medium">模型</th>
                    <th className="px-2 py-2 font-medium">调用次数</th>
                    <th className="px-2 py-2 font-medium">输入 Token</th>
                    <th className="px-2 py-2 font-medium">输出 Token</th>
                    <th className="px-2 py-2 font-medium">总计 Token</th>
                  </tr>
                </thead>
                <tbody>
                  {modelOptions.map((item) => (
                    <tr key={item.model} className="border-t">
                      <td className="px-2 py-1.5">{item.model}</td>
                      <td className="px-2 py-1.5 tabular-nums">{fmtThousand(item.calls)}</td>
                      <td className="px-2 py-1.5 tabular-nums">{fmtM(item.prompt_tokens)}</td>
                      <td className="px-2 py-1.5 tabular-nums">{fmtM(item.completion_tokens)}</td>
                      <td className="px-2 py-1.5 tabular-nums">{fmtM(item.total_tokens)}</td>
                    </tr>
                  ))}
                  {!modelOptions.length ? (
                    <tr><td colSpan={5} className="px-2 py-8 text-center text-muted-foreground">暂无数据</td></tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          </div>
          <div>
            <div className="mb-2 text-sm font-semibold">按场景（Token 占比）</div>
            <div ref={scenePieRef} className="h-[300px] w-full" />
            <div className="mt-2 overflow-auto rounded border">
              <table className="w-full text-left text-[11px]">
                <thead className="bg-slate-50 text-slate-600">
                  <tr>
                    <th className="px-2 py-2 font-medium">场景</th>
                    <th className="px-2 py-2 font-medium">调用次数</th>
                    <th className="px-2 py-2 font-medium">总计 Token</th>
                  </tr>
                </thead>
                <tbody>
                  {sceneOptions.map((item) => (
                    <tr key={item.scene} className="border-t">
                      <td className="px-2 py-1.5">{sceneLabel(item.scene)}</td>
                      <td className="px-2 py-1.5 tabular-nums">{fmtThousand(item.calls)}</td>
                      <td className="px-2 py-1.5 tabular-nums">{fmtM(item.total_tokens)}</td>
                    </tr>
                  ))}
                  {!sceneOptions.length ? (
                    <tr><td colSpan={3} className="px-2 py-8 text-center text-muted-foreground">暂无数据</td></tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div className="text-sm font-semibold">调用明细</div>
          <div className="flex items-center gap-2 text-xs">
            <Select value={String(pageSize)} onValueChange={(value) => { setPageSize(Number(value)); void load(1, Number(value)) }}>
              <SelectTrigger className="h-8 w-[108px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                {[20, 50, 100].map((size) => (
                  <SelectItem key={size} value={String(size)}>{size} 条/页</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button size="sm" variant="ghost" disabled={page <= 1 || loading} onClick={() => void load(page - 1)}>上一页</Button>
            <span>第 {page} 页</span>
            <Button size="sm" variant="ghost" disabled={page >= lastPage || loading} onClick={() => void load(page + 1)}>下一页</Button>
          </div>
        </div>
        <div className="overflow-auto rounded border">
          <table className="w-full text-left text-[11px]">
            <thead className="bg-slate-50 text-slate-600">
              <tr>
                <th className="w-8 px-2 py-2" />
                <th className="px-2 py-2 font-medium">时间</th>
                <th className="px-2 py-2 font-medium">用户ID</th>
                <th className="px-2 py-2 font-medium">场景</th>
                <th className="px-2 py-2 font-medium">平台</th>
                <th className="px-2 py-2 font-medium">模型</th>
                <th className="px-2 py-2 font-medium">输入</th>
                <th className="px-2 py-2 font-medium">输出</th>
                <th className="px-2 py-2 font-medium">总计</th>
              </tr>
            </thead>
            <tbody>
              {records.map((item) => (
                <Fragment key={item.id}>
                  <tr
                    className="cursor-pointer border-t hover:bg-slate-50"
                    onClick={() => setExpandedId(expandedId === item.id ? null : item.id)}
                  >
                    <td className="px-2 py-1.5 text-muted-foreground">{expandedId === item.id ? '▾' : '▸'}</td>
                    <td className="px-2 py-1.5 whitespace-nowrap">{item.created_at || '-'}</td>
                    <td className="px-2 py-1.5">{item.user_id ?? '-'}</td>
                    <td className="px-2 py-1.5">{sceneLabel(item.scene)}</td>
                    <td className="px-2 py-1.5">{item.platform || '-'}</td>
                    <td className="px-2 py-1.5">{item.model}</td>
                    <td className="px-2 py-1.5 tabular-nums">{fmtThousand(item.prompt_tokens)}</td>
                    <td className="px-2 py-1.5 tabular-nums">{fmtThousand(item.completion_tokens)}</td>
                    <td className="px-2 py-1.5 tabular-nums">{fmtThousand(item.total_tokens)}</td>
                  </tr>
                  {expandedId === item.id ? (
                    <tr className="border-t bg-slate-50/70">
                      <td colSpan={9} className="p-3">
                        <div className="mb-1 font-semibold">输入</div>
                        <pre className="mb-3 max-h-[300px] overflow-auto whitespace-pre-wrap break-words text-sm">{item.input_text || '-'}</pre>
                        <div className="mb-1 font-semibold">输出</div>
                        <pre className="max-h-[300px] overflow-auto whitespace-pre-wrap break-words text-sm">{item.output_text || '-'}</pre>
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
              {!records.length ? (
                <tr><td colSpan={9} className="px-2 py-8 text-center text-muted-foreground">暂无数据</td></tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
