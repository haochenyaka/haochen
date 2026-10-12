<script setup lang="ts">
import { onLaunch } from '@dcloudio/uni-app'
import { getSession, installNavigationGuards } from '@/stores/auth'

onLaunch(() => {
  installNavigationGuards()
  if (!getSession()) {
    uni.reLaunch({ url: '/pages/login/index' })
  }
})
</script>

<style lang="scss">
@import './styles/theme.scss';
</style>