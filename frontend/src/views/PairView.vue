<template>
  <div style="text-align:center">
    <!-- R13 UI 重排：策略选择与辅助操作一行（左策略/右辅助），"同时好评/差评"
         作为主操作放在两张卡下方居中——按钮不再全挤在页首 -->
    <div style="margin-bottom:14px; display:flex; gap:10px; flex-wrap:wrap; justify-content:space-between; align-items:center">
      <el-radio-group v-model="strategy" :size="ui.isMobile ? 'small' : 'default'" @change="load">
        <el-radio-button value="diverse">人物优先对比（推荐）</el-radio-button>
        <el-radio-button value="similar">相似分对比（更有区分度）</el-radio-button>
        <el-radio-button value="random">随机对比</el-radio-button>
      </el-radio-group>
      <div style="display:flex; gap:8px">
        <el-button size="small" @click="load">换一对</el-button>
        <el-button size="small" type="primary" plain :disabled="!pair" @click="openCurrentVideoFaces">
          本片面容
        </el-button>
        <el-button size="small" :loading="undoing" @click="undoLast">撤销上一条</el-button>
      </div>
    </div>

    <div v-if="loading">加载中…</div>
    <el-empty v-else-if="!pair" description="没有可对比的面容（全部对比完成，或面容库为空）" />

    <div v-else class="pair-grid" :class="{ mobile: ui.isMobile }">
      <el-card v-for="side in ['a', 'b']" :key="side" shadow="hover"
               style="width:320px; max-width:100%; cursor:pointer" @click="choose(side)">
        <img :src="pair[side].rep_thumb" class="pair-face-img"
             style="height:300px; width:auto; max-width:280px; object-fit:contain; border-radius:8px" />
        <div style="margin-top:10px; display:flex; gap:6px; flex-wrap:wrap; justify-content:center">
          <el-tag type="info" effect="plain">基础分 {{ pair[side].base_score ?? '—' }}</el-tag>
          <el-tag v-if="pair[side].female_prob_mean != null" type="info" effect="plain">
            女性 {{ Math.round(pair[side].female_prob_mean * 100) }}%
          </el-tag>
        </div>
        <el-tooltip content="点击播放该视频（跳到这张面容出现的时刻）">
          <el-tag effect="plain" style="cursor:pointer; margin-top:6px; max-width:100%"
                  class="pair-video-tag" @click.stop="playSide(side)">
            {{ pair[side].video_filename }} ▶
          </el-tag>
        </el-tooltip>
        <div style="margin-top:12px; color:#409eff">点选更好的一张</div>
      </el-card>
    </div>

    <div v-if="pair" style="margin-top:16px; display:flex; flex-direction:column; align-items:center; gap:6px">
      <div style="display:flex; gap:14px; flex-wrap:wrap; justify-content:center">
        <el-button type="success" :disabled="busy" @click="rateBoth('up')">👍 同时好评</el-button>
        <el-button type="danger" :disabled="busy" @click="rateBoth('down')">👎 同时差评</el-button>
      </div>
      <div style="color:#c0c4cc; font-size:12px">
        两张难分高下（或都不喜欢）时用；也可直接点卡片选更好的一张
        <span v-if="count">· 已累计 {{ count }} 次对比</span>
      </div>
    </div>
    <PlayerDialog ref="playerRef" />
    <FaceRatingDrawer ref="faceDrawerRef" :video="currentVideo" @rated="load" />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errText } from '../api'
import { ui } from '../ui'
import PlayerDialog from '../components/PlayerDialog.vue'
import FaceRatingDrawer from '../components/FaceRatingDrawer.vue'

const pair = ref(null)
const strategy = ref('diverse')
const loading = ref(true)
const count = ref(0)
const playerRef = ref(null)
const faceDrawerRef = ref(null)
const currentVideo = ref(null)
const busy = ref(false)      // R12：同时好评/差评提交中
const undoing = ref(false)   // R12：撤销提交中

function playSide(side) {
  const f = pair.value?.[side]
  if (!f || !f.stream_url) {
    ElMessage.warning('该面容暂无可用视频流')
    return
  }
  playerRef.value?.open({
    title: f.video_filename,
    stream_url: f.stream_url,
    hls_url: f.hls_url,       // R8：移动端 HLS 会话
    stream_mode: f.stream_mode,
    duration: f.duration_sec,  // R10：真实总时长（进度条钉死不随缓冲增长）
    startAt: f.timestamp_sec,  // 跳到面容出现的时刻
  })
}

function openCurrentVideoFaces() {
  const face = pair.value?.a
  if (!face) return
  currentVideo.value = { id: face.video_id, filename: face.video_filename,
                         identity_count: null }
  faceDrawerRef.value?.open()
}

async function load() {
  loading.value = true
  try {
    const r = await api.get('/faces/pair', { params: { strategy: strategy.value } })
    pair.value = r.data
  } catch (e) {
    pair.value = null
    if (e?.response?.status !== 404) ElMessage.error(errText(e))
  } finally {
    loading.value = false
  }
  try {
    count.value = (await api.get('/ratings/stats')).data.pair_count
  } catch { /* 忽略统计失败 */ }
}

async function choose(winnerSide) {
  if (!pair.value) return
  const loserSide = winnerSide === 'a' ? 'b' : 'a'
  try {
    await api.post('/faces/pair/compare', {
      winner_id: pair.value[winnerSide].id,
      loser_id: pair.value[loserSide].id
    })
    await load()
  } catch (e) {
    ElMessage.error(errText(e))
  }
}

// R12：两张都不相上下/都不喜欢时，一次给两张同样的好评（或差评）——
// 等价于各写一条 thumbs 评分，然后自动换下一对。
async function rateBoth(verdict) {
  if (!pair.value || busy.value) return
  busy.value = true
  try {
    await api.post('/faces/pair/rate-both', {
      identity_ids: [pair.value.a.id, pair.value.b.id],
      verdict,
    })
    ElMessage.success(verdict === 'up' ? '已同时好评两张' : '已同时差评两张')
    await load()
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    busy.value = false
  }
}

// R12：撤销最近一次对比/打分（跨页全局最近一条）。撤销的是对比时把这一对
// 重新摆出来供"重来"；撤销的是评分时提示到评分页重评。
async function undoLast() {
  if (undoing.value) return
  undoing.value = true
  try {
    const r = await api.post('/faces/undo')
    if (r.data.kind === 'pair') {
      const [a, b] = await Promise.all([
        api.get(`/faces/${r.data.winner_id}`),
        api.get(`/faces/${r.data.loser_id}`),
      ])
      pair.value = { a: a.data, b: b.data }
      ElMessage.success('已撤销上次对比，这一对已重新摆出')
    } else {
      ElMessage.success('已撤销最近一次评分（评分页可重评该面容）')
      try { count.value = (await api.get('/ratings/stats')).data.pair_count } catch { /* 忽略 */ }
    }
  } catch (e) {
    if (e?.response?.status === 404) ElMessage.info('没有可撤销的记录')
    else ElMessage.error(errText(e))
  } finally {
    undoing.value = false
  }
}

onMounted(load)
</script>

<style>
/* R6 移动端：A/B 纵排、图片限高、长文件名截断 */
.pair-grid { display: flex; justify-content: center; gap: 24px; flex-wrap: wrap; }
@media (max-width: 768px) {
  .pair-face-img { height: auto !important; max-height: 38vh; max-width: 92% !important; }
  .pair-video-tag { display: inline-block; max-width: 100%; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; vertical-align: middle; }
}
</style>
