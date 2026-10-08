<template>
  <div style="max-width:900px; margin:0 auto">
    <el-card shadow="never" style="margin-bottom:16px">
      <template #header>面容与处理</template>
      <div class="settings-grid">
        <div>
          <div class="settings-label">面容性别</div>
          <el-radio-group v-model="runtimeSettings.gender_selection">
            <el-radio-button value="female">仅女性</el-radio-button>
            <el-radio-button value="male">仅男性</el-radio-button>
            <el-radio-button value="all">全部</el-radio-button>
          </el-radio-group>
          <div class="settings-help">只影响后续处理与“重新进行人脸识别”，不改变已提取面容。</div>
        </div>
        <div>
          <div class="settings-label">处理分批大小（个/批）</div>
          <el-input-number v-model="runtimeSettings.process_batch_size" :min="1" :max="50" />
        </div>
        <div>
          <div class="settings-label">同一视频同一人最多保留面容数（0 = 不限制）</div>
          <el-input-number v-model="runtimeSettings.max_faces_per_person" :min="0" :max="500" />
        </div>
        <div>
          <div class="settings-label">自动训练触发（新增评分/对比条数，0 = 关闭）</div>
          <el-input-number v-model="runtimeSettings.auto_train_every" :min="0" :max="100000" />
        </div>
      </div>
    </el-card>

    <el-card shadow="never" style="margin-bottom:16px">
      <template #header>自动化</template>
      <div class="settings-grid">
        <div>
          <div class="settings-label">自动扫描工作目录</div>
          <el-switch v-model="runtimeSettings.auto_scan_enabled" />
        </div>
        <div>
          <div class="settings-label">每天扫描时刻</div>
          <el-time-picker v-model="runtimeSettings.auto_scan_time" value-format="HH:mm"
                          format="HH:mm" :disabled="!runtimeSettings.auto_scan_enabled"
                          style="width:120px" placeholder="时刻" />
        </div>
        <div>
          <div class="settings-label">自动处理面容</div>
          <el-switch v-model="runtimeSettings.auto_process_enabled" />
        </div>
      </div>
      <el-divider style="margin:14px 0" />
      <el-button type="primary" :loading="saving" @click="save">保存设置</el-button>
    </el-card>

    <el-card shadow="never">
      <template #header>面容库重建</template>
      <el-alert type="warning" :closable="false" show-icon style="margin-bottom:12px">
        将按分批设置重跑全部视频，应用最新面容优选与性别设置。已训练模型文件保持不变；
        旧面容派生评分/对比会被替换，人工记录无法可靠映射到新面容 ID 时会被删除。
      </el-alert>
      <el-button type="danger" :loading="rebuilding" @click="rebuildFaces">重新进行人脸识别</el-button>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, errText } from '../api'

const runtimeSettings = reactive({
  process_batch_size: 4,
  max_faces_per_person: 5,
  auto_train_every: 30,
  auto_scan_enabled: false,
  auto_scan_time: '03:00',
  auto_process_enabled: false,
  gender_selection: 'female',
})
const saving = ref(false)
const rebuilding = ref(false)

async function load() {
  try {
    Object.assign(runtimeSettings, (await api.get('/settings')).data.settings)
  } catch (error) {
    ElMessage.error(errText(error))
  }
}

async function save() {
  saving.value = true
  try {
    Object.assign(runtimeSettings,
      (await api.put('/settings', { ...runtimeSettings })).data.settings)
    ElMessage.success('设置已保存')
  } catch (error) {
    ElMessage.error(errText(error))
  } finally {
    saving.value = false
  }
}

async function rebuildFaces() {
  try {
    await ElMessageBox.confirm(
      `将重跑全部视频并重建面容库；当前每批 ${runtimeSettings.process_batch_size} 个。任务可暂停/取消。继续？`,
      '重新进行人脸识别', { type: 'warning', confirmButtonText: '开始重建', cancelButtonText: '取消' })
  } catch {
    return
  }
  rebuilding.value = true
  try {
    const response = await api.post('/process/rebuild-faces')
    ElMessage.success(`重建任务 #${response.data.job.id} 已提交，共 ${response.data.total_videos} 个视频`)
  } catch (error) {
    ElMessage.error(errText(error))
  } finally {
    rebuilding.value = false
  }
}

onMounted(load)
</script>

<style>
.settings-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }
.settings-label { margin-bottom: 6px; font-size: 13px; color: #606266; }
.settings-help { margin-top: 6px; font-size: 12px; color: #909399; }
@media (max-width: 768px) { .settings-grid { grid-template-columns: 1fr; } }
</style>
