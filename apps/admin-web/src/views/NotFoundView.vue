<script setup lang="ts">
import { useRouter } from 'vue-router'

const router = useRouter()
</script>

<template>
  <a-result
    status="404"
    title="404"
    sub-title="页面不存在或已被移除"
  >
    <template #extra>
      <a-button
        type="primary"
        @click="router.replace('/dashboard')"
      >
        返回仪表盘
      </a-button>
    </template>
  </a-result>
</template>
