<script setup lang="ts">
import zhCN from 'ant-design-vue/es/locale/zh_CN'
import { themeConfig } from '@/theme/tokens'
</script>

<template>
  <a-config-provider :theme="themeConfig" :locale="zhCN">
    <router-view />
  </a-config-provider>
</template>