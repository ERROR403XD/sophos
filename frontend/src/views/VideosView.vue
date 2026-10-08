<template>
  <div>
    <div style="margin-bottom:12px; display:flex; gap:12px; align-items:center; flex-wrap:wrap">
      <el-input v-model="q" placeholder="按文件名搜索" clearable :style="ui.isMobile ? 'width:100%' : 'width:220px'"
                @input="queueSearch" />
      <el-select v-model="library" clearable filterable placeholder="库（工作目录）"
                 :style="ui.isMobile ? 'width:100%' : 'width:260px'" @change="applyFilter">
        <el-option v-for="d in libraryOptions" :key="d" :label="baseName(d)" :value="d" />
      </el-select>
      <el-select v-model="status" clearable placeholder="状态" style="width:130px" @change="applyFilter">
        <el-option v-for="s in ['done', 'pending', 'processing', 'failed', 'missing']"
                   :key="s" :label="s" :value="s" />
      </el-select>
      <span style="color:#909399">共 {{ total }} 个视频</span>
    </div>

    <!-- 桌面：表格 -->
    <el-table v-if="!ui.isMobile" :data="items" v-loading="loading" @row-click="open"
              style="cursor:pointer" size="large">
      <el-table-column label="视频" prop="filename" min-width="220" sortable>
        <template #default="{ row }">
          <el-tooltip :content="row.path" placement="top" :show-after="300">
            <span>{{ row.filename }}</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="综合分" prop="final_score" width="110" sortable>
        <template #default="{ row }">
          <el-tag v-if="row.final_score != null" :type="scoreType(row.final_score)"
                  effect="dark">{{ row.final_score }}</el-tag>
          <span v-else style="color:#c0c4cc">未处理</span>
        </template>
      </el-table-column>
      <el-table-column label="基础分" prop="base_final" width="100" sortable />
      <el-table-column label="个性化分" prop="personalized_final" width="110" sortable />
      <el-table-column label="面容数" prop="identity_count" width="90" />
      <el-table-column label="状态" prop="status" width="100">
        <template #default="{ row }">
          <el-tag :type="row.status === 'done' ? 'success' : row.status === 'failed' ? 'danger' : 'info'"
                  effect="plain">{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="时长" prop="duration_sec" width="80">
        <template #default="{ row }">{{ row.duration_sec ? Math.round(row.duration_sec) + 's' : '—' }}</template>
      </el-table-column>
      <el-table-column label="播放" prop="stream_mode" width="90">
        <template #default="{ row }">
          <el-tag v-if="row.stream_mode === 'direct'" type="success" effect="plain">直出</el-tag>
          <el-tag v-else-if="row.stream_mode === 'remux'" type="primary" effect="plain">重封装</el-tag>
          <el-tag v-else-if="row.stream_mode === 'transcode'" type="warning" effect="plain">转码</el-tag>
          <el-tag v-else type="info" effect="plain">不支持</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="面容评分" width="180" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click.stop="openFaces(row)">面容/整体评分</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- R6 移动端：卡片列表（表格在窄屏无法用） -->
    <div v-else v-loading="loading" class="video-cards">
      <el-empty v-if="!items.length && !loading" description="没有视频" :image-size="60" />
      <el-card v-for="row in items" :key="row.id" shadow="hover" class="video-card"
               @click="open(row)">
        <div class="video-card-title">{{ row.filename }}</div>
        <div class="video-card-tags">
          <el-tag v-if="row.final_score != null" :type="scoreType(row.final_score)" effect="dark">
            综合 {{ row.final_score }}
          </el-tag>
          <el-tag v-if="row.base_final != null" type="info" effect="plain">基础 {{ row.base_final }}</el-tag>
          <el-tag v-if="row.personalized_final != null" type="warning" effect="plain">个性 {{ row.personalized_final }}</el-tag>
          <el-tag :type="row.status === 'done' ? 'success' : row.status === 'failed' ? 'danger' : 'info'" effect="plain">
            {{ row.status }}
          </el-tag>
          <el-tag v-if="row.duration_sec" type="info" effect="plain">{{ Math.round(row.duration_sec) }}s</el-tag>
          <el-tag v-if="row.stream_mode === 'direct'" type="success" effect="plain">直出</el-tag>
          <el-tag v-else-if="row.stream_mode === 'remux'" type="primary" effect="plain">重封装</el-tag>
          <el-tag v-else-if="row.stream_mode === 'transcode'" type="warning" effect="plain">转码</el-tag>
        </div>
        <div class="video-card-actions" @click.stop>
          <el-button size="small" @click="openFaces(row)">面容/整体评分</el-button>
        </div>
      </el-card>
    </div>

    <!-- R10：移动端收窄布局（prev/pager/next），总数已在筛选行展示。组件库
         .el-pagination 默认不换行，"sizes+total" 在窄屏必然把文档撑宽——页面
         变得可横向拖动，fixed 定位的底部 TabBar 随之显示不全（用户报障）。 -->
    <el-pagination :style="ui.isMobile ? 'margin-top:14px; justify-content:center' : 'margin-top:14px; justify-content:flex-end'"
      :layout="ui.isMobile ? 'prev, pager, next' : 'prev, pager, next, sizes, total'"
      :pager-count="ui.isMobile ? 5 : 7" :small="ui.isMobile"
      :total="total" v-model:current-page="page" v-model:page-size="pageSize"
      :page-sizes="[20, 50, 100]" @current-change="load" @size-change="load" />

    <PlayerDialog ref="playerRef" />
    <FaceRatingDrawer ref="faceDrawerRef" :video="faceDrawerVideo" />
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errText } from '../api'
import { ui } from '../ui'
import PlayerDialog from '../components/PlayerDialog.vue'
import FaceRatingDrawer from '../components/FaceRatingDrawer.vue'

const items = ref([])
const total = ref(0)
const loading = ref(false)
const q = ref('')
const library = ref('')
const status = ref('')
const page = ref(1)
const pageSize = ref(20)
const playerRef = ref(null)
const workdirs = ref([])
const dbLibraries = ref([])
const faceDrawerVideo = ref(null)
const faceDrawerRef = ref(null)
let searchTimer = null
let requestSeq = 0

// 库下拉 = 当前工作目录 ∪ DB 中历史库（删除库后历史视频仍可筛）
const libraryOptions = computed(() => {
  const set = new Set([...workdirs.value, ...dbLibraries.value])
  return [...set]
})

function baseName(p) {
  if (!p) return ''
  const parts = String(p).replace(/\\/g, '/').split('/').filter(Boolean)
  return parts[parts.length - 1] || p
}

function scoreType(s) {
  return s >= 70 ? 'success' : s >= 40 ? 'warning' : 'info'
}

async function loadLibraries() {
  try {
    workdirs.value = (await api.get('/workdirs')).data.workdirs || []
  } catch { /* 忽略 */ }
  try {
    dbLibraries.value = (await api.get('/videos/libraries')).data.libraries || []
  } catch { /* 忽略 */ }
}

async function load() {
  const seq = ++requestSeq
  loading.value = true
  try {
    const r = await api.get('/videos', {
      params: { page: page.value, page_size: pageSize.value, q: q.value || undefined,
                library: library.value || undefined,
                status: status.value || undefined, sort: 'final_score', order: 'desc' }
    })
    if (seq !== requestSeq) return
    items.value = r.data.items
    total.value = r.data.total
  } catch (e) {
    if (seq === requestSeq) ElMessage.error(errText(e))
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

function queueSearch() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    searchTimer = null
    page.value = 1
    load()
  }, 300)
}

function cancelSearch() {
  if (searchTimer) {
    clearTimeout(searchTimer)
    searchTimer = null
  }
  requestSeq += 1
}

function applyFilter() {
  cancelSearch()
  page.value = 1
  load()
}

function open(row) {
  if (row.stream_mode === 'unsupported') {
    ElMessage.warning('该视频编码/格式不支持在线播放（容器探测失败或转码已关闭）')
    return
  }
  // R5.2：播放器统一为 PlayerDialog（ArtPlayer 内核，评分/对比页共用）
  // R8：hls_url 一并下发，移动端播放器据此走 HLS 会话传输
  // R10：duration（DB 实测时长）一并下发——HLS event 流/渐进 fMP4 的
  // video.duration 只有已缓冲部分，播放器据此把进度条总长钉在真实时长上
  playerRef.value?.open({ title: row.filename, stream_url: row.stream_url,
                          hls_url: row.hls_url, stream_mode: row.stream_mode,
                          duration: row.duration_sec })
}

function openFaces(row) {
  faceDrawerVideo.value = row
  faceDrawerRef.value?.open()
}

onMounted(() => { loadLibraries(); load() })
onUnmounted(cancelSearch)
</script>

<style>
/* R6 移动端：视频卡片列表 */
.video-cards { display: flex; flex-direction: column; gap: 8px; min-height: 120px; }
.video-card { --el-card-padding: 10px 12px; cursor: pointer; }
.video-card-title {
  font-size: 14px; font-weight: 600; color: #303133;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.video-card-tags { margin-top: 6px; display: flex; gap: 6px; flex-wrap: wrap; }
.video-card-actions { margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; }
.video-card-actions .el-button + .el-button { margin-left: 0; }
@media (max-width: 768px) {
  .video-card-actions .el-button { width: 100%; }
}
</style>
