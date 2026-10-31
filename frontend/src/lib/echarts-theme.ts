/** 对齐 AlphaLom echartsTheme 的精简版，供 Token 统计等图表复用。 */

export const COLORS = {
  axisLine: '#e2e8f0',
  splitLine: '#eef1f6',
  secondary: '#64748b',
  text: '#374151',
}

export const FONT = {
  legend: 11,
  tooltip: 11,
}

export const PALETTE = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899', '#14b8a6']

export const PIE_PALETTE = [
  '#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899',
  '#14b8a6', '#ef4444', '#6366f1', '#f97316', '#0ea5e9',
  '#a855f7', '#22c55e',
]

export function axisLabel(extra: Record<string, unknown> = {}) {
  return {
    color: COLORS.secondary,
    fontSize: FONT.legend,
    ...extra,
  }
}

export function splitLine(extra: Record<string, unknown> = {}) {
  return {
    lineStyle: { color: COLORS.splitLine },
    ...extra,
  }
}

export function tooltipBase(extra: Record<string, unknown> = {}) {
  return {
    trigger: 'axis',
    backgroundColor: '#fff',
    borderColor: COLORS.axisLine,
    borderWidth: 1,
    textStyle: { color: COLORS.text, fontSize: FONT.tooltip },
    ...extra,
  }
}
