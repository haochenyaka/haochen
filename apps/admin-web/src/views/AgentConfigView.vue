<script setup lang="tsx">
import { onMounted, ref, computed } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  listAgents,
  getAgentDetail,
  saveAgentConfig,
  deleteAgentConfig,
  listTools,
  listSkills,
  type AgentDetail,
  type Tool,
  type Skill,
  type ConfigItem,
} from '@/api/agentConfig'
import { EmptyState, SectionCard } from '@/components'

const agents = ref<AgentDetail[]>([])
const tools = ref<Tool[]>([])
const skills = ref<Skill[]>([])
const selectedSlug = ref('')
const detail = ref<AgentDetail | null>(null)
const loading = ref(true)
const saving = ref(false)
const activeTab = ref('basic')

// 编辑中的表单值
const formValues = ref<Record<string, unknown>>({})
const enabledTools = ref<string[]>([])
const enabledSkills = ref<string[]>([])

onMounted(async () => {
  try {
    const [agentsData, toolsData, skillsData] = await Promise.all([
      listAgents(),
      listTools(),
      listSkills(),
    ])
    agents.value = agentsData
    tools.value = toolsData
    skills.value = skillsData
  } catch {
    message.error('加载数据失败')
  } finally {
    loading.value = false
  }
})

function selectAgent(slug: string) {
  selectedSlug.value = slug
  loadDetail(slug)
}

async function loadDetail(slug: string) {
  loading.value = true
  try {
    const d = await getAgentDetail(slug)
    detail.value = d
    // 初始化表单值：既有配置 > 代码默认值
    formValues.value = {}
    if (d.config) {
      formValues.value = { ...d.config }
    } else {
      for (const item of d.config_items) {
        formValues.value[item.key] = item.default ?? ''
      }
    }
    enabledTools.value = d.enabled_tools ? [...d.enabled_tools] : [...d.tool_ids]
    enabledSkills.value = d.enabled_skills ? [...d.enabled_skills] : [...d.skill_ids]
  } catch {
    message.error('加载 Agent 详情失败')
  } finally {
    loading.value = false
  }
}

function renderFormControl(item: ConfigItem) {
  const value = formValues.value[item.key] ?? item.default ?? ''
  switch (item.kind) {
    case 'textarea':
      return (
        <a-textarea
          value={value as string}
          onUpdate:value={(v: string) => { formValues.value[item.key] = v }}
          rows={4}
          placeholder={item.help_text || item.label}
        />
      )
    case 'number':
      return (
        <a-input-number
          value={value as number}
          onUpdate:value={(v: number | null) => { formValues.value[item.key] = v ?? 0 }}
          style="width:100%"
        />
      )
    case 'boolean':
      return (
        <a-switch
          checked={Boolean(value)}
          onUpdate:checked={(v: boolean) => { formValues.value[item.key] = v }}
        />
      )
    case 'select':
      return (
        <a-select
          value={value as string}
          onUpdate:value={(v: string) => { formValues.value[item.key] = v }}
          style="width:100%"
          allow-clear
        >
          {item.options.map((opt) => (
            <a-select-option key={opt} value={opt}>{opt}</a-select-option>
          ))}
        </a-select>
      )
    default:
      return (
        <a-input
          value={value as string}
          onUpdate:value={(v: string) => { formValues.value[item.key] = v }}
          placeholder={item.help_text || item.label}
        />
      )
  }
}

async function handleSave() {
  if (!detail.value) return
  saving.value = true
  try {
    await saveAgentConfig(selectedSlug.value, {
      config_json: {
        context: formValues.value,
        middleware: detail.value.middleware ?? {},
      },
      enabled_tools: enabledTools.value.length > 0 ? enabledTools.value : null,
      enabled_skills: enabledSkills.value.length > 0 ? enabledSkills.value : null,
    })
    message.success('配置已保存')
    await loadDetail(selectedSlug.value)
    // 更新列表中该 agent 的配置状态
    const idx = agents.value.findIndex((a) => a.slug === selectedSlug.value)
    if (idx >= 0) {
      agents.value[idx].configured = true
    }
  } catch {
    message.error('保存失败')
  } finally {
    saving.value = false
  }
}

function handleReset() {
  if (!detail.value) return
  Modal.confirm({
    title: '确认重置',
    content: `将清除「${detail.value.name}」的所有配置覆盖，恢复代码默认值。`,
    okText: '确认重置',
    cancelText: '取消',
    onOk: async () => {
      try {
        await deleteAgentConfig(selectedSlug.value)
        message.success('已恢复默认配置')
        await loadDetail(selectedSlug.value)
        const idx = agents.value.findIndex((a) => a.slug === selectedSlug.value)
        if (idx >= 0) {
          agents.value[idx].configured = false
        }
      } catch {
        message.error('重置失败')
      }
    },
  })
}

function toggleTool(name: string) {
  const idx = enabledTools.value.indexOf(name)
  if (idx >= 0) {
    enabledTools.value.splice(idx, 1)
  } else {
    enabledTools.value.push(name)
  }
}

const groupedConfigItems = computed(() => {
  if (!detail.value) return {}
  const groups: Record<string, ConfigItem[]> = {}
  for (const item of detail.value.config_items) {
    const g = item.group || 'basic'
    if (!groups[g]) groups[g] = []
    groups[g].push(item)
  }
  return groups
})

const toolsByCategory = computed(() => {
  const groups: Record<string, Tool[]> = {}
  for (const tool of tools.value) {
    const cat = tool.category || 'general'
    if (!groups[cat]) groups[cat] = []
    groups[cat].push(tool)
  }
  return groups
})
</script>

<template>
  <a-spin :spinning="loading">
    <a-space direction="vertical" style="width:100%" :size="16">
      <!-- Agent 选择 -->
      <SectionCard title="Agent 列表">
        <a-row :gutter="[16, 16]">
          <a-col v-for="agent in agents" :key="agent.slug" :span="6">
            <a-card
              :class="['agent-card', { selected: agent.slug === selectedSlug }]"
              :bordered="true"
              hoverable
              size="small"
              @click="selectAgent(agent.slug)"
            >
              <template #extra>
                <a-tag v-if="agent.configured" color="blue">已配置</a-tag>
                <a-tag v-else>默认</a-tag>
              </template>
              <a-card-meta :title="agent.name">
                <template #description>
                  <div style="font-size:12px;color:var(--text-3)">
                    {{ agent.slug }}
                  </div>
                  <div style="font-size:12px;margin-top:4px;color:var(--text-4)">
                    {{ agent.description?.slice(0, 60) }}{{ agent.description?.length > 60 ? '...' : '' }}
                  </div>
                </template>
              </a-card-meta>
            </a-card>
          </a-col>
        </a-row>
      </SectionCard>

      <!-- 选中 Agent 的配置面板 -->
      <SectionCard v-if="detail" :title="`配置：${detail.name}`">
        <template #extra>
          <a-space>
            <a-button @click="handleSave" type="primary" :loading="saving">保存配置</a-button>
            <a-button v-if="detail.configured" @click="handleReset" danger>恢复默认</a-button>
          </a-space>
        </template>

        <a-tabs v-model:activeKey="activeTab">
          <!-- 基本配置 -->
          <a-tab-pane key="basic" tab="基本配置">
            <template v-if="Object.keys(groupedConfigItems).length === 0">
              <EmptyState description="该 Agent 无可配置项" />
            </template>
            <template v-for="(items, group) in groupedConfigItems" :key="group">
              <a-divider v-if="Object.keys(groupedConfigItems).length > 1" :orientation="'left'">
                {{ group === 'basic' ? '基本' : group === 'model' ? '模型' : group === 'prompt' ? '提示词' : group }}
              </a-divider>
              <a-form :label-col="{ span: 4 }" :wrapper-col="{ span: 18 }">
                <a-form-item
                  v-for="item in items"
                  :key="item.key"
                  :label="item.label"
                  :required="item.required"
                  :help="item.help_text || undefined"
                >
                  <component :is="renderFormControl(item)" />
                </a-form-item>
              </a-form>
            </template>
          </a-tab-pane>

          <!-- 工具管理 -->
          <a-tab-pane key="tools" tab="工具">
            <a-alert
              v-if="enabledTools.length !== detail.tool_ids.length"
              type="info"
              show-icon
              style="margin-bottom:16px"
              message="已自定义工具选择，不同于代码声明"
            />
            <template v-for="(items, cat) in toolsByCategory" :key="cat">
              <a-divider :orientation="'left'">{{ cat }}</a-divider>
              <a-checkbox-group v-model:value="enabledTools">
                <a-row :gutter="[16, 8]">
                  <a-col v-for="tool in items" :key="tool.name" :span="12">
                    <a-checkbox :value="tool.name" @change="toggleTool(tool.name)">
                      <span :class="{ dangerous: tool.dangerous }">
                        {{ tool.display_name }}
                      </span>
                      <template v-if="tool.dangerous">
                        <a-tag color="red" style="margin-left:4px" size="small">危险</a-tag>
                      </template>
                      <template v-if="tool.read_only">
                        <a-tag color="green" style="margin-left:4px" size="small">只读</a-tag>
                      </template>
                      <div style="font-size:12px;color:var(--text-4);margin-top:2px">
                        {{ tool.description?.slice(0, 80) }}
                      </div>
                    </a-checkbox>
                  </a-col>
                </a-row>
              </a-checkbox-group>
            </template>
          </a-tab-pane>

          <!-- Skill -->
          <a-tab-pane key="skills" tab="Skill">
            <template v-if="skills.length === 0">
              <EmptyState description="暂无可用 Skill" />
            </template>
            <a-checkbox-group v-model:value="enabledSkills">
              <a-row :gutter="[16, 8]">
                <a-col v-for="skill in skills" :key="skill.slug" :span="12">
                  <a-checkbox :value="skill.slug">
                    {{ skill.name }}
                    <div style="font-size:12px;color:var(--text-4);margin-top:2px">
                      {{ skill.description?.slice(0, 80) }}
                    </div>
                  </a-checkbox>
                </a-col>
              </a-row>
            </a-checkbox-group>
          </a-tab-pane>

          <!-- 配置预览 -->
          <a-tab-pane key="preview" tab="配置预览">
            <a-descriptions :column="1" size="small" bordered>
              <a-descriptions-item label="Agent">{{ detail.name }}（{{ detail.slug }}）</a-descriptions-item>
              <a-descriptions-item label="配置状态">
                <a-tag v-if="detail.configured" color="blue">已自定义</a-tag>
                <a-tag v-else>代码默认</a-tag>
              </a-descriptions-item>
              <a-descriptions-item label="代码声明工具">
                {{ detail.tool_ids.join(', ') || '无' }}
              </a-descriptions-item>
              <a-descriptions-item label="当前启用工具">
                {{ enabledTools.join(', ') || '无' }}
              </a-descriptions-item>
              <a-descriptions-item label="代码声明 Skill">
                {{ detail.skill_ids.join(', ') || '无' }}
              </a-descriptions-item>
              <a-descriptions-item label="当前启用 Skill">
                {{ enabledSkills.join(', ') || '无' }}
              </a-descriptions-item>
              <a-descriptions-item label="代码声明知识库">
                {{ detail.knowledge_base_ids.join(', ') || '无' }}
              </a-descriptions-item>
              <a-descriptions-item label="配置 JSON">
                <pre style="max-height:300px;overflow:auto;font-size:12px">{{ JSON.stringify(formValues, null, 2) || '无配置覆盖' }}</pre>
              </a-descriptions-item>
            </a-descriptions>
          </a-tab-pane>
        </a-tabs>
      </SectionCard>

      <!-- 未选中 Agent -->
      <SectionCard v-if="!detail && !loading">
        <EmptyState description="请从上方列表选择一个 Agent 进行配置" />
      </SectionCard>
    </a-space>
  </a-spin>
</template>

<style scoped>
.agent-card {
  cursor: pointer;
  transition: all 0.2s;
}
.agent-card:hover {
  border-color: var(--brand-6);
}
.agent-card.selected {
  border-color: var(--brand-6);
  box-shadow: 0 0 0 2px rgba(26, 58, 107, 0.2);
}
.dangerous {
  color: var(--error);
  font-weight: 500;
}
:deep(.ant-checkbox-wrapper) {
  display: inline-flex;
  align-items: flex-start;
  padding: 8px;
  border: 1px solid var(--divider);
  border-radius: var(--radius-sm);
  width: 100%;
}
:deep(.ant-checkbox-wrapper:hover) {
  border-color: var(--brand-6);
}
</style>