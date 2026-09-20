import axios from 'axios'
import { ui, requireLogin } from './ui'

export const api = axios.create({ baseURL: '/api', timeout: 60000 })

export function errText(e) {
  const d = e?.response?.data
  return d?.message || d?.detail?.message || e?.message || String(e)
}

// R6：访问口令。启动时探测一次；任何请求 401（会话过期/改密码）→ 弹登录层。
api.interceptors.response.use(
  (r) => r,
  (e) => {
    if (e?.response?.status === 401) {
      ui.authRequired = true
      ui.authed = false
      requireLogin()
    }
    return Promise.reject(e)
  },
)

export async function probeAuth() {
  try {
    const r = await axios.get('/api/auth/status')
    ui.authRequired = !!r.data.required
    ui.authed = !!r.data.authed
    if (ui.authRequired && !ui.authed) ui.loginVisible = true
  } catch { /* 后端过旧无此端点：视为无鉴权 */ }
}

export async function login(password) {
  await axios.post('/api/auth/login', { password })
  ui.authed = true
  ui.loginVisible = false
}
