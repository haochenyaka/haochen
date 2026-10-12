/**
 * ECharts 按需注册入口 —— 仅引入使用的图表与组件，避免全量包。
 * 品牌色板统一在此定义，供各图表复用。
 */
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart, GaugeChart, RadarChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  DatasetComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  GaugeChart,
  RadarChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  DatasetComponent,
  CanvasRenderer,
])

/** 品牌图表色板：藏青 / 金 / 蓝 / 绿 / 橙 / 浅蓝 / 红。 */
export const CHART_PALETTE = [
  '#1A3A6B',
  '#C9A227',
  '#4F73A5',
  '#2E9E5B',
  '#D98A1F',
  '#7E9AC1',
  '#D03050',
]

/** 图表通用文本/网格色。 */
export const CHART_INK = {
  text: '#6B7280',
  axis: '#E5E7EB',
  split: '#F0EDE6',
}

export { echarts }