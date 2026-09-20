<template>
  <div style="text-align:center">
    <div style="margin-bottom:14px; display:flex; gap:10px; flex-wrap:wrap; justify-content:center; align-items:center">
      <el-radio-group v-model="strategy" :size="ui.isMobile ? 'small' : 'default'" @change="load">
        <el-radio-button value="diverse">人物优先对比（推荐）</el-radio-button>
        <el-radio-button value="similar">相似分对比（更有区分度）</el-radio-button>
        <el-radio-button value="random">随机对比</el-radio-button>
      </el-radio-group>
      <div>
        <el-button @click="load">换一对</el-button>
        <span v-if="count" style="margin-left:10px; color:#909399; font-size:13px">已累计 {{ count }} 次对比</span>
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
    <PlayerDialog ref="playerRef" />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errText } from '../api'
import { ui } from '../ui'
import PlayerDialog from '../components/PlayerDialog.vue'

const pair = ref(null)
const strategy = ref('diverse')
const loading = ref(true)
const count = ref(0)
const playerRef = ref(null)

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
    startAt: f.timestamp_sec,  // 跳到面容出现的时刻
  })
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
