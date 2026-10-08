<template>
  <el-drawer v-model="visible" :size="ui.isMobile ? '100%' : '760px'"
             :with-header="true" destroy-on-close class="video-face-drawer"
             @open="onOpen" @closed="onClosed">
    <template #header>
      <div class="face-drawer-title">{{ video?.filename || '视频面容' }}</div>
    </template>
    <div class="face-drawer-body">
      <div class="face-toolbar">
        <span>共 {{ total }} 个面容 · 逐个评分只影响对应面容</span>
        <div>
          <el-button size="small" type="success" plain :disabled="!total || bulkRating"
                     @click="rateAll('up')">整体好评</el-button>
          <el-button size="small" type="danger" plain :disabled="!total || bulkRating"
                     @click="rateAll('down')">整体差评</el-button>
        </div>
      </div>

      <div v-loading="loading" class="face-grid">
        <el-empty v-if="!items.length && !loading" description="该视频暂无提取面容"
                  :image-size="70" />
        <el-card v-for="face in items" :key="face.id" shadow="never" class="face-card">
          <img :src="face.rep_thumb" class="face-thumb" loading="lazy" alt="面容缩略图" />
          <div class="face-meta">
            <el-tag size="small" type="info" effect="plain">基础 {{ face.base_score ?? '—' }}</el-tag>
            <el-tag v-if="face.personalized_score != null" size="small" type="warning"
                    effect="plain">个性 {{ face.personalized_score }}</el-tag>
            <el-tag size="small" type="info" effect="plain">{{ face.n_samples }} 帧</el-tag>
            <el-tag v-if="face.my_rating" size="small" type="success" effect="plain">
              已评 {{ face.my_rating }}
            </el-tag>
          </div>
          <div class="score-row">
            <el-button v-for="n in 10" :key="n" size="small"
                       :disabled="ratingFaceId === face.id"
                       @click="rateFace(face, 'score', n)">{{ n }}</el-button>
          </div>
          <div class="thumb-row">
            <el-button size="small" type="success" :disabled="ratingFaceId === face.id"
                       @click="rateFace(face, 'thumbs', 'up')">👍 好评</el-button>
            <el-button size="small" type="danger" :disabled="ratingFaceId === face.id"
                       @click="rateFace(face, 'thumbs', 'down')">👎 差评</el-button>
          </div>
        </el-card>
      </div>

      <el-pagination v-if="total > pageSize" small layout="prev, pager, next"
                     :total="total" :page-size="pageSize"
                     v-model:current-page="page" @current-change="loadFaces" />
    </div>
  </el-drawer>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, errText } from '../api'
import { ui } from '../ui'

const props = defineProps({
  video: { type: Object, default: null },
})
const emit = defineEmits(['rated'])

const visible = ref(false)
const items = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 12
const loading = ref(false)
const bulkRating = ref(false)
const ratingFaceId = ref(null)
const currentVideoId = computed(() => props.video?.id ?? null)

async function loadFaces() {
  if (currentVideoId.value == null) return
  const seq = ++requestSeq
  loading.value = true
  try {
    const response = await api.get('/faces', {
      params: { page: page.value, page_size: pageSize, video_id: currentVideoId.value, order: 'id' }
    })
    if (seq !== requestSeq) return
    items.value = response.data.items
    total.value = response.data.total
  } catch (error) {
    if (seq === requestSeq) ElMessage.error(errText(error))
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

async function rateAll(verdict) {
  if (!props.video) return
  const count = props.video.identity_count ?? total.value
  try {
    await ElMessageBox.confirm(
      `将给该视频的 ${count} 个面容全部写入${verdict === 'up' ? '好评' : '差评'}，继续？`,
      '整体面容评价', { type: 'warning', confirmButtonText: '继续', cancelButtonText: '取消' })
  } catch {
    return
  }
  bulkRating.value = true
  try {
    const response = await api.post('/faces/rate-video', { video_id: props.video.id, verdict })
    ElMessage.success(`已评价 ${response.data.rated.length} 个面容`)
    emit('rated', { videoId: props.video.id, count: response.data.rated.length, verdict })
    await loadFaces()
  } catch (error) {
    ElMessage.error(errText(error))
  } finally {
    bulkRating.value = false
  }
}

async function rateFace(face, type, value) {
  ratingFaceId.value = face.id
  try {
    await api.post(`/faces/${face.id}/rating`, { type, value })
    face.my_rating = String(value)
    ElMessage.success(type === 'score' ? `已评分 ${value} 分` :
      value === 'up' ? '已好评' : '已差评')
    emit('rated', { videoId: currentVideoId.value, count: 1, verdict: type === 'thumbs' ? value : null })
  } catch (error) {
    ElMessage.error(errText(error))
  } finally {
    ratingFaceId.value = null
  }
}

function open() {
  page.value = 1
  items.value = []
  total.value = props.video?.identity_count || 0
  visible.value = true
}

function onOpen() {
  armBackCapture()
  loadFaces()
}

function onClosed() {
  disarmBackCapture(false)
  reset()
}

function reset() {
  page.value = 1
  items.value = []
  total.value = 0
}

let requestSeq = 0
let backArmed = false

function onBackPop() {
  if (!backArmed) return
  backArmed = false
  window.removeEventListener('popstate', onBackPop)
  visible.value = false
}

function armBackCapture() {
  if (backArmed) return
  history.pushState({ sophosFaceDrawer: Date.now() }, '')
  backArmed = true
  window.addEventListener('popstate', onBackPop)
}

function disarmBackCapture(consume) {
  if (!backArmed) return
  backArmed = false
  window.removeEventListener('popstate', onBackPop)
  if (consume) history.back()
}

onMounted(() => {
  if (visible.value) armBackCapture()
})
onBeforeUnmount(() => {
  window.removeEventListener('popstate', onBackPop)
})
defineExpose({ open })
</script>

<style>
.face-drawer-title { font-size: 15px; font-weight: 600; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; max-width: 60vw; }
.face-drawer-body { display: flex; flex-direction: column; gap: 14px; }
.face-toolbar { display: flex; justify-content: space-between; align-items: center;
  gap: 10px; flex-wrap: wrap; color: #606266; font-size: 13px; }
.face-grid { min-height: 180px; display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  gap: 12px; align-items: start; }
.face-card { --el-card-padding: 8px; }
.face-thumb { width: 100%; height: 190px; object-fit: cover; border-radius: 6px; background: #f5f7fa; }
.face-meta { margin-top: 8px; display: flex; gap: 4px; flex-wrap: wrap; }
.score-row { margin-top: 8px; display: grid; grid-template-columns: repeat(5, 1fr); gap: 4px; }
.score-row .el-button { margin-left: 0; padding: 5px 0; width: 100%; }
.thumb-row { margin-top: 8px; display: flex; gap: 6px; }
.thumb-row .el-button + .el-button { margin-left: 0; }
.thumb-row .el-button { flex: 1; }
@media (max-width: 768px) {
  .face-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .face-toolbar { align-items: flex-start; }
}
</style>
