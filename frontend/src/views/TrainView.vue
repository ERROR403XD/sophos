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
        <el-button v-if="status.active_version" size="small" @click="deactivate">停用（回退基础分）</el-button>
      </div>
      <div style="margin-top:10px; color:#909399; font-size:13px">
        累计 ≥20 条评分/对比才可训练；每新增 30 条自动训练一次（需手动启用新版本）。
      </div>
      <el-alert v-if="lastJob && lastJob.status === 'failed'" type="error" :closable="false"
                style="margin-top:10px" :title="'上次训练失败：' + (lastJob.error || '').slice(0, 160)" />
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <span>模型版本</span>
          <el-button size="small" @click="load">刷新</el-button>
        </div>
      </template>
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
          <el-table-column label="操作" width="140">
            <template #default="{ row }">
              <el-button v-if="row.active" size="small" type="success" plain disabled>已启用</el-button>
              <el-button v-else size="small" type="primary" @click="activate(row.version)">启用</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errText } from '../api'

const status = ref({ active_version: null, last_job: null })
const versions = ref([])
const training = ref(false)

async function load() {
  try {
    status.value = (await api.get('/train/status')).data
    versions.value = (await api.get('/train/versions')).data.versions
  } catch (e) {
    ElMessage.error(errText(e))
  }
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
      if (body.status === 'done') { ElMessage.success('训练完成，可在下方启用新版本'); break }
      if (body.status === 'failed') { ElMessage.error((body.error || '').slice(0, 160)); break }
    }
    await load()
  } catch (e) {
    ElMessage.error(errText(e))
  } finally {
    training.value = false
  }
}

async function activate(version) {
  try {
    await api.post(`/train/activate/${version}`)
    ElMessage.success(`已启用 ${version}，视频综合分已滚动更新`)
    await load()
  } catch (e) {
    ElMessage.error(errText(e))
  }
}

async function deactivate() {
  try {
    await api.post('/train/deactivate')
    ElMessage.success('已停用，回退基础分')
    await load()
  } catch (e) {
    ElMessage.error(errText(e))
  }
}

onMounted(load)
</script>
