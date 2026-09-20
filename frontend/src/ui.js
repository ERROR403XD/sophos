// 全局 UI 状态单例（R6）：
// - isMobile：≤768px 或移动 UA（PlayerDialog 打开时求值的历史教训——
//   matchMedia 不是响应式的，这里统一用 resize 监听维护，供各视图直接引用）
// - authRequired / authed：访问口令层（配合 api.js 的 401 拦截器）
import { reactive } from 'vue'

function checkMobile() {
  return window.matchMedia('(max-width: 768px)').matches ||
    /Android|iPhone|iPad|Mobile/i.test(navigator.userAgent)
}

export const ui = reactive({
  isMobile: checkMobile(),
  authRequired: false,   // 服务端是否启用了口令
  authed: true,          // 当前会话是否已通过
  loginVisible: false,   // 登录层显隐
})

window.addEventListener('resize', () => { ui.isMobile = checkMobile() })

export function requireLogin() {
  // 任一 API 返回 401 → 弹登录层（幂等）
  if (ui.authRequired && !ui.authed) ui.loginVisible = true
}
