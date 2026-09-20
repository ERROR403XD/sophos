<template>
  <div v-if="loading">加载中…</div>
  <div v-else-if="!face" style="text-align:center; padding:60px 0">
    <el-result icon="success" title="没有待评分的面容"
      sub-title="先到「任务与设置」添加目录并运行扫描/处理，或所有面容都已评分">
      <template #extra>
        <el-button @click="load">刷新</el-button>
      </template>
    </el-result>
  </div>
  <div v-else style="max-width:860px; margin:0 auto; text-align:center">
    <el-card shadow="hover" :body-style="{ padding: ui.isMobile ? '14px 10px' : '' }">
      <img :src="face.rep_thumb" class="rate-face-img"
           style="height:300px; width:auto; max-width:100%; border-radius:8px" />
      <div style="margin:14px 0 4px">
        <el-tag type="info" effect="plain">基础分 {{ face.base_score ?? '—' }}</el-tag>
        <el-tag v-if="face.personalized_score != null" type="warning" effect="plain">
          个性化 {{ face.personalized_score }}
        </el-tag>
        <el-tag type="info" effect="plain">{{ face.n_samples }} 帧</el-tag>
        <el-tag v-if="face.female_prob_mean != null" type="info" effect="plain">
          女性 {{ Math.round(face.female_prob_mean * 100) }}%<template
            v-if="face.clip_female_mean != null">·C{{ Math.round(face.clip_female_mean * 100) }}%</template>
        </el-tag>
        <el-tooltip content="点击播放该视频（跳到这张面容出现的时刻）">
          <el-tag effect="plain" style="cursor:pointer; max-width:100%" class="video-name-tag" @click="playCurrent">
            {{ face.video_filename }} @ {{ fmtTime(face.timestamp_sec) }} ▶
          </el-tag>
        </el-tooltip>
        <el-tag v-if="face.my_rating" type="success" effect="plain">已评: {{ face.my_rating }}</el-tag>
        <div style="margin-top:8px; display:flex; justify-content:center; align-items:center; gap:8px; flex-wrap:wrap">
          <span style="color:#909399; font-size:12px">遮挡标记：</span>
          <el-switch v-model="face.occluded" size="small"
                     active-text="有遮挡" inactive-text="无"
                     @change="toggleOcclusion" />
        </div>
      </div>

      <div style="margin:18px 0 8px; color:#909399">1-10 打分</div>
      <div style="display:flex; justify-content:center; gap:6px; flex-wrap:wrap">
        <el-button v-for="n in 10" :key="n" :style="ui.isMobile ? 'width:44px; margin:0' : 'width:56px'"
                   @click="rate('score', n)">{{ n }}</el-button>
      </div>
      <el-divider />
      <div style="display:flex; justify-content:center; gap:12px; flex-wrap:wrap">
        <el-button type="success" size="large" :style="ui.isMobile ? 'flex:1' : ''" @click="rate('thumbs', 'up')">👍 好评</el-button>
        <el-button type="danger" size="large" :style="ui.isMobile ? 'flex:1' : ''" @click="rate('thumbs', 'down')">👎 差评</el-button>
        <el-button size="large" text @click="skip">跳过</el-button>
      </div>
      <div style="margin-top:10px; color:#c0c4cc; font-size:12px">
        本批剩余 {{ queue.length - 1 }} / 已加载 {{ total }} 个未评面容
      </div>
    </el-card>
    <PlayerDialog ref="playerRef" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errText } from '../api'
import { ui } from '../ui'
import PlayerDialog from '../components/PlayerDialog.vue'

const queue = ref([])
const total = ref(0)
const loading = ref(true)
const face = computed(() => queue.value[0] || null)
const playerRef = ref(null)

function fmtTime(sec) {
  if (sec == null) return '—'
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

// R5.2：预取接下来几张缩略图——评分点击后下一张立即可见，
// 不再等网络盘上的 JPEG 下载（配合缩略图端点的 immutable 缓存头）
function prefetchNext() {
  queue.value.slice(1, 4).forEach(f => { const im = new Image(); im.src = f.rep_thumb })
}

async function load() {
  loading.value = true
  try {
    // order=random：未评面容随机抽样（R5.2），避免按 id 顺序总在刷同几个视频
    const r = await api.get('/faces', { params: { unrated: true, page_size: 100, order: 'random' } })
    queue.value = r.data.items
    total.value = r.data.total
    prefetchNext()
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    loading.value = false
  }
}

function playCurrent() {
  if (!face.value || !face.value.stream_url) {
    ElMessage.warning('该面容暂无可用视频流')
    return
  }
  playerRef.value?.open({
    title: face.value.video_filename,
    stream_url: face.value.stream_url,
    hls_url: face.value.hls_url,       // R8：移动端 HLS 会话
    stream_mode: face.value.stream_mode,
    startAt: face.value.timestamp_sec,  // 跳到面容出现的时刻
  })
}

async function rate(type, value) {
  try {
    await api.post(`/faces/${face.value.id}/rating`, { type, value })
    queue.value.shift()
    prefetchNext()
    if (!queue.value.length) await load()
  } catch (e) {
    ElMessage.error(errText(e))
  }
}

// R1(P2)：遮挡人工标记（manual 覆盖 auto）。遮挡面容保留可评分，仅影响训练隔离
async function toggleOcclusion(val) {
  try {
    await api.post(`/faces/${face.value.id}/occlusion`, { occluded: !!val })
  } catch (e) {
    face.value.occluded = !val  // 回滚 UI
    ElMessage.error(errText(e))
  }
}

function skip() {
  queue.value.push(queue.value.shift())  // 挪到队尾
  prefetchNext()
}

onMounted(load)
</script>

<style>
/* R6 移动端：面容图自适应视口高度；长文件名标签截断防撑破卡片 */
@media (max-width: 768px) {
  .rate-face-img { height: auto !important; max-height: 44vh; }
  .video-name-tag { display: inline-block; max-width: 100%; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; vertical-align: middle; }
}
</style>
