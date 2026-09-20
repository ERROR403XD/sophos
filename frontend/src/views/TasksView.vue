<template>
  <div style="max-width:900px; margin:0 auto">
    <el-card shadow="never" style="margin-bottom:16px">
      <template #header>工作目录</template>
      <div style="display:flex; gap:10px; margin-bottom:12px">
        <el-input v-model="newDir" placeholder="输入视频目录绝对路径，如 E:\videos"
                  @keyup.enter="addDir" />
        <el-button type="primary" @click="addDir">添加</el-button>
      </div>
      <el-tag v-for="d in workdirs" :key="d" closable style="margin:0 8px 8px 0"
              @close="removeDir(d)">{{ d }}</el-tag>
      <el-empty v-if="!workdirs.length" description="尚未添加工作目录" :image-size="60" />
    </el-card>

    <el-card shadow="never" style="margin-bottom:16px">
      <template #header>扫描与处理</template>
      <div style="display:flex; gap:12px; flex-wrap:wrap; align-items:center">
        <el-button type="primary" :loading="starting" @click="runJob('/scan/start')">① 增量扫描目录</el-button>
        <el-button type="success" :loading="starting" @click="runJob('/process/start')">② 处理面容</el-button>
      </div>
    </el-card>

    <el-card shadow="never" style="margin-bottom:16px">
      <template #header>设置</template>
      <div style="display:flex; gap:24px; flex-wrap:wrap; align-items:center">
        <div>
          <div style="font-size:13px; color:#606266; margin-bottom:4px">处理分批大小（个/批）</div>
          <el-input-number v-model="runtimeSettings.process_batch_size" :min="1" :max="50" />
        </div>
        <div>
          <div style="font-size:13px; color:#606266; margin-bottom:4px">
            同一视频同一人最多保留面容数（0 = 不限制）
          </div>
          <el-input-number v-model="runtimeSettings.max_faces_per_person" :min="0" :max="500" />
        </div>
        <div>
          <div style="font-size:13px; color:#606266; margin-bottom:4px">自动训练触发（新增评分/对比条数，0 = 关闭）</div>
          <el-input-number v-model="runtimeSettings.auto_train_every" :min="0" :max="100000" />
        </div>
      </div>
      <el-divider style="margin:14px 0" />
      <div style="display:flex; gap:24px; flex-wrap:wrap; align-items:center">
        <div>
          <div style="font-size:13px; color:#606266; margin-bottom:4px">自动扫描工作目录</div>
          <el-switch v-model="runtimeSettings.auto_scan_enabled" />
        </div>
        <div>
          <div style="font-size:13px; color:#606266; margin-bottom:4px">每天扫描时刻</div>
          <el-time-picker v-model="runtimeSettings.auto_scan_time" value-format="HH:mm"
                          format="HH:mm" :disabled="!runtimeSettings.auto_scan_enabled"
                          style="width:120px" placeholder="时刻" />
        </div>
        <div>
          <div style="font-size:13px; color:#606266; margin-bottom:4px">自动处理面容（发现新视频即处理，可与自动扫描联动）</div>
          <el-switch v-model="runtimeSettings.auto_process_enabled" />
        </div>
        <el-button type="primary" :loading="savingSettings" @click="saveSettings">保存设置</el-button>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <span>任务状态</span>
          <el-button size="small" @click="loadJobs">手动刷新</el-button>
        </div>
      </template>

      <!-- 桌面：表格 -->
      <el-table v-if="!ui.isMobile" :data="jobs" size="small">
        <el-table-column prop="id" label="#" width="60" />
        <el-table-column prop="type" label="类型" width="90" />
        <el-table-column prop="status" label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" effect="plain">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="160">
          <template #default="{ row }">
            <el-progress v-if="row.total" :percentage="Math.round(100 * row.done / row.total)" />
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="结果 / 错误" min-width="220">
          <template #default="{ row }">
            <template v-if="row.result || row.error">
              <span v-if="row.result" style="color:#909399; font-size:12px">{{ resultSummary(row) }}</span>
              <span v-else style="color:#f56c6c; font-size:12px">{{ row.error.slice(0, 60) }}…</span>
              <el-button size="small" text type="primary" style="margin-left:6px"
                         @click="showDetail(row)">详情</el-button>
            </template>
            <span v-else style="color:#c0c4cc">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <template v-if="row.status === 'queued' || row.status === 'running'">
              <el-button size="small" @click="control(row.id, 'pause')">暂停</el-button>
              <el-button size="small" type="danger" plain @click="control(row.id, 'cancel')">取消</el-button>
            </template>
            <template v-else-if="row.status === 'paused'">
              <el-button size="small" type="primary" @click="control(row.id, 'resume')">恢复</el-button>
              <el-button size="small" type="danger" plain @click="control(row.id, 'cancel')">取消</el-button>
            </template>
            <span v-else style="color:#c0c4cc">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="170" />
      </el-table>

      <!-- R6 移动端：任务卡片 -->
      <div v-else class="job-cards">
        <el-empty v-if="!jobs.length" description="暂无任务" :image-size="60" />
        <el-card v-for="row in jobs" :key="row.id" shadow="never" class="job-card">
          <div class="job-card-head">
            <span class="job-card-title">#{{ row.id }} · {{ jobTypeName(row.type) }}</span>
            <el-tag size="small" :type="statusTagType(row.status)" effect="plain">{{ row.status }}</el-tag>
          </div>
          <el-progress v-if="row.total" :percentage="Math.round(100 * row.done / row.total)" />
          <div v-if="row.result" class="job-card-line">结果：{{ resultSummary(row) }}
            <el-button size="small" text type="primary" style="padding:0"
                       @click="showDetail(row)">详情</el-button>
          </div>
          <div v-else-if="row.error" class="job-card-line" style="color:#f56c6c">
            {{ row.error.slice(0, 60) }}…
            <el-button size="small" text type="primary" style="padding:0"
                       @click="showDetail(row)">详情</el-button>
          </div>
          <div class="job-card-line" style="color:#c0c4cc">{{ row.created_at }}</div>
          <div v-if="['queued', 'running', 'paused'].includes(row.status)" style="margin-top:6px">
            <el-button v-if="row.status !== 'paused'" size="small" @click="control(row.id, 'pause')">暂停</el-button>
            <el-button v-if="row.status === 'paused'" size="small" type="primary" @click="control(row.id, 'resume')">恢复</el-button>
            <el-button size="small" type="danger" plain @click="control(row.id, 'cancel')">取消</el-button>
          </div>
        </el-card>
      </div>
    </el-card>

    <!-- 任务详情弹窗：完整参数 / 结果 JSON / 错误栈 -->
    <el-dialog v-model="detail.open" :title="detail.job ? `任务 #${detail.job.id} 详情` : '任务详情'"
               :width="ui.isMobile ? '94%' : '720px'" top="6vh" destroy-on-close>
      <template v-if="detail.job">
        <el-descriptions :column="ui.isMobile ? 1 : 2" size="small" border>
          <el-descriptions-item label="任务">
            #{{ detail.job.id }} · {{ jobTypeName(detail.job.type) }}
          </el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag size="small" :type="statusTagType(detail.job.status)" effect="plain">{{ detail.job.status }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="进度">{{ detail.job.done }} / {{ detail.job.total || '—' }}</el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ detail.job.created_at }}</el-descriptions-item>
          <el-descriptions-item label="开始时间">{{ detail.job.started_at || '—' }}</el-descriptions-item>
          <el-descriptions-item label="结束时间">{{ detail.job.finished_at || '—' }}</el-descriptions-item>
          <el-descriptions-item label="参数" :span="ui.isMobile ? 1 : 2">
            <pre class="detail-pre">{{ pretty(detail.job.params) }}</pre>
          </el-descriptions-item>
        </el-descriptions>
        <template v-if="detail.job.result">
          <div class="detail-label">结果</div>
          <pre class="detail-pre">{{ pretty(detail.job.result) }}</pre>
        </template>
        <template v-if="detail.job.error">
          <div class="detail-label">错误</div>
          <pre class="detail-pre detail-pre-error">{{ detail.job.error }}</pre>
        </template>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errText } from '../api'
import { ui } from '../ui'

const workdirs = ref([])
const newDir = ref('')
const jobs = ref([])
const starting = ref(false)
const savingSettings = ref(false)
const runtimeSettings = reactive({
  process_batch_size: 4, max_faces_per_person: 5, auto_train_every: 30,
  auto_scan_enabled: false, auto_scan_time: "03:00", auto_process_enabled: false,
})
let timer = null

const JOB_TYPE_NAMES = { scan: '扫描', process: '面容处理', train: '训练', analyze: '外部分析' }
function jobTypeName(t) { return JOB_TYPE_NAMES[t] || t }

// R6.1：结果/错误详情弹窗（列表内只留一行摘要，完整 JSON/错误栈点开看）
const detail = reactive({ open: false, job: null })

function showDetail(row) {
  detail.job = row
  detail.open = true
}

function pretty(v) {
  if (v == null) return '—'
  return typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v)
}

// 一行摘要：顶层标量字段拼 "键=值"（process→ok=4 · failed=0 …，scan/train 同理）；
// 无标量字段时退化为截断 JSON
function resultSummary(row) {
  const r = row.result
  if (r == null) return ''
  if (typeof r !== 'object') return String(r).slice(0, 80)
  const kv = Object.entries(r)
    .filter(([, v]) => v == null || ['string', 'number', 'boolean'].includes(typeof v))
    .map(([k, v]) => `${k}=${v}`)
  const s = kv.length ? kv.join(' · ') : JSON.stringify(r)
  return s.length > 80 ? s.slice(0, 80) + '…' : s
}

function statusTagType(s) {
  if (s === 'done') return 'success'
  if (s === 'failed') return 'danger'
  if (s === 'running') return 'warning'
  return 'info'
}

async function loadDirs() {
  workdirs.value = (await api.get('/workdirs')).data.workdirs
}

async function addDir() {
  if (!newDir.value.trim()) return
  try {
    await api.post('/workdirs', { path: newDir.value.trim() })
    newDir.value = ''
    await loadDirs()
  } catch (e) {
    ElMessage.error(errText(e))
  }
}

async function removeDir(d) {
  try {
    await api.delete('/workdirs', { params: { path: d } })
    await loadDirs()
  } catch (e) {
    ElMessage.error(errText(e))
  }
}

async function loadSettings() {
  try {
    Object.assign(runtimeSettings, (await api.get('/settings')).data.settings)
  } catch { /* 保持默认 */ }
}

async function saveSettings() {
  savingSettings.value = true
  try {
    Object.assign(runtimeSettings,
      (await api.put('/settings', { ...runtimeSettings })).data.settings)
    ElMessage.success('设置已保存')
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    savingSettings.value = false
  }
}

async function runJob(url) {
  starting.value = true
  try {
    const r = await api.post(url)
    ElMessage.success(`任务 #${r.data.job.id} 已提交`)
    await loadJobs()
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    starting.value = false
  }
}

async function control(jobId, action) {
  try {
    const r = await api.post(`/jobs/${jobId}/${action}`)
    if (r.data.action === 'requested') {
      ElMessage.info('将在当前步骤结束后生效')
    }
    await loadJobs()
  } catch (e) {
    ElMessage.error(errText(e))
  }
}

async function loadJobs() {
  try {
    jobs.value = (await api.get('/jobs', { params: { limit: 15 } })).data.items
  } catch { /* 忽略轮询失败 */ }
}

onMounted(async () => {
  await loadDirs()
  await loadSettings()
  await loadJobs()
  timer = setInterval(loadJobs, 2000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style>
/* R6 移动端：任务卡片列表 */
.job-cards { display: flex; flex-direction: column; gap: 8px; }
.job-card { --el-card-padding: 10px 12px; }
.job-card-head { display: flex; justify-content: space-between; align-items: center; }
.job-card-title { font-size: 13px; font-weight: 600; color: #303133; }
.job-card-line { margin-top: 4px; font-size: 12px; color: #606266; word-break: break-all; }

/* R6.1：任务详情弹窗 */
.detail-label { margin-top: 12px; font-size: 13px; font-weight: 600; color: #606266; }
.detail-pre {
  margin: 6px 0 0; padding: 10px; max-height: 300px; overflow: auto;
  background: #f5f7fa; border-radius: 6px; font-size: 12px; line-height: 1.6;
  white-space: pre-wrap; word-break: break-all;
}
.detail-pre-error { background: #fef0f0; color: #c45656; }
</style>
