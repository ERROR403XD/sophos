<template>
  <div class="app-shell" :class="{ mobile: ui.isMobile }">
    <!-- 移动端顶栏 -->
    <header v-if="ui.isMobile" class="app-topbar">
      <div class="brand">
        <img src="/icon.png" class="brand-icon" alt="" />
        <span class="brand-name">Sophos</span>
      </div>
    </header>

    <!-- 桌面顶栏 -->
    <el-header v-else style="background:#1f2d3d; color:#fff; display:flex; align-items:center; gap:12px">
      <img src="/icon.png" class="brand-icon" alt="" />
      <span style="font-size:20px; font-weight:600">Sophos</span>
    </el-header>

    <main class="app-main">
      <template v-if="!ui.isMobile">
        <el-tabs v-model="tab" type="border-card">
          <el-tab-pane v-for="t in TABS" :key="t.name" :label="t.label" :name="t.name">
            <component :is="t.comp" />
          </el-tab-pane>
        </el-tabs>
      </template>
      <template v-else>
        <KeepAlive>
          <component :is="activeComp" />
        </KeepAlive>
      </template>
    </main>

    <!-- 移动端底部 TabBar -->
    <nav v-if="ui.isMobile" class="app-tabbar">
      <button v-for="t in TABS" :key="t.name" class="tab-item"
              :class="{ active: tab === t.name }" @click="tab = t.name">
        <span class="tab-icon">{{ t.icon }}</span>
        <span class="tab-label">{{ t.label }}</span>
      </button>
    </nav>

    <!-- R6：访问口令登录层（401 时全局弹出，不可关闭） -->
    <div v-if="ui.loginVisible" class="login-mask">
      <div class="login-card">
        <div class="login-title">Sophos</div>
        <div class="login-sub">请输入访问密码</div>
        <el-input v-model="password" type="password" size="large" show-password
                  placeholder="访问密码" @keyup.enter="doLogin" />
        <el-button type="primary" size="large" style="width:100%; margin-top:14px"
                   :loading="logging" @click="doLogin">进入</el-button>
        <div v-if="loginError" class="login-error">{{ loginError }}</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import RateView from './views/RateView.vue'
import PairView from './views/PairView.vue'
import VideosView from './views/VideosView.vue'
import TasksView from './views/TasksView.vue'
import TrainView from './views/TrainView.vue'
import AnalyzeView from './views/AnalyzeView.vue'
import { ui } from './ui'
import { login, probeAuth } from './api'

const TABS = [
  { name: 'rate', label: '评分', icon: '⭐', comp: RateView },
  { name: 'pair', label: '对比', icon: '⚖️', comp: PairView },
  { name: 'videos', label: '视频库', icon: '🎬', comp: VideosView },
  { name: 'analyze', label: '分析', icon: '🔍', comp: AnalyzeView },
  { name: 'train', label: '训练', icon: '🧠', comp: TrainView },
  { name: 'tasks', label: '任务', icon: '⚙️', comp: TasksView },
]

const tab = ref('rate')
const activeComp = computed(() => TABS.find(t => t.name === tab.value)?.comp)

const password = ref('')
const logging = ref(false)
const loginError = ref('')

async function doLogin() {
  if (!password.value) return
  logging.value = true
  loginError.value = ''
  try {
    await login(password.value)
    password.value = ''
    ElMessage.success('已登录')
    location.reload()  // 整页刷新：所有视图带 Cookie 重新拉取
  } catch (e) {
    loginError.value = e?.response?.data?.message || '密码错误'
  } finally {
    logging.value = false
  }
}

// 会话过期/改密码后重新登录成功 → reload 会整页重载；这里兜底隐藏登录层
watch(() => ui.authed, (v) => { if (v) ui.loginVisible = false })

onMounted(probeAuth)
</script>

<style>
/* R6 全局：移动端外壳 + 登录层。桌面样式保持 Element Plus 默认。 */
html, body, #app { height: 100%; }
body { margin: 0; background: #f5f7fa; -webkit-tap-highlight-color: transparent; }

.app-shell { min-height: 100vh; }
.app-main { padding: 12px; }

/* ---- 移动端 ---- */
.app-shell.mobile { padding-bottom: env(safe-area-inset-bottom); }
.app-topbar {
  position: sticky; top: 0; z-index: 100;
  display: flex; align-items: center; gap: 10px;
  padding: calc(10px + env(safe-area-inset-top)) 16px 10px;
  background: linear-gradient(135deg, #1f2d3d, #2b3f55);
  color: #fff; box-shadow: 0 2px 8px rgba(0,0,0,.15);
}
.brand { display: flex; align-items: center; gap: 8px; }
.brand-icon { width: 28px; height: 28px; border-radius: 50%; display: block; }
.brand-name { font-size: 18px; font-weight: 700; letter-spacing: .5px; }

.app-tabbar {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 100;
  display: flex;
  padding-bottom: env(safe-area-inset-bottom);
  background: #fff; border-top: 1px solid #ebeef5;
}
.tab-item {
  flex: 1; border: 0; background: none; cursor: pointer;
  display: flex; flex-direction: column; align-items: center; gap: 1px;
  padding: 7px 0 6px; color: #909399; font-size: 11px;
}
.tab-item .tab-icon { font-size: 19px; line-height: 1; }
.tab-item.active { color: #409eff; }
.app-shell.mobile .app-main { padding: 10px 10px calc(64px + env(safe-area-inset-bottom)); }

/* ---- 登录层 ---- */
.login-mask {
  position: fixed; inset: 0; z-index: 2000;
  display: flex; align-items: center; justify-content: center;
  background: rgba(15, 23, 42, .55); backdrop-filter: blur(6px);
}
.login-card {
  width: min(88vw, 340px); padding: 28px 24px;
  background: #fff; border-radius: 14px; text-align: center;
  box-shadow: 0 12px 40px rgba(0,0,0,.25);
}
.login-title { font-size: 22px; font-weight: 700; color: #1f2d3d; }
.login-sub { margin: 6px 0 18px; font-size: 13px; color: #909399; }
.login-error { margin-top: 10px; font-size: 12px; color: #f56c6c; }

/* 移动端通用：卡内表格可横滑兜底 */
.mobile-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
</style>
