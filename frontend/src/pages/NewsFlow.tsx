import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, ChevronLeft, ChevronRight, ChevronUp, ChevronsLeft, ChevronsRight, RefreshCw } from 'lucide-react'
import type { TFunction } from 'i18next'
import { useTranslation } from 'react-i18next'
import { newsFlowApi, type NewsFlowDigest, type NewsFlowItem, type NewsFlowTopic } from '@panwatch/api'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@panwatch/base-ui/components/ui/select'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

import styles from './NewsFlow.module.css'

const PAGE_SIZES = [10, 20, 50]
const DIGEST_MAX_POLL_MS = 5 * 60 * 1000
const PENDING_POLL_MS = 30 * 1000

function itemStatus(item: NewsFlowItem): string {
  return item.analysis_status || (Number(item.analyzed || 0) ? 'done' : 'pending')
}

function wallClockTime(raw?: string): string {
  if (!raw) return ''
  const matched = raw.match(/(\d{2}:\d{2})/)
  return matched ? matched[1] : raw
}

function wallClockShort(raw?: string): string {
  if (!raw) return ''
  const matched = raw.match(/(\d{2})-(\d{2}).*?(\d{2}:\d{2})/)
  return matched ? `${matched[1]}.${matched[2]} ${matched[3]}` : wallClockTime(raw)
}

/** 速览刷新时间是后台部署时区的墙上时间；时区取自接口，缺省按浏览器时区。 */
function wallClockNow(timeZone?: string): Date {
  const now = new Date()
  return timeZone ? new Date(now.toLocaleString('en-US', { timeZone })) : now
}

function isNextDay(raw?: string, timeZone?: string): boolean {
  if (!raw) return false
  const matched = raw.match(/(\d{4})-(\d{2})-(\d{2})/)
  if (!matched) return false
  const now = wallClockNow(timeZone)
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
  return `${matched[1]}-${matched[2]}-${matched[3]}` > today
}

/** 按浏览器时区显示「MM-DD HH:mm」；跨年时带年份。 */
function newsTimeLabel(iso: string): string {
  if (!iso) return ''
  const moment = new Date(iso)
  if (Number.isNaN(moment.getTime())) return iso
  const pad = (value: number) => String(value).padStart(2, '0')
  const date = `${pad(moment.getMonth() + 1)}-${pad(moment.getDate())}`
  const time = `${pad(moment.getHours())}:${pad(moment.getMinutes())}`
  const year = moment.getFullYear() === new Date().getFullYear() ? '' : `${moment.getFullYear()}-`
  return `${year}${date} ${time}`
}

function stockHref(rawCode: string): string {
  const upper = String(rawCode || '').trim().toUpperCase()
  const suffix = upper.match(/\.(SH|SZ|BJ|HK|US)$/)?.[1]
  const code = upper.replace(/\.(SH|SZ|BJ|HK|US)$/, '')
  let market = 'ab'
  let symbol = code
  if (suffix === 'HK' || /^\d{4,5}$/.test(code)) {
    market = 'hk'
    symbol = code.padStart(5, '0')
  } else if (suffix === 'US' || /^[A-Z][A-Z.-]*$/.test(code)) {
    market = 'us'
  }
  return `https://gushitong.baidu.com/stock/${market}-${encodeURIComponent(symbol)}`
}

function sourceLabel(item: NewsFlowItem, tr: TFunction<'newsFlow'>): string {
  const raw = String(item.sources || item.source || '').trim().toLowerCase()
  if (!raw) return ''
  if (raw.includes('wallstreet') || raw.includes('wscn')) return tr('sourceWallstreet')
  if (raw.includes('stock_star') || raw.includes('stockstar')) return tr('sourceStockStar')
  if (raw.includes('ths') || raw.includes('tonghuashun') || raw.includes('10jqka')) return tr('sourceThs')
  return raw
}

function relatedStocks(value: NewsFlowItem['relations_stocks'] | string | null | undefined) {
  const rows = typeof value === 'string'
    ? (() => {
        try {
          return JSON.parse(value)
        } catch {
          return []
        }
      })()
    : value
  if (!Array.isArray(rows)) return []
  return rows.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const code = String(item.code || '').trim()
    const name = String(item.name || code).trim()
    return code ? [{ code, name }] : []
  })
}

function wallClockToDelayMs(raw: string, timeZone?: string): number | null {
  const matched = raw.match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/)
  if (!matched) return null
  const now = wallClockNow(timeZone)
  const nowMs = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours(), now.getMinutes())
  const targetMs = Date.UTC(Number(matched[1]), Number(matched[2]) - 1, Number(matched[3]), Number(matched[4]), Number(matched[5]))
  return targetMs - nowMs
}

function SentimentBar({ value }: { value?: number }) {
  const { t } = useTranslation('newsFlow')
  const level = Number(value || 0)
  if (!level) return null
  const filled = Math.floor((Math.abs(level) / 10) * 10)
  const bullish = level > 0
  return (
    <div className={styles.sentiment}>
      <span className={`${styles.sentimentTag} ${bullish ? styles.sentimentBull : styles.sentimentBear}`}>
        {bullish ? t('bullish') : t('bearish')}
      </span>
      <div className={styles.strengthBar}>
        {Array.from({ length: 10 }, (_, index) => (
          <span
            key={index}
            className={`${styles.barSegment} ${index < filled ? (bullish ? styles.barFillBull : styles.barFillBear) : ''}`}
          />
        ))}
      </div>
    </div>
  )
}

function visiblePages(current: number, last: number): number[] {
  const start = Math.max(1, current - 2)
  const end = Math.min(last, start + 4)
  const from = Math.max(1, end - 4)
  return Array.from({ length: end - from + 1 }, (_, index) => from + index)
}

export default function NewsFlowPage() {
  const { t } = useTranslation('newsFlow')
  const { toast } = useToast()
  const [topics, setTopics] = useState<NewsFlowTopic[]>([])
  const [activeTopic, setActiveTopic] = useState('')
  const [items, setItems] = useState<NewsFlowItem[]>([])
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [hasMore, setHasMore] = useState(false)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [digest, setDigest] = useState<NewsFlowDigest | null>(null)
  const [digestLoading, setDigestLoading] = useState(false)
  const [digestRefreshing, setDigestRefreshing] = useState(false)
  const [nextRefreshAt, setNextRefreshAt] = useState('')
  const [digestTimezone, setDigestTimezone] = useState('')
  const [showHighlights, setShowHighlights] = useState(false)
  const [expandedReasonIds, setExpandedReasonIds] = useState<string[]>([])
  const [ingesting, setIngesting] = useState(false)
  const requestSeq = useRef(0)
  const alive = useRef(true)
  const digestTimer = useRef<ReturnType<typeof setTimeout>>()

  const allTopic = topics[0]?.key || ''
  const topicKeys = useMemo(() => new Set(topics.map((topic) => topic.key)), [topics])
  const isDigestFilter = Boolean(activeTopic && !topicKeys.has(activeTopic))
  const lastPage = Math.max(1, Math.ceil((total || (page - 1) * pageSize + items.length + (hasMore ? pageSize : 0)) / pageSize))
  const estimatedFrom = items.length ? (page - 1) * pageSize + 1 : 0
  const estimatedTo = (page - 1) * pageSize + items.length
  const nextRefreshLabel = nextRefreshAt
    ? `${isNextDay(nextRefreshAt, digestTimezone) ? `${t('tomorrow')} ` : ''}${wallClockTime(nextRefreshAt)}`
    : ''

  const loadTopics = useCallback(async () => {
    const data = await newsFlowApi.topics()
    const next = data.items || []
    setTopics(next)
    setActiveTopic((current) => current || next[0]?.key || '')
  }, [])

  const loadNews = useCallback(async (topic: string, nextPage: number, nextSize: number, silent = false) => {
    if (!topic) return
    const seq = ++requestSeq.current
    if (!silent) setLoading(true)
    try {
      const data = await newsFlowApi.search({
        keyword: topic === allTopic ? '' : topic,
        page: nextPage,
        page_size: nextSize,
      })
      if (seq !== requestSeq.current) return
      setItems(data.items || [])
      setHasMore(Boolean(data.has_more))
      setTotal(Number(data.total || 0))
    } catch {
      if (seq !== requestSeq.current || silent) return
      setItems([])
      setHasMore(false)
      setTotal(0)
      toast(t('loadFailed'), 'error')
    } finally {
      if (seq === requestSeq.current) setLoading(false)
    }
  }, [allTopic, t, toast])

  const loadDigest = useCallback(async (silent = false) => {
    if (!silent) setDigestLoading(digest === null)
    try {
      const data = await newsFlowApi.digest()
      setDigest(data.digest || null)
      setNextRefreshAt(data.next_refresh_at || '')
      setDigestTimezone(data.timezone || '')
    } catch {
      // Digest is optional; keep the news list usable.
    } finally {
      setDigestLoading(false)
      setDigestRefreshing(false)
    }
  }, [digest])

  const scheduleDigestPoll = useCallback(() => {
    if (digestTimer.current) clearTimeout(digestTimer.current)
    let delay = DIGEST_MAX_POLL_MS
    if (nextRefreshAt) {
      const diffMs = wallClockToDelayMs(nextRefreshAt, digestTimezone)
      if (diffMs !== null) {
        const untilNew = diffMs + 60 * 1000
        delay = untilNew > 0 ? Math.min(Math.max(untilNew, 60 * 1000), DIGEST_MAX_POLL_MS) : 60 * 1000
      }
    }
    digestTimer.current = setTimeout(async () => {
      if (!alive.current) return
      await loadDigest(true)
      scheduleDigestPoll()
    }, delay)
  }, [loadDigest, nextRefreshAt, digestTimezone])

  const refreshDigest = async () => {
    if (digestRefreshing) return
    const previousGeneratedAt = digest?.generated_at
    setDigestRefreshing(true)
    try {
      const data = await newsFlowApi.refreshDigest()
      setDigest(data.digest || null)
      setNextRefreshAt(data.next_refresh_at || '')
      setDigestTimezone(data.timezone || '')
      toast(t('refreshOk'), 'success')
      scheduleDigestPoll()
    } catch (error) {
      const message = error instanceof Error ? error.message : ''
      const timedOut = message.includes('超时') || message.toLowerCase().includes('timed out')
      if (timedOut) {
        try {
          const data = await newsFlowApi.digest()
          if (data.digest && data.digest.generated_at !== previousGeneratedAt) {
            setDigest(data.digest)
            setNextRefreshAt(data.next_refresh_at || '')
            setDigestTimezone(data.timezone || '')
            toast(t('refreshOk'), 'success')
            scheduleDigestPoll()
            return
          }
        } catch {
          // Fall through to the original error.
        }
      }
      toast(message || t('refreshFailed'), 'error')
    } finally {
      setDigestRefreshing(false)
    }
  }

  const ingestNow = async () => {
    if (ingesting) return
    setIngesting(true)
    try {
      const data = await newsFlowApi.ingest()
      const created = Number(data.created || 0)
      toast(created > 0 ? t('ingestOk', { count: created }) : t('ingestEmpty'), 'success')
      void loadTopics().catch(() => undefined)
      await loadNews(activeTopic, page, pageSize)
    } catch (error) {
      const message = error instanceof Error ? error.message : ''
      toast(message || t('ingestFailed'), 'error')
    } finally {
      setIngesting(false)
    }
  }

  useEffect(() => {
    alive.current = true
    void loadTopics().catch(() => toast(t('loadFailed'), 'error'))
    void loadDigest()
    scheduleDigestPoll()
    return () => {
      alive.current = false
      requestSeq.current += 1
      if (digestTimer.current) clearTimeout(digestTimer.current)
    }
  }, [])

  useEffect(() => {
    if (!activeTopic) return
    void loadNews(activeTopic, page, pageSize)
  }, [activeTopic, page, pageSize, loadNews])

  const hasPending = items.some((item) => itemStatus(item) === 'pending')
  useEffect(() => {
    if (!hasPending || !activeTopic || loading) return
    const timer = setTimeout(() => {
      void loadNews(activeTopic, page, pageSize, true)
    }, PENDING_POLL_MS)
    return () => clearTimeout(timer)
  }, [hasPending, items, activeTopic, page, pageSize, loading, loadNews])

  const selectTopic = (key: string) => {
    setActiveTopic(key)
    setPage(1)
  }

  return (
    <div className={styles.page}>
      <aside className={styles.topics}>
        <div className={styles.topicsHeader}>
          <h3 className={styles.topicsTitle}>{t('topicsTitle')}</h3>
          <p className={styles.topicsSubtitle}>{t('topicsSubtitle')}</p>
        </div>
        <div className={`${styles.topicsList} scrollbar`}>
          {topics.map((topic) => (
            <button
              key={topic.key}
              type="button"
              className={`${styles.topicItem} ${topic.key === activeTopic ? styles.topicItemActive : ''}`}
              onClick={() => selectTopic(topic.key)}
            >
              {topic.label}
            </button>
          ))}
        </div>
      </aside>

      <div className={styles.main}>
        <div className={`${styles.mainScroll} scrollbar`}>
        <section className={styles.digest}>
          <div className={styles.digestHead}>
            <span className={styles.spark}>AI</span>
            <span className={styles.digestTitle}>{t('digestTitle')}</span>
            <span className={styles.digestSub}>
              {digest?.title || t('digestFallback')}
              {digest?.generated_at ? ` (${wallClockShort(digest.generated_at)})` : ''}
            </span>
            <span className={styles.digestActions}>
              {nextRefreshLabel ? <span className={styles.nextRound}>{t('nextRound', { time: nextRefreshLabel })}</span> : null}
              {digest?.highlights?.length ? (
                <button type="button" className={`${styles.btn} ${styles.btnPlain}`} onClick={() => setShowHighlights((open) => !open)}>
                  {showHighlights ? t('collapse') : t('more')}
                  {showHighlights ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
                </button>
              ) : null}
              <button
                type="button"
                className={styles.btn}
                disabled={digestRefreshing}
                title={digestRefreshing ? t('generating') : t('refreshHint')}
                onClick={() => void refreshDigest()}
              >
                <RefreshCw className={`h-3.5 w-3.5 ${digestRefreshing ? 'animate-spin' : ''}`} />
                {digestRefreshing ? t('generating') : t('refresh')}
              </button>
            </span>
          </div>

          {digestLoading ? (
            <div className={styles.empty}>{t('digestLoading')}</div>
          ) : digest ? (
            <>
              {(digest.headlines || []).map((headline, index) => (
                <div key={`${headline.topic}-${index}`} className={styles.row}>
                  <span className={styles.topicTag}>{headline.topic}</span>
                  <div className={styles.body}>
                    <div className={styles.keywords}>
                      {(headline.keywords || []).length ? headline.keywords.map((keyword, kIndex) => (
                        <span key={`${keyword}-${kIndex}`}>
                          {kIndex > 0 ? <span className={styles.sep}>; </span> : null}
                          <button
                            type="button"
                            className={styles.keyword}
                            title={t('keywordFilter', { keyword })}
                            onClick={() => selectTopic(keyword)}
                          >
                            {keyword}
                          </button>
                        </span>
                      )) : <span className={styles.keywordEmpty}>{t('emptyKeyword')}</span>}
                    </div>
                    {headline.summary ? <div className={styles.summary}>{headline.summary}</div> : null}
                  </div>
                </div>
              ))}
              {showHighlights && digest.highlights?.length ? (
                <div className={styles.highlights}>
                  {digest.highlights.map((item) => (
                    <div key={item} className={styles.highlight}>
                      <span className={styles.dot} />
                      {item}
                    </div>
                  ))}
                </div>
              ) : null}
              <div className={styles.foot}>
                {t('basedOn', { count: digest.news_count || 0 })}
                {digest.model ? ` · ${digest.model}` : ''}
                {digest.trigger_type === 'manual' ? ` · ${t('manual')}` : ''}
              </div>
            </>
          ) : (
            <div className={styles.empty}>
              {nextRefreshLabel ? t('digestEmptyNext', { time: nextRefreshLabel }) : t('digestEmpty')}
            </div>
          )}
        </section>

        {isDigestFilter ? (
          <div className={styles.filterBar}>
            <span>{t('filterLabel')}<b>{activeTopic}</b></span>
            <button type="button" className={`${styles.btn} ${styles.btnPlain}`} onClick={() => selectTopic(allTopic)}>
              {t('clearFilter')}
            </button>
          </div>
        ) : null}

        <section className={styles.table}>
          <div className={styles.tableHead}>
            <span>{t('newsDetail')}</span>
            <button
              type="button"
              className={`${styles.btn} ${styles.btnPlain}`}
              disabled={ingesting}
              title={t('ingestHint')}
              onClick={() => void ingestNow()}
            >
              <RefreshCw className={`h-3.5 w-3.5 ${ingesting ? 'animate-spin' : ''}`} />
              {ingesting ? t('ingesting') : t('ingestNow')}
            </button>
          </div>
          <div className={styles.tableBody}>
            {loading && !items.length ? (
              <div className={styles.tableEmpty}>{t('loading')}</div>
            ) : items.length === 0 ? (
              <div className={styles.tableEmpty}>{loading ? t('loading') : t('empty')}</div>
            ) : (
              <ul>
                {items.map((item, index) => {
                  const status = itemStatus(item)
                  const failed = status === 'failed'
                  const pending = status !== 'done' && !failed
                  const unanalyzed = pending || failed
                  const stocks = relatedStocks(item.relations_stocks)
                  const source = sourceLabel(item, t)
                  const itemKey = String(item.id || `${item.news_time}-${index}`)
                  const bullishReason = String(item.bullish_level_reason || '').trim()
                  const relationReason = String(item.relation_reason || '').trim()
                  const rawText = String(item.raw_text || '').trim()
                  const showRaw = Boolean(rawText && rawText !== String(item.digest || '').trim())
                  const rawOpen = expandedReasonIds.includes(itemKey)
                  return (
                  <li
                    key={itemKey}
                    className={`${styles.newsItem} ${unanalyzed ? styles.newsItemPending : ''}`}
                  >
                    <p className={styles.digestText}>
                      {item.news_type === 'report' ? <span className={styles.reportTag}>{t('reportTag')}</span> : null}
                      {pending ? (
                        <span className={styles.pendingTag} title={t('pendingAnalysisHint')}>
                          {t('pendingAnalysis')}
                        </span>
                      ) : null}
                      {failed ? (
                        <span className={styles.failedTag} title={t('analysisFailedHint')}>
                          {t('analysisFailed')}
                        </span>
                      ) : null}
                      {source ? <span className={styles.sourceTag}>{source}</span> : null}
                      {item.digest}
                    </p>
                    <div className={styles.time}>
                      {newsTimeLabel(item.news_time)}
                      {item.url ? (
                        <a href={item.url} target="_blank" rel="noreferrer" className={styles.link}>
                          {item.url}
                        </a>
                      ) : null}
                    </div>
                    {stocks.length ? (
                      <div className={styles.meta}>
                        <strong className={styles.metaLabel}>{t('relatedStocks')}</strong>
                        <span className="ml-1 inline-flex flex-wrap gap-x-2">
                          {stocks.map((stock) => (
                            <a
                              key={stock.code}
                              href={stockHref(stock.code)}
                              target="_blank"
                              rel="noreferrer"
                              className={styles.stockLink}
                            >
                              {stock.code}({stock.name})
                            </a>
                          ))}
                        </span>
                      </div>
                    ) : pending ? (
                      <div className={`${styles.meta} ${styles.pendingHint}`}>{t('pendingAnalysisHint')}</div>
                    ) : failed ? (
                      <div className={`${styles.meta} ${styles.failedHint}`}>{t('analysisFailedHint')}</div>
                    ) : null}
                    {item.tags?.filter(Boolean).length ? (
                      <div className={`${styles.meta} mt-1.5`}>
                        <strong className={styles.metaLabel}>{t('tags')}</strong>
                        {item.tags.filter(Boolean).map((tag) => (
                          <span key={tag} className={styles.tag}>{tag}</span>
                        ))}
                      </div>
                    ) : null}
                    {!unanalyzed ? <SentimentBar value={item.bullish_level} /> : null}
                    {!unanalyzed && bullishReason ? (
                      <div className={styles.meta}>
                        <strong className={styles.metaLabel}>{t('bullishReason')}</strong>
                        {bullishReason}
                      </div>
                    ) : null}
                    {!unanalyzed && relationReason ? (
                      <div className={styles.meta}>
                        <strong className={styles.metaLabel}>{t('relationReason')}</strong>
                        {relationReason}
                      </div>
                    ) : null}
                    {!unanalyzed && showRaw ? (
                      <div className={styles.reasonBlock}>
                        <button
                          type="button"
                          className={styles.reasonToggle}
                          onClick={() => {
                            setExpandedReasonIds((current) => (
                              current.includes(itemKey)
                                ? current.filter((id) => id !== itemKey)
                                : [...current, itemKey]
                            ))
                          }}
                        >
                          {rawOpen ? t('collapse') : t('showRaw')}
                        </button>
                        {rawOpen ? (
                          <div className={styles.reasonBox}>
                            <div className={styles.reasonLine}>
                              <strong className={styles.metaLabel}>{t('rawText')}</strong>
                              {rawText}
                            </div>
                          </div>
                        ) : null}
                      </div>
                    ) : null}
                  </li>
                  )
                })}
              </ul>
            )}
          </div>
        </section>
        </div>
        <div className={styles.pager}>
          <div className={styles.pagerButtons}>
            <button type="button" className={styles.pageBtn} disabled={page <= 1 || loading} aria-label={t('firstPage')} onClick={() => setPage(1)}>
              <ChevronsLeft className="h-3.5 w-3.5" />
            </button>
            <button type="button" className={styles.pageBtn} disabled={page <= 1 || loading} aria-label={t('prevPage')} onClick={() => setPage((current) => Math.max(1, current - 1))}>
              <ChevronLeft className="h-3.5 w-3.5" />
            </button>
            {visiblePages(page, lastPage).map((pageNo) => (
              <button
                key={pageNo}
                type="button"
                className={`${styles.pageBtn} ${pageNo === page ? styles.pageBtnActive : ''}`}
                disabled={loading}
                onClick={() => setPage(pageNo)}
              >
                {pageNo}
              </button>
            ))}
            <button type="button" className={styles.pageBtn} disabled={page >= lastPage || loading} aria-label={t('nextPage')} onClick={() => setPage((current) => Math.min(lastPage, current + 1))}>
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
            <button type="button" className={styles.pageBtn} disabled={page >= lastPage || loading} aria-label={t('lastPage')} onClick={() => setPage(lastPage)}>
              <ChevronsRight className="h-3.5 w-3.5" />
            </button>
          </div>
          <span className={styles.pageReport}>{t('pageReport', { from: estimatedFrom, to: estimatedTo })}</span>
          <Select
            value={String(pageSize)}
            onValueChange={(value) => {
              setPageSize(Number(value))
              setPage(1)
            }}
          >
            <SelectTrigger className={styles.pageSizeTrigger} aria-label={t('pageSize')}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PAGE_SIZES.map((size) => (
                <SelectItem key={size} value={String(size)}>{size}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
    </div>
  )
}
