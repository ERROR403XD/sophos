<template>
  <!-- 桌面：对话框（保持 R5.2 形态） -->
  <el-dialog v-if="!state.isMobile" v-model="state.open" :title="state.title"
             :width="'860px'" @close="close" destroy-on-close class="player-dialog">
    <div class="player-dialog-body">
      <div v-if="state.mode" class="mode-bar">
        <el-tag v-if="state.mode === 'direct'" type="success" effect="plain">直出播放</el-tag>
        <el-tag v-else-if="state.mode === 'remux'" type="primary" effect="plain">重封装播放（秒开）</el-tag>
        <el-tag v-else-if="state.mode === 'transcode'" type="warning" effect="plain">
          转码播放中，首次加载稍慢
        </el-tag>
        <el-tag v-if="state.fellBack" type="danger" effect="plain">已自动切换转码（直出失败）</el-tag>
        <el-tag v-if="state.seeking" type="info" effect="plain">跳转中…（服务端定位）</el-tag>
        <el-button size="small" text @click="toggleFullscreen">全屏</el-button>
      </div>
      <div ref="artRef" class="player-box"
           style="width:100%; aspect-ratio:16/9; background:#000"></div>
      <div v-if="state.error" class="player-error">
        <div>视频播放失败：{{ state.error }}</div>
        <el-button size="small" style="margin-top:6px" @click="retry">重试</el-button>
      </div>
    </div>
  </el-dialog>

  <!-- R7 移动端：专门的全屏播放器（非弹窗）——整屏黑底、顶栏返回+标题+状态，
       播放器填满其余空间；Teleport 到 body 脱离页面布局，z-index 盖过 TabBar。
       R8：高度改 100dvh（移动浏览器动态地址栏下 100vh/inset 与可视区不匹配），
       打开期间 body 整体锁定（position:fixed，iOS 上 overflow:hidden 无效）。 -->
  <Teleport to="body">
    <div v-if="state.isMobile && state.open" class="mplayer">
      <div class="mplayer-top">
        <button class="mplayer-back" aria-label="返回" @click="close">‹</button>
        <div class="mplayer-title">{{ state.title }}</div>
      </div>
      <div v-if="state.mode" class="mplayer-status">
        <span v-if="state.mode === 'direct'" class="mstat ok">直出</span>
        <span v-else-if="state.mode === 'remux'" class="mstat info">重封装</span>
        <span v-else-if="state.mode === 'transcode'" class="mstat warn">转码</span>
        <span v-if="state.useHls" class="mstat hls">HLS</span>
        <span v-if="state.fellBack" class="mstat bad">已切换转码</span>
        <span v-if="state.seeking" class="mstat info">跳转中…</span>
      </div>
      <div ref="artRef" class="mplayer-stage"></div>
      <div v-if="state.error" class="mplayer-error">
        <div>视频播放失败：{{ state.error }}</div>
        <button class="mplayer-retry" @click="retry">重试</button>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
// R7：统一播放器（评分页 / 对比页 / 视频库共用），内核 ArtPlayer。
// 本轮（R8，ADR-029）修订——移动端播放修复：
// - 根因：iOS Safari 起播前发 `Range: bytes=0-1` 探测，服务器必须回 206；
//   fMP4 管道流（/stream）是 200+chunked 无 Range，remux/transcode 在 iOS
//   双双失败（MediaError code 4「源文件不存在或格式不支持」），且降级转码
//   换汤不换药（同一传输层）。移动端非直出档改走 **HLS 会话**（Jellyfin/
//   Emby 同款架构）：iOS Safari 原生 HLS（video.src=m3u8，无 MSE 依赖），
//   Android/桌面 Chromium 用 hls.js（MSE，按需加载独立 chunk）；切片经
//   FileResponse 下发天然支持 Range（后端 /api/videos/{id}/hls/{token}/…）。
// - seek 语义：HLS playlist 内可寻位置交给播放器原生 seek（remux 秒级整
//   文件就绪，实际全靠原生）；目标超出切片边界（转码未推进到）才换新会话
//   （新 token + ?ss=），与 R5 的热切换语义一致。
// - 降级链：HLS remux 失败 → HLS 转码(fallback=1) → 报分型错误；移动端
//   direct 档失败也顺带切到 HLS 转码（原为渐进转码管道，传输层同样的坑）。
// - 返回键（Android 返回手势/iOS 侧滑）：打开时 pushState 占位，popstate
//   时关闭播放器而不是退出网页；UI 内关闭时 history.back() 消费占位。
// - 全屏/滚动：mplayer 高度 100dvh；打开期间 body position:fixed 锁滚动
//   （iOS Safari 对 overflow:hidden 锁定免疫，页面会被拖动的根因）。
// R7 修订（保留）：open() 同步 artEpoch 竞态修复；timeBase 时间轴基准；
// 无进展看门狗；startAt 只应用一次；状态实例局部化。
import { onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import Artplayer from 'artplayer'

const state = reactive({
  open: false, title: '', src: '', baseSrc: '', hlsBase: '', mode: '', seeking: false,
  startAt: 0, error: '', isMobile: false, fellBack: false, useHls: false,
  epoch: 0,  // 每次换源 +1；事件回调据此丢弃旧流消息
})
const artRef = ref(null)
// —— 实例局部播放器状态（勿提升为模块级：三个视图各持一个本组件实例） ——
let art = null
let seekTimer = null
let stallTimer = null
let hooksAttached = false
let artEpoch = 0        // 与 state.epoch 同步：换流后旧回调不再作用
let timeBase = 0        // 当前流 currentTime=0 对应的源内容时刻（remux/transcode/HLS）
let startAtApplied = false  // direct 档 startAt 只应用一次
let mountTimer = null
let hlsLib = null       // 惰性加载的 hls.js（独立 chunk，桌面/直出不加载）
let hlsInst = null      // 当前 hls.js 实例（MSE 路径）
let backArmed = false   // 返回键捕获：pushState 占位是否在栈上
let lockScrollY = 0
let scrollLocked = false
let hlsToken = ''       // 当前 HLS 会话 token（关闭/换流时用于通知服务端停 ffmpeg）

function checkMobile() {
  return window.matchMedia('(max-width: 768px)').matches ||
    /Android|iPhone|iPad|Mobile/i.test(navigator.userAgent)
}

function buildSrc(base, { ss = 0, fallback = false } = {}) {
  const params = []
  if (ss > 0) params.push(`ss=${Number(ss).toFixed(2)}`)
  if (fallback) params.push('fallback=1')
  if (!params.length) return base
  const sep = base.includes('?') ? '&' : '?'
  return `${base}${sep}${params.join('&')}`
}

// ---- R8：HLS 会话源（token 每次起流/热切换随机生成；新 token = 新会话） ----
function newHlsToken() {
  return `t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`
}

function buildHlsSrc({ ss = 0, fallback = 0, token: useToken = '' } = {}) {
  const token = useToken || newHlsToken()
  const params = []
  if (ss > 0) params.push(`ss=${Number(ss).toFixed(2)}`)
  if (fallback) params.push('fallback=1')
  const q = params.length ? `?${params.join('&')}` : ''
  hlsToken = token
  return `${state.hlsBase}/${token}/index.m3u8${q}`
}

// R9：通知服务端结束会话（立即 kill ffmpeg）。不通知的话，快速退出播放后
// 转码进程会继续占满 CPU 直到 120s 心跳超时——"退出后页面卡住"的主因之一。
// 幂等且不关心响应；keepalive 保证关闭页面时仍能发出。
function stopHlsSession(token) {
  if (!token || !state.hlsBase) return
  try {
    fetch(`${state.hlsBase}/${token}/index.m3u8`, {
      method: 'DELETE', keepalive: true, cache: 'no-store',
    }).catch(() => {})
  } catch { /* 忽略：服务端看门狗会兜底回收 */ }
}

function detachHls() {
  stopEndlistPoll()
  if (hlsInst) {
    try { hlsInst.destroy() } catch { /* 已销毁 */ }
    hlsInst = null
  }
}

// HLS 挂载：iOS/其它原生 HLS 浏览器直接 video.src（Apple 自家栈，最稳硬解）；
// 其余走 hls.js（MSE）。fatal 错误先按 hls.js 官方恢复策略自愈，失败进降级链。
let endlistTimer = null

function stopEndlistPoll() {
  if (endlistTimer) { clearTimeout(endlistTimer); endlistTimer = null }
}

// 原生 HLS + EVENT playlist（转码推进中）的兜底：部分 Chromium 系内核自带
// 原生 HLS 但把 event 流当纯直播——不设 duration、seekable 为空（所有原生
// seek 被浏览器丢弃）、以极高频率轮询 playlist。这里自行探测 ENDLIST（remux
// 秒级完成 / 转码推进完 / 看门狗补写），一旦出现就以 VOD 语义重载同一 playlist
// ——重载后 duration/seekable 立即完整，进度条与拖动全部恢复正常。
// iOS Safari 对 event 流原生处理正确（duration 随切片增长），本探测在其上
// 至多多一次无害重载。
function pollEndlistThenReload(url) {
  stopEndlistPoll()
  const tick = async () => {
    endlistTimer = null
    const v = art?.video
    if (!v || state.epoch !== artEpoch) return
    try {
      const text = await fetch(url, { cache: 'no-store' }).then(r => r.text())
      if (state.epoch !== artEpoch) return
      if (text.includes('#EXT-X-ENDLIST') && !Number.isFinite(v.duration)) {
        const at = v.currentTime || 0
        const restore = () => {
          v.removeEventListener('loadedmetadata', restore)
          try { if (at > 0.5) v.currentTime = at } catch { /* 时长未就绪 */ }
        }
        v.addEventListener('loadedmetadata', restore)
        v.src = url  // 同一 playlist 以 VOD 语义重载
        v.load()
        v.play().catch(() => {})
        return
      }
      if (!text.includes('#EXT-X-ENDLIST') && Number.isFinite(v.duration)) return  // 已是 VOD
    } catch { /* 网络抖动：下轮再试 */ }
    endlistTimer = setTimeout(tick, 2000)
  }
  endlistTimer = setTimeout(tick, 1500)
}

async function attachHls(video, url) {
  detachHls()
  const native = video.canPlayType && video.canPlayType('application/vnd.apple.mpegurl')
  if (native) {
    video.src = url
    video.load()
    video.play().catch(() => {})
    pollEndlistThenReload(url)
    return
  }
  const epoch = state.epoch
  if (!hlsLib) hlsLib = (await import('hls.js')).default
  // R9：动态 import 期间用户可能已关闭播放器/换流（快速退出重开）——此时
  // 若继续建实例并 attach 到已销毁的 <video>，该 hls.js 实例永不销毁，
  // 会一直拉切片/拖着服务端 ffmpeg（每开一次泄一个），最终拖垮页面。
  if (!state.open || state.epoch !== epoch || state.epoch !== artEpoch) return
  const Hls = hlsLib
  const inst = new Hls({ enableWorker: true, lowLatencyMode: false,
                         maxBufferLength: 30, backBufferLength: 60 })
  hlsInst = inst
  video._hls = inst  // 调试句柄（官方文档同款模式）
  inst.on(Hls.Events.ERROR, (_evt, data) => {
    if (!data.fatal || state.epoch !== artEpoch) return
    if (data.type === Hls.ErrorTypes.NETWORK_ERROR) { inst.startLoad(); return }
    if (data.type === Hls.ErrorTypes.MEDIA_ERROR) { inst.recoverMediaError(); return }
    onStreamError(4)
  })
  inst.loadSource(url)
  inst.attachMedia(video)
  video.play().catch(() => {})
}

// R9：统一的播放器拆除（re-open 与 close 共用）。反复播放/快速退出时旧
// ArtPlayer 实例与旧 <video> 必须显式销毁——只清 innerHTML 的话旧实例
// 仍持有网络流（服务端 ffmpeg 继续切片），每次重开泄一份，累计拖垮页面。
function teardownPlayer() {
  if (seekTimer) { clearTimeout(seekTimer); seekTimer = null }
  disarmStallWatchdog()
  hooksAttached = false
  detachHls()
  stopHlsSession(hlsToken)
  hlsToken = ''
  if (art) {
    try { art.pause() } catch { /* 尚未就绪 */ }
    art.destroy(true)
    art = null
  }
  if (artRef.value) artRef.value.innerHTML = ''
}

function open(row) {
  // row: { title, stream_url, hls_url?, stream_mode, startAt? }
  // R9：重开安全——上一实例（可能在播放中）先拆干净再建新的
  teardownPlayer()
  if (mountTimer) { clearTimeout(mountTimer); mountTimer = null }
  state.isMobile = checkMobile()  // 打开时求值（matchMedia 非响应式，computed 会缓存陈旧值）
  state.title = row.title || ''
  state.mode = row.stream_mode || ''
  state.startAt = row.startAt || 0
  state.fellBack = false
  state.baseSrc = row.stream_url
  state.hlsBase = row.hls_url || ''
  // R8：移动端非直出档一律 HLS 会话。直出 mp4 的 FileResponse 本就支持
  // Range（iOS 兼容），保持渐进直出不动。
  state.useHls = state.isMobile && !!state.hlsBase && state.mode !== 'direct'
  state.epoch += 1
  artEpoch = state.epoch  // R7 修复：open 必须同步 artEpoch（否则起播 seek 与 seek 拦截全被 epoch 守卫丢弃）
  timeBase = 0
  startAtApplied = false
  const useSs = (state.mode === 'remux' || state.mode === 'transcode') ? state.startAt : 0
  timeBase = useSs  // remux/transcode 的 ss 起播：currentTime=0 对应源内容 useSs
  state.src = state.useHls
    ? buildHlsSrc({ ss: useSs })
    : buildSrc(row.stream_url, { ss: useSs })
  state.seeking = false
  state.error = ''
  state.open = true
  lockPageScroll()
  if (state.isMobile) armBackCapture()  // 返回手势/返回键 → 关播放器而非退出网页
  if (mountTimer) { clearTimeout(mountTimer); mountTimer = null }
  mountTimer = setTimeout(mountPlayer, 50)  // 等 dialog/overlay DOM 渲染
}

// ---- 移动端滚动锁（R8）：iOS Safari 对 body overflow:hidden 免疫，
// 用 position:fixed + 负 top 冻结页面，关闭时还原滚动位置。 ----
function lockPageScroll() {
  if (scrollLocked) return  // 重开（已锁）时别覆盖记录的滚动位置为 0
  scrollLocked = true
  lockScrollY = window.scrollY || 0
  document.body.classList.add('mplayer-lock')
  document.body.style.top = `-${lockScrollY}px`
}

function unlockPageScroll() {
  if (!scrollLocked) return
  scrollLocked = false
  document.body.classList.remove('mplayer-lock')
  document.body.style.top = ''
  window.scrollTo(0, lockScrollY)
}

// ---- 返回键捕获（R8）：history 占位栈条目，popstate 即关闭播放器 ----
function onBackPop() {
  if (!backArmed) return
  backArmed = false
  window.removeEventListener('popstate', onBackPop)
  if (state.open) close({ viaBack: true })
}

function armBackCapture() {
  if (backArmed) return
  history.pushState({ sophosPlayer: Date.now() }, '')
  backArmed = true
  window.addEventListener('popstate', onBackPop)
}

function disarmBackCapture(consume) {
  if (!backArmed) return
  backArmed = false
  window.removeEventListener('popstate', onBackPop)
  if (consume) history.back()  // 消费占位条目（onBackPop 已解绑，不会递归 close）
}

function mountPlayer(attempt = 0) {
  mountTimer = null
  if (!state.open) return
  if (!artRef.value) {
    // overlay/dialog 渲染未完成：短暂重试（原实现 50ms 单发，慢设备上会静默失挂）
    if (attempt < 40) mountTimer = setTimeout(() => mountPlayer(attempt + 1), 50)
    return
  }
  artRef.value.innerHTML = ""  // 清掉上次残留（双保险，防死元素干扰）
  const artOpts = {
    container: artRef.value,
    url: state.src,
    autoplay: true,
    playsInline: true,
    setting: true,
    // 移动端保留原生全屏按钮（Android 横屏锁定；iOS Safari 由 ArtPlayer 走
    // webkit 回退）——外层 overlay 本身已全屏，按钮是增强而非依赖
    fullscreen: true,
    fullscreenWeb: false,
    pip: !state.isMobile,
    miniProgressBar: true,
    autoSize: false,
    autoOrientation: state.isMobile, // 移动端竖屏视频自动旋转铺满
    moreVideoAttr: { playsInline: true, 'webkit-playsinline': true },
  }
  // R8：HLS 挂载走 ArtPlayer customType 扩展点（原生/hls.js 双路径见 attachHls）。
  // 注意：type/customType 只能在 HLS 时存在——显式传 undefined 会覆盖 ArtPlayer
  // 的默认 customType:{}，其 url setter 里 option.customType[typeName] 直接
  // TypeError，桌面渐进播放整个挂掉（实测）。
  if (state.useHls) {
    artOpts.type = 'm3u8'
    artOpts.customType = { m3u8: (video, url) => { attachHls(video, url) } }
  }
  art = new Artplayer(artOpts)
  art.on('destroy', detachHls)
  hooksAttached = false  // 新 art 实例 = 新 <video>，必须重挂原生钩子
  attachNativeHooks()
  art.on('ready', attachNativeHooks)
  // R7 关键修复：Chrome 对无 Range 的 fMP4 管道流（empty_moov 头 duration=0）
  // 判定 seekable=[0,0]——用户拖到缓冲区外时原生 seeking 事件拿到的目标已被
  // **钳制到 0**（实测 seeking@0.00），原生钩子层永远看不到真实落点，表现即
  // "拖动后回到流开头"。ArtPlayer 进度条/触摸拖动统一走 art.seek setter，
  // 其 emit("seek", clamped, intended) 的第二参数是**未钳制目标**——在此拦截
  // 热切换。原生 seeking 钩子保留（兜底不钳制的浏览器/键盘 seek 路径）。
  // R8：HLS 同样拦截——playlist 内可寻位置由 hotSwapTo 交还原生 seek，
  // 超出切片边界（转码未推进到）才换新会话。
  art.on('seek', (_clamped, intended) => {
    if (state.epoch !== artEpoch) return
    if (!(state.useHls || state.mode === 'remux' || state.mode === 'transcode')) return
    const t = Number(intended)
    if (!Number.isFinite(t) || t <= 0.05) return
    const v = art?.video
    if (!v || bufferedCovers(v, t)) return
    if (seekTimer) clearTimeout(seekTimer)  // 连续拖动只取最后落点
    seekTimer = setTimeout(() => hotSwapTo(timeBase + t), 250)
  })
  // direct 档有真 Range：起播后直接跳到目标时间点（如评分页面容所在时刻）。
  // 只应用一次：fallback 换流后的 loadedmetadata 若重放会把时间轴跳乱。
  if (state.mode === 'direct' && state.startAt > 0) {
    art.on('video:loadedmetadata', () => {
      if (startAtApplied || !art?.video) return
      if (state.epoch !== artEpoch) return
      startAtApplied = true
      art.video.currentTime = state.startAt
    })
  }
  armStallWatchdog()  // 起播即挂：死流（只吐 fMP4 头等）不触发 error 时兜底
}

function mediaErrorMessage(code) {
  if (code === 2) return '网络加载失败（连接中断或服务不可用）'
  if (code === 3) return '解码失败（浏览器不支持该编码）'
  if (code === 4) return '源文件不存在或格式不支持'
  return '流加载失败或编码不支持'
}

function attachNativeHooks() {
  if (hooksAttached || !art) return
  const v = art.video || artRef.value?.querySelector('video')
  if (!v) {
    setTimeout(attachNativeHooks, 100)
    return
  }
  hooksAttached = true
  v.addEventListener('seeking', onSeeking)
  const markHealthy = () => {
    if (state.epoch === artEpoch) state.seeking = false
    disarmStallWatchdog()
  }
  v.addEventListener('canplay', markHealthy)
  v.addEventListener('playing', markHealthy)
  v.addEventListener('timeupdate', () => {
    if (state.epoch === artEpoch) state.seeking = false
    disarmStallWatchdog()
  })
  v.addEventListener('waiting', () => {
    if (state.epoch === artEpoch) armStallWatchdog()
  })
  v.addEventListener('error', () => {
    if (state.epoch !== artEpoch) return  // 旧流迟到 error：忽略（R6 竞态加固）
    const code = v.error ? v.error.code : 0
    onStreamError(code)
  })
}

// 无进展看门狗（R7）：waiting/起播后 25s 内既无播放推进也无缓冲增长，
// 判定流死或不可恢复地停滞。direct/remux 尚未降级过 → 自动切转码；
// 已降级（转码档仍停滞）→ 显示错误提示（保留手动重试）。
// 缓冲仍在增长（慢源）则续期等待，不误杀。
let stallMark = -1

function bufferedEnd() {
  const v = art?.video
  try {
    return v && v.buffered.length ? v.buffered.end(v.buffered.length - 1) : -1
  } catch { return -1 }
}

function hlsSeekableEnd() {
  const v = art?.video
  try {
    return v && v.seekable.length ? v.seekable.end(v.seekable.length - 1) : 0
  } catch { return 0 }
}

// R8：HLS 可寻边界。hls.js 的 video.seekable 在缓冲切换的瞬态可能为空（读到 0），
// currentLevel=-1（自动档）也取不到单级详情——遍历全部 levels 取 playlist 详情的
// totalduration（event 型随转码推进增长），读不到再退回 seekable。
function hlsFrontierSec() {
  try {
    let best = 0
    for (const lv of hlsInst?.levels || []) {
      const d = lv?.details?.totalduration
      if (Number.isFinite(d) && d > best) best = d
    }
    if (best > 0) return best
  } catch { /* 详情未就绪 */ }
  return hlsSeekableEnd()
}

function armStallWatchdog() {
  disarmStallWatchdog()
  if (!state.open || state.error) return
  stallMark = bufferedEnd()
  stallTimer = setTimeout(() => {
    stallTimer = null
    if (!state.open || state.error) return
    if (bufferedEnd() > stallMark) { armStallWatchdog(); return }  // 有新数据：慢但在流
    if (!state.fellBack && (state.mode === 'direct' || state.mode === 'remux')
        && state.epoch === artEpoch) {
      ElMessage.info('视频加载停滞，自动切换转码播放')
      switchToFallback(0)
    } else if (!state.error) {
      state.error = '加载长时间无进展（网络慢或服务不可用），可稍后重试'
    }
  }, 25000)
}

function disarmStallWatchdog() {
  if (stallTimer) { clearTimeout(stallTimer); stallTimer = null }
}

// T2（ADR-027）：direct/remux 失败自动降级转码重试一次；仍失败再报分型错误
function onStreamError(code) {
  if (!state.fellBack && (state.mode === 'direct' || state.mode === 'remux')) {
    switchToFallback(code)
    return
  }
  state.error = mediaErrorMessage(code)
  ElMessage.error(`视频播放失败：${state.error}`)
}

function swapVideoSource() {
  // 换源统一入口：HLS 走 attachHls（原生 src 或 hls.js attachMedia），
  // 渐进流直接操作原生 video（与 seek 换流同一套路，move 全浏览器一致；
  // 不用 art.switchUrl——其内部状态与我们的原生事件钩子易互相干扰）
  const v = art?.video
  if (!v) return
  if (state.useHls) {
    attachHls(v, state.src)
  } else {
    v.src = state.src
    v.load()
    v.play().catch(() => {})
  }
}

function switchToFallback(code) {
  state.fellBack = true
  state.mode = 'transcode'
  state.epoch += 1
  artEpoch = state.epoch
  state.seeking = true
  state.error = ''
  ElMessage.info(code === 3 ? '浏览器无法解码该编码，切换转码播放' : '直出失败，切换转码播放')
  // 换流基准：源内容时刻 = timeBase + 当前播放位置（restamped 时间轴）
  const ss = timeBase + (art?.video?.currentTime || 0)
  timeBase = Math.max(0, ss)
  // R8：HLS 会话内降级 = 新 token + fallback=1（强制转码档）；渐进流会话
  // 降级时若具备 HLS 条件（移动端）顺带切换传输层——原渐进转码管道是同样
  // 的无 Range 传输，iOS 上降级了也播不动
  if (!state.useHls && state.isMobile && state.hlsBase) state.useHls = true
  const prevToken = hlsToken
  state.src = state.useHls
    ? buildHlsSrc({ ss, fallback: 1 })
    : buildSrc(state.baseSrc, { ss, fallback: true })
  stopHlsSession(prevToken)  // 旧会话不再需要：立即停旧 ffmpeg（新 token 会话顶上）
  swapVideoSource()
  armStallWatchdog()
}

function retry() {
  // 手动重试：回到初始源（保留降级态，避免再次走已证明失败的直出；
  // HLS 换新 token 起新会话——旧会话可能已死）
  state.error = ''
  state.epoch += 1
  artEpoch = state.epoch
  state.seeking = true
  const ss = (state.mode === 'transcode' && timeBase > 0) ? timeBase : 0
  const prevToken = hlsToken
  state.src = state.useHls
    ? buildHlsSrc({ ss, fallback: state.fellBack ? 1 : 0 })
    : buildSrc(state.baseSrc, { ss, fallback: state.fellBack })
  stopHlsSession(prevToken)
  swapVideoSource()
  armStallWatchdog()
}

function onSeeking() {
  const v = art?.video
  if (!v) return
  if (!(state.useHls || state.mode === 'transcode' || state.mode === 'remux')) return
  const t = v.currentTime
  if (t <= 0.05 || bufferedCovers(v, t)) return
  if (seekTimer) clearTimeout(seekTimer)  // 连续拖动只取最后落点
  seekTimer = setTimeout(() => {
    seekTimer = null
    if (state.epoch !== artEpoch) return  // 已换流：丢弃过期 seek
    // R7：叠加时间轴基准——`?ss=T` 起播的流 currentTime 从 0 计，拖到条上
    // 位置 t 对应源内容 timeBase+t（否则落点偏早 timeBase 秒）
    hotSwapTo(timeBase + t)
  }, 250)
}

// seek 热切换：丢弃当前流，带 ?ss=（源内容时刻）重新起流。
// timeBase 随之更新为新流的 currentTime=0 基准。
// R8：HLS 档 playlist 内可寻位置（seekable 覆盖，含转码已推进部分）交还
// 原生 seek——remux 会话秒级整文件就绪，实际远距跳转全走这条快路；
// 只有目标超出切片边界（转码未推进到）才换新会话。
function hotSwapTo(contentSec) {
  if (state.epoch !== artEpoch) return
  const v = art?.video
  if (state.useHls && v) {
    const t = contentSec - timeBase
    if (t <= hlsFrontierSec() + 0.5) {
      v.currentTime = Math.max(0, t)
      return
    }
  }
  state.seeking = true
  state.epoch += 1
  artEpoch = state.epoch
  const target = Math.max(0, contentSec)
  const prevToken = hlsToken
  state.src = state.useHls
    ? buildHlsSrc({ ss: target, fallback: state.fellBack ? 1 : 0 })
    : buildSrc(state.baseSrc, { ss: target, fallback: state.fellBack })
  stopHlsSession(prevToken)  // 快速拖动连发时立即回收上一个会话（不等服务端挤占）
  timeBase = target
  const url = state.src
  if (v) {
    v.pause()
    if (state.useHls) {
      attachHls(v, url)
    } else {
      v.src = url
      v.load()
      v.play().catch(() => {})
    }
  }
  armStallWatchdog()
}

function bufferedCovers(v, t, margin = 1.0) {
  const b = v.buffered
  for (let i = 0; i < b.length; i++) {
    if (t >= b.start(i) - margin && t <= b.end(i) + margin) return true
  }
  return false
}

function toggleFullscreen() {
  if (art) art.fullscreen = !art.fullscreen
}

function close(opts = {}) {
  const viaBack = !!opts.viaBack
  if (mountTimer) { clearTimeout(mountTimer); mountTimer = null }
  state.epoch += 1  // 关闭后一切回调失效
  artEpoch = state.epoch
  teardownPlayer()
  unlockPageScroll()
  disarmBackCapture(!viaBack)  // 返回键路径的占位已被 pop 消费；UI 内关闭需自己 back
  state.open = false
  state.src = ''
  state.baseSrc = ''
  state.hlsBase = ''
  state.mode = ''
  state.seeking = false
  state.startAt = 0
  state.error = ''
  state.fellBack = false
  state.useHls = false
}

onUnmounted(close)
defineExpose({ open })
</script>

<style>
/* R6：桌面播放器弹窗布局。R7：移动端改为专门全屏播放器（.mplayer*）。
   R8：mplayer 高度 100dvh（动态地址栏下与可视区精确匹配）+ body 滚动锁。 */
.player-dialog .el-dialog__body { padding-top: 10px; }
.player-dialog-body { display: flex; flex-direction: column; }
.mode-bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
.player-error { color: #f56c6c; margin-top: 8px; font-size: 13px; }

/* ---- R7 移动端专门全屏播放器 ---- */
.mplayer {
  position: fixed; inset: 0; z-index: 3000;
  display: flex; flex-direction: column;
  background: #000;
  overscroll-behavior: contain;
  /* R8：100dvh 精确匹配移动浏览器可视视口（100vh 会高出地址栏收起后的区域） */
  height: 100vh;
  height: 100dvh;
}
.mplayer-top {
  display: flex; align-items: center; gap: 10px;
  padding: calc(8px + env(safe-area-inset-top)) 12px 8px;
  background: #10141a; color: #fff; flex: none;
}
.mplayer-back {
  flex: none; width: 34px; height: 34px; border-radius: 50%;
  border: 0; background: rgba(255, 255, 255, .12); color: #fff;
  font-size: 22px; line-height: 30px; padding: 0 6px 4px 0; cursor: pointer;
}
.mplayer-back:active { background: rgba(255, 255, 255, .25); }
.mplayer-title {
  flex: 1; min-width: 0; font-size: 14px; color: #e5eaf3;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.mplayer-status {
  display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  padding: 6px 14px; background: #10141a; color: #c0c8d4;
  border-top: 1px solid rgba(255, 255, 255, .07); font-size: 12px; flex: none;
}
.mstat { padding: 1px 8px; border-radius: 9px; border: 1px solid rgba(255,255,255,.2); }
.mstat.ok { color: #95d475; border-color: rgba(149, 212, 117, .5); }
.mstat.info { color: #79bbff; border-color: rgba(121, 187, 255, .5); }
.mstat.warn { color: #eebe77; border-color: rgba(238, 190, 119, .5); }
.mstat.bad { color: #f89898; border-color: rgba(248, 152, 152, .5); }
.mstat.hls { color: #b48ef8; border-color: rgba(180, 142, 248, .5); }
.mplayer-stage { flex: 1 1 auto; min-height: 0; width: 100%; background: #000; }
.mplayer-error {
  flex: none; padding: 10px 14px calc(10px + env(safe-area-inset-bottom));
  background: #1a1a1a; color: #f56c6c; font-size: 13px;
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
}
.mplayer-retry {
  flex: none; border: 1px solid rgba(255, 255, 255, .3); border-radius: 6px;
  background: none; color: #fff; padding: 5px 16px; font-size: 13px; cursor: pointer;
}
/* R8：全屏播放器打开期间锁页面滚动——iOS Safari 对 overflow:hidden 免疫
   （页面照样能拖动），position:fixed + 负 top 才是可靠锁定，关闭时还原 */
body.mplayer-lock {
  position: fixed; left: 0; right: 0; width: 100%;
  overflow: hidden;
}
</style>
