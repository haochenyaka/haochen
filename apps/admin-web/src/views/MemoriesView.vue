<script setup lang="tsx">
import { onMounted, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  listMemories,
  patchMemory,
  deleteMemory,
  restoreMemory,
  type MemoryItem,
} from '@/api/memory'
import { useAuthState } from '@/stores/auth'
import { EmptyState, SectionCard } from '@/components'

const { session } = useAuthState()
const isAdmin = session?.role === 'admin'

const loading = ref(true)
const memories = ref<MemoryItem[]>([])
const total = ref(0)
const categories = ref<string[]>([])
const offset = ref(0)
const limit = 20

// Filters
const filterGuest = ref('')
const filterCategory = ref('')
const filterSensitive = ref<string>('')
const filterDeleted = ref(false)

const columns = [
  { title: '客人', dataIndex: 'guest_nickname', key: 'guest_nickname', width: 100 },
  { title: '分类', dataIndex: 'category', key: 'category', width: 100 },
  {
    title: '内容',
    dataIndex: 'content',
    key: 'content',
    ellipsis: true,
    customRender: ({ text }: { text: string }) => text?.slice(0, 100) + (text?.length > 100 ? '...' : ''),
  },
  {
    title: '敏感',
    dataIndex: 'sensitive',
    key: 'sensitive',
    width: 60,
    customRender: ({ text }: { text: boolean }) =>
      text ? <a-tag color="red">是</a-tag> : <a-tag color="green">否</a-tag>,
  },
  {
    title: '来源',
    dataIndex: 'source',
    key: 'source',
    width: 100,
    customRender: ({ text }: { text: string | null }) => text || '-',
  },
  {
    title: '已删除',
    key: 'deleted',
    width: 80,
    customRender: ({ record }: { record: MemoryItem }) =>
      record.deleted_at ? <a-tag color="red">已删除</a-tag> : null,
  },
  { title: '创建时间', dataIndex: 'created_at', key: 'created_at', width: 160 },
]

onMounted(async () => {
  await loadData()
})

async function loadData() {
  loading.value = true
  try {
    const params: Record<string, string | number | boolean | undefined> = {
      offset: offset.value,
      limit,
      include_deleted: filterDeleted.value || undefined,
    }
    if (filterGuest.value) params.guest_search = filterGuest.value
    if (filterCategory.value) params.category = filterCategory.value
    if (filterSensitive.value === 'true') params.sensitive = true
    else if (filterSensitive.value === 'false') params.sensitive = false
    const resp = await listMemories(params)
    memories.value = resp.items
    total.value = resp.total
    categories.value = resp.categories
  } catch {
    message.error('加载记忆列表失败')
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  offset.value = 0
  loadData()
}

function handlePageChange(page: number) {
  offset.value = (page - 1) * limit
  loadData()
}

async function toggleSensitive(memory: MemoryItem) {
  if (!isAdmin) return
  try {
    await patchMemory(memory.id, { sensitive: !memory.sensitive })
    message.success(memory.sensitive ? '已取消敏感标记' : '已标记为敏感')
    await loadData()
  } catch {
    message.error('操作失败')
  }
}

function handleDelete(memory: MemoryItem) {
  if (!isAdmin) return
  Modal.confirm({
    title: '确认删除',
    content: `将软删除此记忆${memory.deleted_at ? '（物理删除）' : ''}。`,
    okText: memory.deleted_at ? '物理删除' : '软删除',
    okType: 'danger',
    cancelText: '取消',
    onOk: async () => {
      try {
        await deleteMemory(memory.id, !!memory.deleted_at)
        message.success(memory.deleted_at ? '已物理删除' : '已软删除')
        await loadData()
      } catch {
        message.error('删除失败')
      }
    },
  })
}

async function handleRestore(memory: MemoryItem) {
  if (!isAdmin || !memory.deleted_at) return
  try {
    await restoreMemory(memory.id)
    message.success('已恢复')
    await loadData()
  } catch {
    message.error('恢复失败')
  }
}
</script>

<template>
  <a-spin :spinning="loading">
    <SectionCard title="记忆管理">
      <template #extra>
        <a-badge :count="memories.filter(m => m.sensitive).length">
          <a-tag color="red">敏感记忆</a-tag>
        </a-badge>
      </template>

      <!-- 筛选 -->
      <a-space style="margin-bottom:16px" wrap>
        <a-input v-model:value="filterGuest" placeholder="搜索客人昵称" style="width:160px" allow-clear />
        <a-select
          v-model:value="filterCategory"
          placeholder="分类"
          style="width:130px"
          allow-clear
        >
          <a-select-option v-for="cat in categories" :key="cat" :value="cat">{{ cat }}</a-select-option>
        </a-select>
        <a-select v-model:value="filterSensitive" placeholder="敏感状态" style="width:120px" allow-clear>
          <a-select-option value="true">仅敏感</a-select-option>
          <a-select-option value="false">仅非敏感</a-select-option>
        </a-select>
        <a-checkbox v-model:checked="filterDeleted">含已删除</a-checkbox>
        <a-button type="primary" @click="handleSearch">查询</a-button>
      </a-space>

      <a-table
        :data-source="memories"
        :columns="columns"
        :pagination="{ current: offset / limit + 1, total, pageSize: limit, onChange: handlePageChange }"
        size="small"
        row-key="id"
      >
        <template #empty>
          <EmptyState description="暂无记忆数据" />
        </template>

        <!-- 操作列 -->
        <template #bodyCell="{ column, record }: { column: { key: string }, record: MemoryItem }">
          <template v-if="column.key === 'action' && isAdmin">
            <a-space>
              <a-button size="small" :type="record.sensitive ? 'default' : 'primary'" @click="toggleSensitive(record)">
                {{ record.sensitive ? '取消敏感' : '标记敏感' }}
              </a-button>
              <a-button v-if="record.deleted_at" size="small" @click="handleRestore(record)">恢复</a-button>
              <a-button size="small" danger @click="handleDelete(record)">
                {{ record.deleted_at ? '彻底删除' : '删除' }}
              </a-button>
            </a-space>
          </template>
          <template v-else-if="column.key === 'content'">
            <a-tooltip :title="record.content">
              <span>{{ record.content?.slice(0, 100) }}{{ record.content?.length > 100 ? '...' : '' }}</span>
            </a-tooltip>
          </template>
        </template>
      </a-table>
    </SectionCard>
  </a-spin>
</template>