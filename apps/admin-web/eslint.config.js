import pluginVue from 'eslint-plugin-vue'
import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'

export default defineConfigWithVueTs(
  { ignores: ['dist/**', 'node_modules/**'] },
  pluginVue.configs['flat/recommended'],
  vueTsConfigs.recommended,
  {
    // <script setup lang="tsx"> 内联 JSX（Conversations/Handoff/Mcp/Memories/AgentConfig）
    // 需要显式开启 jsx 解析，否则 vue-eslint-parser 会把整个 script 块当语法错误。
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    rules: {
      'vue/block-lang': ['error', { script: { lang: ['ts', 'tsx'] } }],
    },
  },
  {
    files: ['**/*.d.ts'],
    rules: {
      '@typescript-eslint/no-empty-object-type': 'off',
      '@typescript-eslint/ban-types': 'off',
    },
  },
)
