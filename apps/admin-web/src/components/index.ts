export { default as AppPageHeader } from './AppPageHeader.vue'
export { default as SectionCard } from './SectionCard.vue'
export { default as StatCard } from './StatCard.vue'
export { default as StatGrid } from './StatGrid.vue'
export { default as StatusTag } from './StatusTag.vue'
export { default as EmptyState } from './EmptyState.vue'
export { default as PageToolbar } from './PageToolbar.vue'
// 注意：图表组件（chart/*）刻意不从 barrel 导出，避免 ECharts 被打进
// 所有视图共享的 chunk；图表视图请从 '@/components/chart/*' 直接按需引入。