<template>
  <div style="max-width:900px; margin:0 auto">
    <el-card shadow="never" style="margin-bottom:16px">
      <template #header>分析外部视频</template>
      <div style="font-size:12px; color:#909399; margin-bottom:10px">
        一次性分析打分，不会加入视频库或面容库
      </div>
      <el-upload drag :show-file-list="false" :http-request="doUpload"
                 accept="video/*,.mkv,.mp4,.avi,.mov,.ts,.wmv,.flv,.webm"
                 :disabled="!!running">
        <div style="padding:16px 0">
          <div style="font-size:14px">拖拽视频到此处，或点击选择文件</div>
          <div style="font-size:12px; color:#c0c4cc; margin-top:4px">
            大文件上传请保持页面打开；也可以把文件直接拷到投放目录 data/inbox/
          </div>
        </div>
      </el-upload>
      <el-progress v-if="uploadPct > 0 && uploadPct < 100" :percentage="uploadPct"
                   style="margin-top:10px" />
    </el-card>

    <el-card shadow="never" style="margin-bottom:16px">
      <template #header>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <span>投放目录待分析</span>
          <el-button size="small" @click="loadInbox">刷新</el-button>
        </div>
      </template>
      <el-empty v-if="!inbox.length" description="投放目录为空（data/inbox/）" :image-size="60" />
      <div v-else class="row-list">
        <div v-for="f in inbox" :key="f.filename" class="row-item">
          <div class="row-name">
            {{ f.filename }} <span style="color:#c0c4cc">（{{ fmtSize(f.size_bytes) }}）</span>
          </div>
          <el-button size="small" type="primary" :disabled="!!running"
                     @click="startAnalysis(f.filename)">分析</el-button>
        </div>
      </div>
    </el-card>

    <el-card v-if="running" shadow="never" style="margin-bottom:16px">
      <template #header>分析进行中</template>
      <el-progress :percentage="jobPct" :indeterminate="!running.total" :duration="2" />
      <div style="margin-top:6px; color:#909399; font-size:13px">
        抽帧并识别面容中…{{ running.total ? `（${running.done} / ${running.total} 帧）` : '' }}
      </div>
    </el-card>

    <el-card v-if="result" shadow="never" style="margin-bottom:16px">
      <template #header>
        <div style="display:flex; justify-content:space-between; align-items:center; gap:8px">
          <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap">分析结果 · {{ result.filename }}</span>
          <div style="flex-shrink:0">
            <el-button size="small" type="danger" plain @click="removeResult(result.token)">删除</el-button>
            <el-button size="small" @click="result = null">关闭</el-button>
          </div>
        </div>
      </template>
      <div style="display:flex; gap:14px; align-items:center; flex-wrap:wrap; margin-bottom:12px">
        <el-tag type="warning" effect="dark" size="large" style="font-size:16px">
          综合分 {{ result.final_score ?? '—' }}
        </el-tag>
        <el-tag type="info" effect="plain">最高 {{ result.max_score ?? '—' }}</el-tag>
        <el-tag type="info" effect="plain">面容 {{ result.n_faces }}</el-tag>
        <el-tag v-if="result.duration_sec" type="info" effect="plain">
          时长 {{ Math.round(result.duration_sec) }}s
        </el-tag>
        <el-tag type="info" effect="plain">抽样 {{ result.n_frames }} 帧</el-tag>
      </div>
      <el-empty v-if="!result.faces.length" description="未检测到有效面容" :image-size="60" />
      <div v-else class="face-grid">
        <div v-for="f in result.faces" :key="f.index" class="face-item">
          <img v-if="f.thumb" :src="f.thumb" loading="lazy" alt="" />
          <div class="face-meta">
            <el-tag size="small" :type="f.score >= 70 ? 'success' : f.score >= 40 ? 'warning' : 'info'"
                    effect="dark">{{ f.score ?? '—' }}</el-tag>
            <span style="color:#909399; font-size:12px">{{ fmtTime(f.timestamp_sec) }}</span>
          </div>
        </div>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <span>历史分析</span>
          <el-button size="small" @click="loadHistory">刷新</el-button>
        </div>
      </template>
      <el-empty v-if="!history.length" description="还没有分析记录" :image-size="60" />
      <div v-else class="row-list">
        <div v-for="h in history" :key="h.token" class="row-item">
          <div class="row-name">
            {{ h.filename }}
            <div style="color:#c0c4cc; font-size:12px">
              综合 {{ h.final_score ?? '—' }} · 面容 {{ h.n_faces }} · {{ h.created_at }}
            </div>
          </div>
          <div style="flex-shrink:0">
            <el-button size="small" @click="viewResult(h.token)">查看</el-button>
            <el-button size="small" type="danger" plain @click="removeResult(h.token)">删除</el-button>
          </div>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup>
// R6.3：外部视频一次性分析（不上视频库/面容库）。
// 双通道：页面拖拽上传（POST /api/analyze/upload 流式落盘）或直接拷文件到
// 投放目录 data/inbox/（Docker 部署即挂载卷 ./data/inbox）。
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errText } from '../api'

const inbox = ref([])
const history = ref([])
const result = ref(null)
const uploadPct = ref(0)
const running = ref(null)  // { id, done, total, status }
let timer = null

const jobPct = computed(() => (running.value && running.value.total
  ? Math.round(100 * running.value.done / running.value.total) : 0))

function fmtSize(n) {
  return n >= 1e9 ? (n / 1e9).toFixed(1) + ' GB'
    : n >= 1e6 ? (n / 1e6).toFixed(0) + ' MB' : (n / 1e3).toFixed(0) + ' KB'
}

function fmtTime(sec) {
  if (sec == null) return '—'
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

async function loadInbox() {
  try { inbox.value = (await api.get('/analyze/inbox')).data.items }
  catch (e) { ElMessage.error(errText(e)) }
}

async function loadHistory() {
  try { history.value = (await api.get('/analyze/list')).data.items }
  catch { /* 忽略 */ }
}

async function doUpload({ file }) {
  uploadPct.value = 1
  const fd = new FormData()
  fd.append('file', file)
  try {
    const r = await api.post('/analyze/upload', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 0,  // 大文件上传不限时
      onUploadProgress: (e) => { if (e.total) uploadPct.value = Math.max(1, Math.round(100 * e.loaded / e.total)) },
    })
    uploadPct.value = 0
    ElMessage.success('上传完成，开始分析')
    await startAnalysis(r.data.filename)
    await loadInbox()
  } catch (e) {
    uploadPct.value = 0
    ElMessage.error(errText(e))
  }
}

async function startAnalysis(filename) {
  try {
    const r = await api.post('/analyze/start', { filename })
    running.value = { id: r.data.job.id, done: 0, total: 0, status: r.data.job.status }
    ElMessage.success(`分析任务 #${r.data.job.id} 已提交`)
    pollOnce()
  } catch (e) { ElMessage.error(errText(e)) }
}

async function pollOnce() {
  if (!running.value) return
  try {
    const b = (await api.get(`/jobs/${running.value.id}`)).data
    running.value = { id: b.id, done: b.done, total: b.total, status: b.status }
    if (b.status === 'done') {
      const token = b.result?.token
      running.value = null
      await loadHistory()
      if (token) await viewResult(token)
    } else if (b.status === 'failed') {
      ElMessage.error((b.error || '分析失败').slice(0, 160))
      running.value = null
    } else if (b.status === 'cancelled' || b.status === 'paused') {
      running.value = null
    }
  } catch { /* 下个周期重试 */ }
}

async function viewResult(token) {
  try { result.value = (await api.get(`/analyze/${token}`)).data }
  catch (e) { ElMessage.error(errText(e)) }
}

async function removeResult(token) {
  try {
    await api.delete(`/analyze/${token}`)
    if (result.value?.token === token) result.value = null
    await loadHistory()
    await loadInbox()
  } catch (e) { ElMessage.error(errText(e)) }
}

onMounted(async () => {
  await Promise.all([loadInbox(), loadHistory()])
  timer = setInterval(pollOnce, 2000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style>
/* R6.3 分析页：待分析行列表 + 结果面容网格（自适应列数） */
.row-list { display: flex; flex-direction: column; gap: 8px; }
.row-item {
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
  padding: 8px 10px; border: 1px solid #ebeef5; border-radius: 6px;
}
.row-name { font-size: 13px; color: #303133; word-break: break-all; min-width: 0; }
.face-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px;
}
.face-item img {
  width: 100%; display: block; border-radius: 8px; background: #f5f7fa; aspect-ratio: 3/4; object-fit: cover;
}
.face-meta { margin-top: 4px; display: flex; align-items: center; justify-content: space-between; gap: 6px; }
</style>
