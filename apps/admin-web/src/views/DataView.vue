<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  ACCOUNT_ROLES,
  ACCOUNT_ROLE_LABELS,
  ACCOUNT_STATUSES,
  ACCOUNT_STATUS_LABELS,
  ROOM_STATUSES,
  ROOM_STATUS_LABELS,
  STAFF_POSITIONS,
  STAFF_POSITION_LABELS,
  createAccount,
  createRoom,
  createRoomType,
  createStaff,
  getHotelConfig,
  listAccounts,
  listRoomTypes,
  listRooms,
  listStaff,
  patchAccount,
  patchRoom,
  patchRoomType,
  patchStaff,
  updateHotelConfig,
  type Account,
  type HotelConfig,
  type Room,
  type RoomType,
  type Staff,
} from '@/api/admin'
import { useAuthState } from '@/stores/auth'
import { PageToolbar, SectionCard, StatCard, StatGrid } from '@/components'

const { session } = useAuthState()
const isAdmin = computed(() => session?.role === 'admin')
const canReadAccounts = isAdmin

const activeTab = ref('roomTypes')
const loading = reactive({ roomTypes: false, rooms: false, staff: false, accounts: false, hotelConfig: false })
const saving = ref(false)

const roomTypes = ref<RoomType[]>([])
const rooms = ref<Room[]>([])
const staff = ref<Staff[]>([])
const accounts = ref<Account[]>([])
const hotelConfig = ref<HotelConfig>({ hotel_name: '', address: null, phone: null, brand: null, description: null })

const roomTypeOptions = computed(() =>
  roomTypes.value.map((item) => ({ value: item.id, label: `${item.code} · ${item.name}` })),
)
const roomTypeMap = computed(() => new Map(roomTypes.value.map((item) => [item.id, item])))
const roomTypeStats = computed(() => {
  const all = roomTypes.value
  return {
    total: all.length,
    active: all.filter((r) => r.active).length,
    minPrice: all.length ? Math.min(...all.map((r) => r.base_price_fen)) : 0,
    maxPrice: all.length ? Math.max(...all.map((r) => r.base_price_fen)) : 0,
  }
})
const accountOptions = computed(() =>
  accounts.value.map((item) => ({ value: item.id, label: item.username })),
)

// Search
const searchText = reactive({ roomTypes: '', rooms: '', staff: '', accounts: '' })

const modal = reactive<{ open: boolean; mode: 'create' | 'edit'; targetId: string }>({
  open: false,
  mode: 'create',
  targetId: '',
})

// Common hotel amenities
const AMENITY_OPTIONS = [
  'WiFi', '空调', '电视', '冰箱', '吹风机', '热水壶',
  '保险箱', '迷你吧', '浴缸', '淋浴', '智能马桶',
  '书桌', '沙发', '阳台', '厨房', '洗衣机',
]

// 设施标签配色：收敛为品牌藏青/金/语义色板循环
const AMENITY_PALETTE = ['#1A3A6B', '#C9A227', '#4F73A5', '#2E9E5B', '#D98A1F', '#7E9AC1']
const AMENITY_COLORS: Record<string, string> = Object.fromEntries(
  AMENITY_OPTIONS.map((name, index) => [name, AMENITY_PALETTE[index % AMENITY_PALETTE.length]]),
)

const roomTypeForm = reactive({
  code: '',
  name: '',
  base_price_fen: 0,
  capacity: 2,
  bed_type: 'king',
  description: '',
  amenities: [] as string[],
  active: true,
})
const roomForm = reactive({
  room_no: '',
  room_type_id: '',
  floor: 1,
  status: 'available',
  lock_device_no: '',
})
const staffForm = reactive({
  staff_no: '',
  name: '',
  position: 'housekeeping' as string,
  user_id: null as string | null,
  status: 'active',
})
const accountForm = reactive({
  username: '',
  password: '',
  display_name: '',
  role: 'operator',
  status: 'active',
})

const priceFen = (value: number): string => `¥${(value / 100).toFixed(2)}`
const roomTypeName = (id: string): string => {
  const item = roomTypeMap.value.get(id)
  return item ? `${item.code} · ${item.name}` : id
}

// ── Load ──

async function loadRoomTypes(): Promise<void> {
  loading.roomTypes = true
  try {
    roomTypes.value = await listRoomTypes(true, searchText.roomTypes || undefined)
  } catch (error) {
    message.error((error as { message?: string }).message || '房型加载失败')
  } finally {
    loading.roomTypes = false
  }
}

async function loadRooms(): Promise<void> {
  loading.rooms = true
  try {
    rooms.value = await listRooms({ search: searchText.rooms || undefined })
  } catch (error) {
    message.error((error as { message?: string }).message || '房间加载失败')
  } finally {
    loading.rooms = false
  }
}

async function loadStaff(): Promise<void> {
  loading.staff = true
  try {
    staff.value = await listStaff({ search: searchText.staff || undefined })
  } catch (error) {
    message.error((error as { message?: string }).message || '员工加载失败')
  } finally {
    loading.staff = false
  }
}

async function loadAccounts(): Promise<void> {
  if (!canReadAccounts.value) return
  loading.accounts = true
  try {
    accounts.value = await listAccounts({ search: searchText.accounts || undefined })
  } catch (error) {
    message.error((error as { message?: string }).message || '账号加载失败')
  } finally {
    loading.accounts = false
  }
}

async function loadHotelConfig(): Promise<void> {
  loading.hotelConfig = true
  try {
    hotelConfig.value = await getHotelConfig()
  } catch (error) {
    message.error((error as { message?: string }).message || '酒店配置加载失败')
  } finally {
    loading.hotelConfig = false
  }
}

function onTabChange(key: string) {
  if (key === 'hotelConfig' && !hotelConfig.value.hotel_name) {
    loadHotelConfig()
  }
}

// ── Search ──

let searchTimer: ReturnType<typeof setTimeout> | null = null
function onSearch(kind: 'roomTypes' | 'rooms' | 'staff' | 'accounts') {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    if (kind === 'roomTypes') loadRoomTypes()
    else if (kind === 'rooms') loadRooms()
    else if (kind === 'staff') loadStaff()
    else loadAccounts()
  }, 300)
}

// ── Modal ──

function openCreate(kind: 'roomTypes' | 'rooms' | 'staff' | 'accounts'): void {
  modal.mode = 'create'
  modal.targetId = ''
  if (kind === 'roomTypes') {
    Object.assign(roomTypeForm, {
      code: '', name: '', base_price_fen: 0, capacity: 2, bed_type: 'king',
      description: '', amenities: [], active: true,
    })
  } else if (kind === 'rooms') {
    Object.assign(roomForm, {
      room_no: '', room_type_id: roomTypeOptions.value[0]?.value ?? '',
      floor: 1, status: 'available', lock_device_no: '',
    })
  } else if (kind === 'staff') {
    Object.assign(staffForm, {
      staff_no: '', name: '', position: 'housekeeping', user_id: null, status: 'active',
    })
  } else {
    Object.assign(accountForm, {
      username: '', password: '', display_name: '', role: 'operator', status: 'active',
    })
  }
  modal.open = true
}

function openEdit(kind: 'roomTypes' | 'rooms' | 'staff' | 'accounts', id: string): void {
  modal.mode = 'edit'
  modal.targetId = id
  if (kind === 'roomTypes') {
    const row = roomTypes.value.find((item) => item.id === id)
    if (row) {
      Object.assign(roomTypeForm, {
        code: row.code, name: row.name, base_price_fen: Math.round(row.base_price_fen / 100),
        capacity: row.capacity, bed_type: row.bed_type, description: row.description ?? '',
        amenities: row.amenities ?? [], active: row.active,
      })
    }
  } else if (kind === 'rooms') {
    const row = rooms.value.find((item) => item.id === id)
    if (row) {
      Object.assign(roomForm, {
        room_no: row.room_no, room_type_id: row.room_type_id, floor: row.floor,
        status: row.status, lock_device_no: row.lock_device_no ?? '',
      })
    }
  } else if (kind === 'staff') {
    const row = staff.value.find((item) => item.id === id)
    if (row) {
      Object.assign(staffForm, {
        staff_no: row.staff_no, name: row.name, position: row.position,
        user_id: row.user_id, status: row.status,
      })
    }
  } else {
    const row = accounts.value.find((item) => item.id === id)
    if (row) {
      Object.assign(accountForm, {
        username: row.username, password: '', display_name: row.display_name ?? '',
        role: row.role, status: row.status,
      })
    }
  }
  modal.open = true
}

async function submit(): Promise<void> {
  saving.value = true
  try {
    if (activeTab.value === 'roomTypes') {
      const body = {
        name: roomTypeForm.name, base_price_fen: Math.round(roomTypeForm.base_price_fen * 100),
        capacity: roomTypeForm.capacity, bed_type: roomTypeForm.bed_type,
        description: roomTypeForm.description || null,
        amenities: roomTypeForm.amenities.length ? roomTypeForm.amenities : null,
        active: roomTypeForm.active,
      }
      if (modal.mode === 'create') {
        await createRoomType({ code: roomTypeForm.code, ...body })
      } else {
        await patchRoomType(modal.targetId, body)
      }
      await loadRoomTypes()
    } else if (activeTab.value === 'rooms') {
      const body = {
        room_type_id: roomForm.room_type_id, floor: roomForm.floor,
        status: roomForm.status, lock_device_no: roomForm.lock_device_no || null,
      }
      if (modal.mode === 'create') {
        await createRoom({ room_no: roomForm.room_no, ...body })
      } else {
        await patchRoom(modal.targetId, body)
      }
      await loadRooms()
    } else if (activeTab.value === 'staff') {
      const body = {
        name: staffForm.name, position: staffForm.position,
        user_id: staffForm.user_id, status: staffForm.status,
      }
      if (modal.mode === 'create') {
        await createStaff({ staff_no: staffForm.staff_no, ...body })
      } else {
        await patchStaff(modal.targetId, body)
      }
      await loadStaff()
    } else {
      if (modal.mode === 'create') {
        if (!accountForm.password || accountForm.password.length < 8) {
          throw new Error('密码长度至少 8 位')
        }
        await createAccount({
          username: accountForm.username, password: accountForm.password,
          display_name: accountForm.display_name || null, role: accountForm.role,
        })
      } else {
        if (accountForm.password && accountForm.password.length < 8) {
          throw new Error('密码长度至少 8 位')
        }
        await patchAccount(modal.targetId, {
          display_name: accountForm.display_name || null, role: accountForm.role,
          status: accountForm.status, ...(accountForm.password ? { password: accountForm.password } : {}),
        })
      }
      await loadAccounts()
    }
    message.success(modal.mode === 'create' ? '已新增' : '已保存')
    modal.open = false
  } catch (error) {
    message.error((error as { message?: string }).message || '保存失败')
  } finally {
    saving.value = false
  }
}

function toggleRoomTypeActive(row: RoomType): void {
  Modal.confirm({
    title: row.active ? '停用房型' : '启用房型',
    content: `确认${row.active ? '停用' : '启用'}「${row.name}」？停用后仍保留历史引用。`,
    okText: '确认', cancelText: '取消',
    onOk: async () => {
      await patchRoomType(row.id, { active: !row.active })
      await loadRoomTypes()
      message.success('已更新')
    },
  })
}

function toggleAccountStatus(row: Account): void {
  Modal.confirm({
    title: row.status === 'active' ? '禁用账号' : '启用账号',
    content: `确认${row.status === 'active' ? '禁用' : '启用'}账号「${row.username}」？禁用会立即失效其全部会话。`,
    okText: '确认', cancelText: '取消',
    onOk: async () => {
      await patchAccount(row.id, { status: row.status === 'active' ? 'disabled' : 'active' })
      await loadAccounts()
      message.success('已更新')
    },
  })
}

// ── Room status quick-switch ──

async function quickSwitchRoomStatus(room: Room, newStatus: string) {
  Modal.confirm({
    title: '变更房态',
    content: `将「${room.room_no}」从「${ROOM_STATUS_LABELS[room.status] || room.status}」改为「${ROOM_STATUS_LABELS[newStatus] || newStatus}」？`,
    okText: '确认', cancelText: '取消',
    onOk: async () => {
      try {
        await patchRoom(room.id, { status: newStatus })
        room.status = newStatus
        message.success('房态已更新')
      } catch (error) {
        message.error((error as { message?: string }).message || '更新失败')
      }
    },
  })
}

// ── Hotel config ──

async function saveHotelConfig() {
  saving.value = true
  try {
    const cfg = await updateHotelConfig({
      hotel_name: hotelConfig.value.hotel_name,
      address: hotelConfig.value.address || null,
      phone: hotelConfig.value.phone || null,
      brand: hotelConfig.value.brand || null,
      description: hotelConfig.value.description || null,
    })
    Object.assign(hotelConfig.value, cfg)
    message.success('酒店配置已保存')
  } catch (error) {
    message.error((error as { message?: string }).message || '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadRoomTypes(), loadRooms(), loadStaff()])
  await loadAccounts()
})
</script>

<template>
  <SectionCard>
    <template #title>酒店基础数据</template>
    <template #extra>
      <a-tag color="blue">{{ isAdmin ? '可读写' : '只读（店长）' }}</a-tag>
    </template>

    <a-tabs v-model:active-key="activeTab" @change="onTabChange">
      <!-- ────── 房型 ────── -->
      <a-tab-pane key="roomTypes" tab="房型">
        <!-- 统计摘要 -->
        <div style="margin-bottom:16px">
          <StatGrid :column="4" :gutter="12">
            <StatCard title="房型总数" :value="roomTypeStats.total" />
            <StatCard title="启用中" :value="roomTypeStats.active" suffix="/ 共" />
            <StatCard title="最低价" :value="`¥${(roomTypeStats.minPrice / 100).toFixed(0)}`" />
            <StatCard title="最高价" :value="`¥${(roomTypeStats.maxPrice / 100).toFixed(0)}`" />
          </StatGrid>
        </div>
        <PageToolbar>
          <a-input-search
            v-model:value="searchText.roomTypes"
            placeholder="搜索编码/名称"
            style="width:240px"
            @input="onSearch('roomTypes')"
            allow-clear
          />
          <a-button v-if="isAdmin" type="primary" @click="openCreate('roomTypes')">新增房型</a-button>
        </PageToolbar>
        <a-table :data-source="roomTypes" :loading="loading.roomTypes" row-key="id" size="middle" :pagination="{ pageSize: 10 }">
          <a-table-column title="编码" data-index="code" :width="100" />
          <a-table-column title="名称" data-index="name" :width="140" />
          <a-table-column title="门市价" :width="110">
            <template #default="{ record }">{{ priceFen(record.base_price_fen) }}</template>
          </a-table-column>
          <a-table-column title="可住" data-index="capacity" :width="60" />
          <a-table-column title="床型" data-index="bed_type" :width="100" />
          <a-table-column title="设施" :min-width="220" ellipsis>
            <template #default="{ record }">
              <template v-if="record.amenities?.length">
                <a-tag v-for="item in record.amenities" :key="item" :color="AMENITY_COLORS[item] || 'default'" style="margin:2px 4px 2px 0">
                  {{ item }}
                </a-tag>
              </template>
              <span v-else class="muted">—</span>
            </template>
          </a-table-column>
          <a-table-column title="状态" :width="80">
            <template #default="{ record }">
              <a-tag :color="record.active ? 'green' : 'default'">{{ record.active ? '启用' : '停用' }}</a-tag>
            </template>
          </a-table-column>
          <a-table-column v-if="isAdmin" title="操作" :width="160">
            <template #default="{ record }">
              <a-button type="link" @click="openEdit('roomTypes', record.id)">编辑</a-button>
              <a-button type="link" @click="toggleRoomTypeActive(record)">{{ record.active ? '停用' : '启用' }}</a-button>
            </template>
          </a-table-column>
        </a-table>
      </a-tab-pane>

      <!-- ────── 房间 ────── -->
      <a-tab-pane key="rooms" tab="房间">
        <PageToolbar>
          <a-input-search
            v-model:value="searchText.rooms"
            placeholder="搜索房间号"
            style="width:200px"
            @input="onSearch('rooms')"
            allow-clear
          />
          <a-button v-if="isAdmin" type="primary" @click="openCreate('rooms')">新增房间</a-button>
        </PageToolbar>
        <a-table :data-source="rooms" :loading="loading.rooms" row-key="id" size="middle" :pagination="{ pageSize: 10 }">
          <a-table-column title="房间号" data-index="room_no" :width="100" />
          <a-table-column title="房型" :width="200">
            <template #default="{ record }">{{ roomTypeName(record.room_type_id) }}</template>
          </a-table-column>
          <a-table-column title="楼层" data-index="floor" :width="60" />
          <a-table-column title="房态" :width="160">
            <template #default="{ record }">
              <template v-if="isAdmin">
                <a-select
                  :value="record.status"
                  size="small"
                  style="width:110px"
                  @change="(val: string) => quickSwitchRoomStatus(record, val)"
                >
                  <a-select-option v-for="s in ROOM_STATUSES" :key="s" :value="s">
                    {{ ROOM_STATUS_LABELS[s] }}
                  </a-select-option>
                </a-select>
              </template>
              <a-tag v-else>{{ ROOM_STATUS_LABELS[record.status] ?? record.status }}</a-tag>
            </template>
          </a-table-column>
          <a-table-column title="门锁设备" data-index="lock_device_no" :width="160" />
          <a-table-column v-if="isAdmin" title="操作" :width="100">
            <template #default="{ record }">
              <a-button type="link" @click="openEdit('rooms', record.id)">编辑</a-button>
            </template>
          </a-table-column>
        </a-table>
      </a-tab-pane>

      <!-- ────── 员工 ────── -->
      <a-tab-pane key="staff" tab="员工">
        <PageToolbar>
          <a-input-search
            v-model:value="searchText.staff"
            placeholder="搜索姓名/工号"
            style="width:240px"
            @input="onSearch('staff')"
            allow-clear
          />
          <a-button v-if="isAdmin" type="primary" @click="openCreate('staff')">新增员工</a-button>
        </PageToolbar>
        <a-table :data-source="staff" :loading="loading.staff" row-key="id" size="middle" :pagination="{ pageSize: 10 }">
          <a-table-column title="工号" data-index="staff_no" :width="140" />
          <a-table-column title="姓名" data-index="name" :width="140" />
          <a-table-column title="岗位" :width="120">
            <template #default="{ record }">{{ STAFF_POSITION_LABELS[record.position] ?? record.position }}</template>
          </a-table-column>
          <a-table-column title="登录账号" :width="140">
            <template #default="{ record }">{{ record.user_id ? '已绑定' : '未绑定' }}</template>
          </a-table-column>
          <a-table-column title="状态" :width="100">
            <template #default="{ record }">
              <a-tag :color="record.status === 'active' ? 'green' : 'default'">{{ ACCOUNT_STATUS_LABELS[record.status] ?? record.status }}</a-tag>
            </template>
          </a-table-column>
          <a-table-column v-if="isAdmin" title="操作" :width="100">
            <template #default="{ record }">
              <a-button type="link" @click="openEdit('staff', record.id)">编辑</a-button>
            </template>
          </a-table-column>
        </a-table>
      </a-tab-pane>

      <!-- ────── 账号 ────── -->
      <a-tab-pane v-if="canReadAccounts" key="accounts" tab="账号">
        <PageToolbar>
          <a-input-search
            v-model:value="searchText.accounts"
            placeholder="搜索用户名/显示名"
            style="width:240px"
            @input="onSearch('accounts')"
            allow-clear
          />
          <a-button type="primary" @click="openCreate('accounts')">新增账号</a-button>
        </PageToolbar>
        <a-table :data-source="accounts" :loading="loading.accounts" row-key="id" size="middle" :pagination="{ pageSize: 10 }">
          <a-table-column title="用户名" data-index="username" :width="180" />
          <a-table-column title="显示名" data-index="display_name" :width="160" />
          <a-table-column title="角色" :width="100">
            <template #default="{ record }">{{ ACCOUNT_ROLE_LABELS[record.role] ?? record.role }}</template>
          </a-table-column>
          <a-table-column title="状态" :width="100">
            <template #default="{ record }">
              <a-tag :color="record.status === 'active' ? 'green' : 'red'">{{ ACCOUNT_STATUS_LABELS[record.status] ?? record.status }}</a-tag>
            </template>
          </a-table-column>
          <a-table-column title="最近登录" :width="200">
            <template #default="{ record }">{{ record.last_login_at ? new Date(record.last_login_at).toLocaleString() : '—' }}</template>
          </a-table-column>
          <a-table-column title="操作" :width="160">
            <template #default="{ record }">
              <a-button type="link" @click="openEdit('accounts', record.id)">编辑</a-button>
              <a-button type="link" :disabled="record.username === session?.username" @click="toggleAccountStatus(record)">
                {{ record.status === 'active' ? '禁用' : '启用' }}
              </a-button>
            </template>
          </a-table-column>
        </a-table>
      </a-tab-pane>

      <!-- ────── 酒店配置 ────── -->
      <a-tab-pane key="hotelConfig" tab="酒店配置">
        <a-spin :spinning="loading.hotelConfig">
          <a-form layout="vertical" style="max-width:600px">
            <a-form-item label="酒店名称" required>
              <a-input v-model:value="hotelConfig.hotel_name" placeholder="皓晨酒店" />
            </a-form-item>
            <a-form-item label="品牌">
              <a-input v-model:value="hotelConfig.brand" placeholder="如 皓晨" />
            </a-form-item>
            <a-form-item label="地址">
              <a-input v-model:value="hotelConfig.address" placeholder="酒店地址" />
            </a-form-item>
            <a-form-item label="联系电话">
              <a-input v-model:value="hotelConfig.phone" placeholder="前台电话" />
            </a-form-item>
            <a-form-item label="简介">
              <a-textarea v-model:value="hotelConfig.description" :rows="3" placeholder="酒店简介" />
            </a-form-item>
            <a-form-item>
              <a-button type="primary" :loading="saving" @click="saveHotelConfig">保存配置</a-button>
            </a-form-item>
          </a-form>
        </a-spin>
      </a-tab-pane>
    </a-tabs>

    <!-- ────── 新增/编辑弹窗 ────── -->
    <a-modal
      v-model:open="modal.open"
      :title="modal.mode === 'create' ? '新增' : '编辑'"
      :confirm-loading="saving"
      ok-text="保存"
      cancel-text="取消"
      @ok="submit"
      :width="560"
    >
      <!-- 房型表单 -->
      <a-form v-if="activeTab === 'roomTypes'" layout="vertical">
        <a-form-item label="房型编码">
          <a-input v-model:value="roomTypeForm.code" :disabled="modal.mode === 'edit'" placeholder="如 KING" />
        </a-form-item>
        <a-form-item label="名称">
          <a-input v-model:value="roomTypeForm.name" placeholder="如 皓晨大床房" />
        </a-form-item>
        <a-form-item label="门市价（¥）">
          <a-input-number v-model:value="roomTypeForm.base_price_fen" :min="0" :precision="0" :formatter="(v: any) => v !== undefined ? `¥ ${v}` : ''" :parser="(v: string) => v.replace(/[¥\s]/g, '')" style="width:100%" />
        </a-form-item>
        <a-row :gutter="16">
          <a-col :span="12">
            <a-form-item label="可住人数">
              <a-input-number v-model:value="roomTypeForm.capacity" :min="1" :max="16" style="width:100%" />
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item label="床型">
              <a-input v-model:value="roomTypeForm.bed_type" placeholder="如 king" />
            </a-form-item>
          </a-col>
        </a-row>
        <a-form-item label="设施">
          <a-checkbox-group v-model:value="roomTypeForm.amenities">
            <a-checkbox v-for="item in AMENITY_OPTIONS" :key="item" :value="item">{{ item }}</a-checkbox>
          </a-checkbox-group>
        </a-form-item>
        <a-form-item label="描述">
          <a-textarea v-model:value="roomTypeForm.description" :rows="2" />
        </a-form-item>
      </a-form>

      <!-- 房间表单 -->
      <a-form v-else-if="activeTab === 'rooms'" layout="vertical">
        <a-form-item label="房间号">
          <a-input v-model:value="roomForm.room_no" :disabled="modal.mode === 'edit'" placeholder="如 301" />
        </a-form-item>
        <a-row :gutter="16">
          <a-col :span="14">
            <a-form-item label="房型">
              <a-select v-model:value="roomForm.room_type_id" :options="roomTypeOptions" placeholder="选择房型" />
            </a-form-item>
          </a-col>
          <a-col :span="10">
            <a-form-item label="楼层">
              <a-input-number v-model:value="roomForm.floor" :min="0" :max="200" style="width:100%" />
            </a-form-item>
          </a-col>
        </a-row>
        <a-form-item label="房态">
          <a-select
            v-model:value="roomForm.status"
            :options="ROOM_STATUSES.map((value) => ({ value, label: ROOM_STATUS_LABELS[value] }))"
          />
        </a-form-item>
        <a-form-item label="门锁设备号">
          <a-input v-model:value="roomForm.lock_device_no" placeholder="如 LOCK-301" />
        </a-form-item>
      </a-form>

      <!-- 员工表单 -->
      <a-form v-else-if="activeTab === 'staff'" layout="vertical">
        <a-form-item label="工号">
          <a-input v-model:value="staffForm.staff_no" :disabled="modal.mode === 'edit'" placeholder="如 HK-2001" />
        </a-form-item>
        <a-row :gutter="16">
          <a-col :span="14">
            <a-form-item label="姓名">
              <a-input v-model:value="staffForm.name" />
            </a-form-item>
          </a-col>
          <a-col :span="10">
            <a-form-item label="岗位">
              <a-select
                v-model:value="staffForm.position"
                :options="STAFF_POSITIONS.map((v) => ({ value: v, label: STAFF_POSITION_LABELS[v] }))"
              />
            </a-form-item>
          </a-col>
        </a-row>
        <a-form-item label="绑定登录账号">
          <a-select
            v-model:value="staffForm.user_id"
            :options="accountOptions"
            allow-clear
            placeholder="可选：绑定后可用该账号登录保洁端"
          />
        </a-form-item>
        <a-form-item label="状态">
          <a-select
            v-model:value="staffForm.status"
            :options="ACCOUNT_STATUSES.map((v) => ({ value: v, label: ACCOUNT_STATUS_LABELS[v] }))"
          />
        </a-form-item>
      </a-form>

      <!-- 账号表单 -->
      <a-form v-else layout="vertical">
        <a-form-item label="用户名">
          <a-input v-model:value="accountForm.username" :disabled="modal.mode === 'edit'" />
        </a-form-item>
        <a-form-item :label="modal.mode === 'create' ? '密码' : '重置密码（留空不变）'">
          <a-input-password v-model:value="accountForm.password" autocomplete="new-password" />
        </a-form-item>
        <a-row :gutter="16">
          <a-col :span="14">
            <a-form-item label="显示名">
              <a-input v-model:value="accountForm.display_name" />
            </a-form-item>
          </a-col>
          <a-col :span="10">
            <a-form-item label="角色">
              <a-select
                v-model:value="accountForm.role"
                :options="ACCOUNT_ROLES.map((v) => ({ value: v, label: ACCOUNT_ROLE_LABELS[v] }))"
              />
            </a-form-item>
          </a-col>
        </a-row>
        <a-form-item v-if="modal.mode === 'edit'" label="状态">
          <a-select
            v-model:value="accountForm.status"
            :options="ACCOUNT_STATUSES.map((v) => ({ value: v, label: ACCOUNT_STATUS_LABELS[v] }))"
          />
        </a-form-item>
      </a-form>
    </a-modal>
  </SectionCard>
</template>

<style scoped>
.muted {
  color: var(--text-4);
}
</style>