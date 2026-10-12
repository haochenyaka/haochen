<script setup lang="ts">
import { useRoute } from 'vue-router'
import { EmptyState, SectionCard } from '@/components'

const route = useRoute()
</script>

<template>
  <SectionCard :title="route.meta.title ?? '模块'">
    <EmptyState description="模块骨架已就位，功能随阶段接入" />
    <p class="stage">
      计划：{{ route.meta.stage }}
    </p>
  </SectionCard>
</template>

<style scoped>
.stage {
  margin: 8px 0 0;
  color: var(--text-3);
  font-size: 13px;
  text-align: center;
}
</style>