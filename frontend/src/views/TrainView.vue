<template>
  <div style="max-width:960px; margin:0 auto">
    <el-card shadow="never" style="margin-bottom:16px">
      <template #header>训练</template>
      <div style="display:flex; gap:14px; align-items:center; flex-wrap:wrap">
        <el-button type="primary" :loading="training" @click="startTrain">
          用我的评分与对比训练个性化打分器
        </el-button>
        <el-tag v-if="status.active_version" type="success" effect="dark">
          当前启用：{{ status.active_version }}
        </el-tag>
        <el-tag v-else type="info" effect="plain">未启用个性化模型（使用基础分）</el-tag>
        <el-button v-if="status.active_version" size="small" :disabled="activating"
                   @click="deactivate">停用（回退基础分）</el-button>
      </div>
      <div style="margin-top:10px; color:#909399; font-size:13px">
        累计 ≥20 条评分/对比才可训练；每新增 30 条自动训练一次（需手动启用新版本）。
      </div>
      <el-alert v-if="lastJob && lastJob.status === 'failed'" type="error" :closable="false"
                style="margin-top:10px" :title="'上次训练失败：' + (lastJob.error || '').slice(0, 160)" />
      <!-- R14：启用/停用为后台任务（interactive 池），大库上分钟级——进度横幅 -->
      <div v-if="activating" style="margin-top:10px">
        <el-progress :percentage="activatePct"
                     :indeterminate="!activateProgress.total" :duration="2" />
        <div style="margin-top:4px; color:#909399; font-size:12px">
          {{ activateProgress.detail || '排队中…' }}
          <template v-if="activateProgress.total">
            （{{ activateProgress.done }} / {{ activateProgress.total }}）
          </template>
        </div>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <span>模型版本</span>
          <div style="display:flex; gap:8px; align-items:center">
            <el-upload :show-file-list="false" :http-request="doImport" accept=".zip"
                       :disabled="importing">
              <el-button size="small" :loading="importing">导入</el-button>
            </el-upload>
            <el-button size="small" @click="load">刷新</el-button>
          </div>
        </div>
      </template>
      <div style="margin-bottom:8px; color:#909399; font-size:12px">
        导出 = 下载该版本的偏好模型（几 KB）；导入后出现在版本列表中，需手动"启用"才生效。
      </div>
      <el-empty v-if="!versions.length" description="还没有训练过模型" :image-size="60" />
      <div v-else class="mobile-scroll">
        <el-table :data="versions" size="small">
          <el-table-column prop="version" label="版本" width="80" />
          <el-table-column prop="n_abs" label="绝对评分数" width="110" />
          <el-table-column prop="n_pair" label="对比数" width="90" />
          <el-table-column prop="mae_train" label="MAE(训练)" width="110" />
          <el-table-column prop="r2_train" label="R²(训练)" width="100" />
          <el-table-column prop="pair_acc" label="对比准确率" width="110" />
          <el-table-column prop="created_at" label="训练时间" width="170" />
          <el-table-column label="操作" width="200">
            <template #default="{ row }">
              <el-button v-if="row.active" size="small" type="success" plain disabled>已启用</el-button>
              <el-button v-else size="small" type="primary" :disabled="activating"
                         @click="activate(row.version)">启用</el-button>
              <el-button size="small" @click="exportVersion(row.version)">导出</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errText } from '../api'

const status = ref({ active_version: null, last_job: null })
const versions = ref([])
const training = ref(false)
const importing = ref(false)
// R14：启用/停用为 interactive 池后台任务——点击后立即得到响应，进度在此展示
const activating = ref(false)
const activateProgress = ref({ done: 0, total: 0, detail: '' })
let activateTimer = null

const activatePct = computed(() => (activateProgress.value.total
  ? Math.round(100 * activateProgress.value.done / activateProgress.value.total)
  : 0))

async function load() {
  try {
    status.value = (await api.get('/train/status')).data
    versions.value = (await api.get('/train/versions')).data.versions
  } catch (e) {
    ElMessage.error(errText(e))
  }
}

// 页面刷新后恢复进行中的启用/停用任务
async function restoreActivateJob() {
  try {
    const r = await api.get('/jobs', { params: { active: true, limit: 20 } })
    const job = r.data.items.find(item =>
      (item.type === 'activate' || item.type === 'deactivate')
      && (item.status === 'queued' || item.status === 'running'))
    if (job) watchActivateJob(job.id)
  } catch { /* 忽略 */ }
}

function watchActivateJob(jobId) {
  activating.value = true
  activateProgress.value = { done: 0, total: 0, detail: '' }
  if (activateTimer) clearInterval(activateTimer)
  activateTimer = setInterval(async () => {
    try {
      const b = (await api.get(`/jobs/${jobId}`)).data
      activateProgress.value = {
        done: b.done, total: b.total, detail: b.detail || '',
      }
      if (b.status === 'done') {
        stopWatchActivate()
        const res = b.result || {}
        ElMessage.success(res.videos_recomputed != null
          ? `完成，已重算 ${res.videos_recomputed} 个视频的综合分`
          : '完成')
        await load()
      } else if (b.status === 'failed') {
        stopWatchActivate()
        ElMessage.error(('启用/停用失败：' + (b.error || '')).slice(0, 160))
      } else if (b.status === 'cancelled' || b.status === 'paused') {
        stopWatchActivate()
      }
    } catch { /* 下个周期重试 */ }
  }, 1000)
}

function stopWatchActivate() {
  if (activateTimer) { clearInterval(activateTimer); activateTimer = null }
  activating.value = false
}

async function startTrain() {
  training.value = true
  try {
    const r = await api.post('/train/start')
    ElMessage.success(`训练任务 #${r.data.job.id} 已提交`)
    // 轮询直至结束
    for (let i = 0; i < 100; i++) {
      await new Promise(res => setTimeout(res, 500))
      const body = (await api.get(`/jobs/${r.data.job.id}`)).data
      if (body.status === 'done') {
        await load()
        // R14.2：训练产物必须可见——job 结果里有版本号而列表缺失 = 环境/挂载问题，明示而非静默
        const v = body.result?.version
        if (v && !versions.value.some(x => x.version === v)) {
          ElMessage.warning(`已训练 ${v}，但未出现在版本列表——请检查 data/models 卷挂载与容器日志`)
        } else {
          ElMessage.success(`训练完成（${v || '新版本'}），可在下方启用新版本`)
        }
        break
      }
      if (body.status === 'failed') { ElMessage.error((body.error || '').slice(0, 160)); break }
    }
    await load()
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    training.value = false
  }
}

// R14：启用/停用走后台任务（interactive 池）——点击立刻得到响应（202 + job），
// 全库个性化分应用与聚合重算的进度在上方横幅展示。
async function activate(version) {
  if (activating.value) return
  try {
    const r = await api.post(`/train/activate/${version}`)
    ElMessage.success('启用任务已提交，正在应用个性化分…')
    watchActivateJob(r.data.job.id)
  } catch (e) {
    ElMessage.error(errText(e))
  }
}

async function deactivate() {
  if (activating.value) return
  try {
    const r = await api.post('/train/deactivate')
    ElMessage.success('停用任务已提交，正在回退基础分…')
    watchActivateJob(r.data.job.id)
  } catch (e) {
    ElMessage.error(errText(e))
  }
}

// R11：偏好模型导出/导入。导出 = 单个 zip（模型参数 + 训练指标，几 KB），
// 导入 = 落盘为新版本（冲突自动重编号），不自动启用。
async function exportVersion(version) {
  try {
    const r = await api.get(`/train/versions/${version}/export`, { responseType: 'blob' })
    const url = URL.createObjectURL(r.data)
    const a = document.createElement('a')
    a.href = url
    a.download = `sophos-personalizer-${version}.zip`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    ElMessage.error(errText(e))
  }
}

async function doImport({ file }) {
  importing.value = true
  const fd = new FormData()
  fd.append('file', file)
  try {
    const r = await api.post('/train/import', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    ElMessage.success(`已导入为 ${r.data.meta.version}，请在列表中手动启用`)
    await load()
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    importing.value = false
  }
}

onMounted(async () => {
  await Promise.all([load(), restoreActivateJob()])
})
onUnmounted(stopWatchActivate)
</script>
