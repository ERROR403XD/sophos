import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import App from './App.vue'

createApp(App).use(ElementPlus, { locale: zhCn }).mount('#app')

// R6.1：PWA Service Worker 注册（仅安全上下文生效——localhost 或 https；
// 经局域网 IP 的 http 访问时浏览器拒绝注册，属平台限制，静默跳过）
if ('serviceWorker' in navigator && window.isSecureContext) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {})
  })
}
