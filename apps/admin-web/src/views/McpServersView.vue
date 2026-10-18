<script setup lang="tsx">
import { onMounted, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  listMcpServers,
  createMcpServer,
  updateMcpServer,
  deleteMcpServer,
  testMcpServer,
  listMcpTools,
  MCP_TRANSPORTS,
  type McpServer,
  type McpTool,
  type CreateMcpServerBody,
  type UpdateMcpServerBody,
} from '@/api/mcpServer'
import { useAuthState } from '@/stores/auth'
import { EmptyState, SectionCard } from '@/components'

const { session } = useAuthState()
const isAdmin = session?.role === 'admin'

const servers = ref<McpServer[]>([])
const loading = ref(true)
const saving = ref(false)
const modalVisible = ref(false)
const editingId = ref<number | null>(null)
const testingId = ref<number | null>(null)

// 工具清单抽屉
const toolsDrawer = ref(false)
const toolsLoading = ref(false)
const toolsServer = ref<McpServer | null>(null)
const tools = ref<McpTool[]>([])

// 表单数据
const formSlug = ref('')
const formName = ref('')
const formUrl = ref('')
const formTransport = ref('streamable_http')
const formDescription = ref('')
const formActive = ref(true)

onMounted(async () => {
  await loadServers()
})

async function loadServers() {
  loading.value = true
  try {
    servers.value = await listMcpServers()
  } catch {
    message.error('加载 MCP 服务器列表失败')
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = null
  formSlug.value = ''
  formName.value = ''
  formUrl.value = ''
  formTransport.value = 'streamable_http'
  formDescription.value = ''
  formActive.value = true
  modalVisible.value = true
}

function openEdit(server: McpServer) {
  editingId.value = server.id
  formSlug.value = server.slug
  formName.value = server.name
  formUrl.value = server.server_url
  formTransport.value = server.transport || 'streamable_http'
  formDescription.value = server.description ?? ''
  formActive.value = server.active
  modalVisible.value = true
}

async function handleSave() {
  if (!formSlug.value || !formName.value || !formUrl.value) {
    message.warning('slug、名称和 URL 为必填')
    return
  }
  saving.value = true
  try {
    if (editingId.value) {
      const body: UpdateMcpServerBody = {
        name: formName.value,
        description: formDescription.value || null,
        server_url: formUrl.value,
        transport: formTransport.value,
        active: formActive.value,
      }
      await updateMcpServer(editingId.value, body)
      message.success('已更新')
    } else {
      const body: CreateMcpServerBody = {
        slug: formSlug.value,
        name: formName.value,
        server_url: formUrl.value,
        transport: formTransport.value,
        description: formDescription.value || null,
        active: formActive.value,
      }
      await createMcpServer(body)
      message.success('已创建')
    }
    modalVisible.value = false
    await loadServers()
  } catch (err) {
    message.error((err as { message?: string }).message || '保存失败')
  } finally {
    saving.value = false
  }
}

function handleDelete(server: McpServer) {
  Modal.confirm({
    title: '确认删除',
    content: `将永久删除 MCP 服务器「${server.name}」（${server.slug}），此操作不可撤销。`,
    okText: '确认删除',
    okType: 'danger',
    cancelText: '取消',
    onOk: async () => {
      try {
        await deleteMcpServer(server.id)
        message.success('已删除')
        await loadServers()
      } catch {
        message.error('删除失败')
      }
    },
  })
}

async function toggleActive(server: McpServer) {
  try {
    await updateMcpServer(server.id, { active: !server.active })
    message.success(server.active ? '已停用' : '已启用')
    await loadServers()
  } catch {
    message.error('操作失败')
  }
}

async function handleTest(server: McpServer) {
  testingId.value = server.id
  try {
    const result = await testMcpServer(server.id)
    if (result.ok) {
      message.success(`连接成功，发现 ${result.tool_count} 个工具（${result.latency_ms}ms）`)
    } else {
      message.error(`连接失败：${result.error || result.status}`)
    }
    await loadServers()
  } catch (err) {
    message.error((err as { message?: string }).message || '测试连接失败')
  } finally {
    testingId.value = null
  }
}

async function openTools(server: McpServer) {
  toolsServer.value = server
  toolsDrawer.value = true
  toolsLoading.value = true
  try {
    const resp = await listMcpTools(server.id)
    tools.value = resp.tools
  } catch {
    tools.value = []
    message.error('加载工具清单失败')
  } finally {
    toolsLoading.value = false
  }
}

function statusTag(status: string | null): { color: string; text: string } {
  if (status === 'ok') return { color: 'success', text: '正常' }
  if (status === 'failed') return { color: 'error', text: '失败' }
  return { color: 'default', text: '未测试' }
}

const columns = [
  { title: 'Slug', dataIndex: 'slug', key: 'slug' },
  { title: '名称', dataIndex: 'name', key: 'name' },
  { title: 'URL', dataIndex: 'server_url', key: 'server_url', ellipsis: true },
  { title: '传输', dataIndex: 'transport', key: 'transport', width: 120 },
  {
    title: '探测状态',
    key: 'last_status',
    width: 120,
    customRender: ({ record }: { record: McpServer }) => {
      const tag = statusTag(record.last_status)
      return (
        <a-space direction="vertical" size={0}>
          <a-tag color={tag.color}>{tag.text}</a-tag>
          {record.last_error ? (
            <span style="color:var(--error);font-size:12px">{record.last_error.slice(0, 40)}</span>
          ) : null}
        </a-space>
      )
    },
  },
  {
    title: '状态',
    key: 'active',
    width: 90,
    customRender: ({ record }: { record: McpServer }) => (
      <a-switch
        checked={record.active}
        onChange={() => toggleActive(record)}
        disabled={!isAdmin}
        checkedChildren="启用"
        unCheckedChildren="停用"
      />
    ),
  },
  {
    title: '操作',
    key: 'action',
    customRender: ({ record }: { record: McpServer }) =>
      isAdmin
        ? (
        <a-space>
          {testingId.value === record.id ? (
            <span style="color:var(--text-secondary)">测试中…</span>
          ) : (
            <a onClick={() => handleTest(record)} style="cursor:pointer">测试连接</a>
          )}
          <a-divider type="vertical" />
          <a onClick={() => openTools(record)} style="cursor:pointer">工具</a>
          <a-divider type="vertical" />
          <a onClick={() => openEdit(record)} style="cursor:pointer">编辑</a>
          <a-divider type="vertical" />
          <a onClick={() => handleDelete(record)} style="color:var(--error);cursor:pointer">删除</a>
        </a-space>
          )
        : (
        <a-space>
          <a onClick={() => openTools(record)} style="cursor:pointer">工具</a>
        </a-space>
          ),
  },
]
</script>

<template>
  <a-spin :spinning="loading">
    <a-space direction="vertical" style="width:100%" :size="16">
      <SectionCard title="MCP 服务器管理">
        <template #extra>
          <a-button v-if="isAdmin" type="primary" @click="openCreate">新建 MCP</a-button>
        </template>

        <a-table
          :data-source="servers"
          :columns="columns"
          :pagination="{ pageSize: 20 }"
          size="small"
          row-key="id"
        >
          <template #empty>
            <EmptyState description="暂无 MCP 服务器，点击「新建 MCP」添加" />
          </template>
        </a-table>
      </SectionCard>
    </a-space>

    <!-- 创建/编辑弹窗 -->
    <a-modal
      v-model:open="modalVisible"
      :title="editingId ? '编辑 MCP 服务器' : '新建 MCP 服务器'"
      :on-ok="handleSave"
      :confirm-loading="saving"
      ok-text="保存"
      cancel-text="取消"
    >
      <a-form :label-col="{ span: 5 }" :wrapper-col="{ span: 18 }">
        <a-form-item label="Slug" required>
          <a-input
            v-model:value="formSlug"
            placeholder="唯一标识，如 lock-mcp"
            :disabled="editingId !== null"
          />
        </a-form-item>
        <a-form-item label="名称" required>
          <a-input v-model:value="formName" placeholder="显示名称" />
        </a-form-item>
        <a-form-item label="URL" required>
          <a-input v-model:value="formUrl" placeholder="http://mcp-gateway:9200/mcp" />
        </a-form-item>
        <a-form-item label="传输">
          <a-select v-model:value="formTransport">
            <a-select-option v-for="t in MCP_TRANSPORTS" :key="t.value" :value="t.value">
              {{ t.label }}
            </a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item label="描述">
          <a-textarea v-model:value="formDescription" :rows="2" placeholder="可选描述" />
        </a-form-item>
        <a-form-item label="启用">
          <a-switch v-model:checked="formActive" checkedChildren="启用" unCheckedChildren="停用" />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- 工具清单抽屉 -->
    <a-drawer
      v-model:open="toolsDrawer"
      :title="`工具清单 · ${toolsServer?.name ?? ''}`"
      width="480"
    >
      <a-spin :spinning="toolsLoading">
        <EmptyState
          v-if="!toolsLoading && tools.length === 0"
          description="暂无工具，请先「测试连接」"
        />
        <a-list v-else :data-source="tools" size="small">
          <template #renderItem="{ item }">
            <a-list-item>
              <a-list-item-meta>
                <template #title><code>{{ item.name }}</code></template>
                <template #description>{{ item.description || '（无描述）' }}</template>
              </a-list-item-meta>
            </a-list-item>
          </template>
        </a-list>
      </a-spin>
    </a-drawer>
  </a-spin>
</template>