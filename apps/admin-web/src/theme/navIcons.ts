import { h, type Component, type VNode } from 'vue'
import {
  ApiOutlined,
  AppstoreOutlined,
  AuditOutlined,
  BarChartOutlined,
  BookOutlined,
  BulbOutlined,
  CloudServerOutlined,
  CustomerServiceOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  FileTextOutlined,
  HeartOutlined,
  MessageOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons-vue'

/** 路由名 → 菜单图标。新增菜单时在此补充，未命中时走兜底图标。 */
export const NAV_ICONS: Record<string, Component> = {
  dashboard: DashboardOutlined,
  agents: RobotOutlined,
  'mcp-servers': CloudServerOutlined,
  conversations: MessageOutlined,
  reports: BarChartOutlined,
  health: HeartOutlined,
  logs: FileTextOutlined,
  data: DatabaseOutlined,
  memories: BulbOutlined,
  handoff: CustomerServiceOutlined,
  approvals: SafetyCertificateOutlined,
  'eval-agent': ExperimentOutlined,
  'eval-kb': BookOutlined,
  'quality-scorecard': AuditOutlined,
  'data-pipeline': ApiOutlined,
}

export const FALLBACK_NAV_ICON: Component = AppstoreOutlined

export function resolveNavIcon(name: string): Component {
  return NAV_ICONS[name] ?? FALLBACK_NAV_ICON
}

export function navIconNode(name: string): VNode {
  return h(resolveNavIcon(name))
}
