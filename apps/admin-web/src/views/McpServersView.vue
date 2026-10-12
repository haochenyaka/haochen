<script setup lang="tsx">
import { onMounted, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  listMcpServers,
  createMcpServer,
  updateMcpServer,
  deleteMcpServer,
  type McpServer,
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

// 表单数据
const formSlug = ref('')
const formName = ref('')
const formUrl = ref('')
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
  formDescription.value = ''
  formActive.value = true
  modalVisible.value = true
}

function openEdit(server: McpServer) {
  editingId.value = server.id
  formSlug.value = server.slug
  formName.value = server.name
  formUrl.value = server.server_url
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
        active: formActive.value,
      }
      await updateMcpServer(editingId.value, body)
      message.success('已更新')
    } else {
      const body: CreateMcpServerBody = {
        slug: formSlug.value,
        name: formName.value,
        server_url: formUrl.value,
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

const columns = [
  { title: 'Slug', dataIndex: 'slug', key: 'slug' },
  { title: '名称', dataIndex: 'name', key: 'name' },
  { title: 'URL', dataIndex: 'server_url', key: 'server_url', ellipsis: true },
  { title: '描述', dataIndex: 'description', key: 'description', ellipsis: true },
  {
    title: '状态',
    key: 'active',
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
          <a onClick={() => openEdit(record)} style="cursor:pointer">编辑</a>
          <a-divider type="vertical" />
          <a onClick={() => handleDelete(record)} style="color:var(--error);cursor:pointer">删除</a>
        </a-space>
          )
        : null,
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
            placeholder="唯一标识，如 my-mcp-server"
            :disabled="editingId !== null"
          />
        </a-form-item>
        <a-form-item label="名称" required>
          <a-input v-model:value="formName" placeholder="显示名称" />
        </a-form-item>
        <a-form-item label="URL" required>
          <a-input v-model:value="formUrl" placeholder="https://mcp.example.com/sse" />
        </a-form-item>
        <a-form-item label="描述">
          <a-textarea v-model:value="formDescription" :rows="2" placeholder="可选描述" />
        </a-form-item>
        <a-form-item label="启用">
          <a-switch v-model:checked="formActive" checkedChildren="启用" unCheckedChildren="停用" />
        </a-form-item>
      </a-form>
    </a-modal>
  </a-spin>
</template>