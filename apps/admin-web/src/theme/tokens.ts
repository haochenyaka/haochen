/**
 * 品牌设计 Token（admin-web）—— 三端唯一事实源之一。
 *
 * 主色：藏青 #1A3A6B   强调：香槟金 #C9A227
 * ⚠️ 改色需同步：src/styles/tokens.css（CSS 变量镜像）、
 *    ../../guest-miniapp/src/uni.scss、../../staff-miniapp/src/uni.scss。
 */
import type { ThemeConfig } from 'ant-design-vue/es/config-provider/context'

/** 藏青主色阶（1 浅 → 10 深）。brand[6] 即 colorPrimary。 */
export const brand = {
  1: '#EEF2F8',
  2: '#D6E0EF',
  3: '#AEC1DB',
  4: '#7E9AC1',
  5: '#4F73A5',
  6: '#1A3A6B',
  7: '#16315B',
  8: '#12284B',
  9: '#0E1F3B',
  10: '#0A162B',
} as const

/** 香槟金色阶（1 浅 → 6 强调 → 7 hover）。gold[6] 即 accent。 */
export const gold = {
  1: '#FBF6E7',
  2: '#F6EBC7',
  3: '#EDD892',
  4: '#E3C45D',
  5: '#D2AF38',
  6: '#C9A227',
  7: '#A8851F',
} as const

/** 中性色。 */
export const neutral = {
  text1: '#1F2329',
  text2: '#4B5563',
  text3: '#6B7280',
  text4: '#9CA3AF',
  border: '#E5E7EB',
  divider: '#F0EDE6',
  bgLayout: '#F7F5F0',
  bgContainer: '#FFFFFF',
  bgHover: '#FAF9F5',
} as const

/** 语义色。 */
export const semantic = {
  success: '#2E9E5B',
  warning: '#D98A1F',
  error: '#D03050',
  info: brand[6],
} as const

/** 圆角（px）。 */
export const radius = {
  sm: 6,
  md: 8,
  lg: 12,
  xl: 16,
} as const

export const gradientBrand = `linear-gradient(135deg, ${brand[6]}, #24487C)`
export const gradientGold = `linear-gradient(135deg, ${gold[6]}, ${gold[4]})`

/** AntD Vue ConfigProvider 主题。 */
export const themeConfig: ThemeConfig = {
  token: {
    colorPrimary: brand[6],
    colorInfo: brand[6],
    colorLink: brand[6],
    colorLinkHover: brand[5],
    colorSuccess: semantic.success,
    colorWarning: semantic.warning,
    colorError: semantic.error,
    colorBgLayout: neutral.bgLayout,
    colorBgContainer: neutral.bgContainer,
    colorBorder: neutral.border,
    colorBorderSecondary: neutral.divider,
    colorText: neutral.text1,
    colorTextSecondary: neutral.text2,
    colorTextTertiary: neutral.text3,
    colorTextQuaternary: neutral.text4,
    borderRadius: radius.md,
    borderRadiusSM: radius.sm,
    borderRadiusLG: radius.lg,
    fontFamily:
      "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif",
    fontSize: 14,
    controlHeight: 34,
    boxShadow: '0 2px 8px rgba(26, 58, 107, 0.08)',
    boxShadowSecondary: '0 4px 16px rgba(26, 58, 107, 0.10)',
  },
  components: {
    // 说明：ant-design-vue 4.2 仅 Layout / Menu 暴露了可用的组件级 token；
    // Table / Card / Input / Select / Tag 等的细粒度样式改由
    // src/styles/global.css 的 CSS 覆盖承担（见该文件）。
    Layout: {
      colorBgHeader: '#FFFFFF',
      colorBgBody: neutral.bgLayout,
      colorBgTrigger: brand[9],
    },
    Menu: {
      // 深色菜单值：藏青深底 + 白字 + 透明选中底（选中态渐变由 AdminLayout CSS 覆盖）。
      colorItemBg: brand[8],
      colorSubItemBg: brand[9],
      colorItemText: 'rgba(255, 255, 255, 0.72)',
      colorItemTextHover: '#FFFFFF',
      colorItemTextSelected: '#FFFFFF',
      colorItemBgHover: 'rgba(255, 255, 255, 0.08)',
      colorItemBgSelected: 'transparent',
      radiusItem: radius.sm,
      itemMarginInline: 8,
    },
  },
}