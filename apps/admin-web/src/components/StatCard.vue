<script setup lang="ts">
/**
 * KPI 指标卡 —— 顶部金色渐变条 + 标题 + 数值 + 单位/趋势。
 * 替代 Dashboard/DataView/Quality 里散落的 a-statistic 与手写渐变卡。
 */
withDefaults(
  defineProps<{
    title: string
    value: string | number
    suffix?: string
    /** 趋势文案，如 "较昨日 +3" */
    trend?: string
    /** 趋势方向，决定颜色 */
    trendType?: 'up' | 'down' | 'neutral'
    /** 标题左侧图标（AntD 图标组件） */
    icon?: unknown
  }>(),
  { suffix: undefined, trend: undefined, trendType: 'neutral', icon: undefined },
)
</script>

<template>
  <div class="stat-card">
    <div class="stat-card__bar" />
    <div class="stat-card__body">
      <div class="stat-card__head">
        <span class="stat-card__title">{{ title }}</span>
        <component :is="icon" v-if="icon" class="stat-card__icon" />
      </div>
      <div class="stat-card__value">
        {{ value }}
        <span v-if="suffix" class="stat-card__suffix">{{ suffix }}</span>
      </div>
      <div v-if="trend" class="stat-card__trend" :class="`is-${trendType}`">{{ trend }}</div>
    </div>
  </div>
</template>

<style scoped>
.stat-card {
  position: relative;
  overflow: hidden;
  background: var(--bg-container);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-card);
  transition: box-shadow 0.2s, transform 0.2s;
}

.stat-card:hover {
  box-shadow: var(--shadow-md);
  transform: translateY(-1px);
}

.stat-card__bar {
  height: 3px;
  background: var(--gradient-gold);
}

.stat-card__body {
  padding: 18px 20px 20px;
}

.stat-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.stat-card__title {
  color: var(--text-3);
  font-size: 13px;
}

.stat-card__icon {
  color: var(--brand-4);
  font-size: 18px;
}

.stat-card__value {
  margin-top: 10px;
  color: var(--brand-6);
  font-size: 28px;
  font-weight: 700;
  line-height: 1.2;
}

.stat-card__suffix {
  margin-left: 4px;
  color: var(--text-3);
  font-size: 13px;
  font-weight: 400;
}

.stat-card__trend {
  margin-top: 8px;
  font-size: 12px;
}

.stat-card__trend.is-up {
  color: var(--success);
}

.stat-card__trend.is-down {
  color: var(--error);
}

.stat-card__trend.is-neutral {
  color: var(--text-4);
}
</style>