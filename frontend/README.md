# Sophos 前端（M5 已落地）

**Vue 3 + Vite + Element Plus**；构建产物由 FastAPI StaticFiles 托管（单容器部署，ADR-009）。

## 已实现页面（src/views/）

| 文件 | 功能 |
|---|---|
| `RateView.vue` | 评分页：面容卡片流（缩略图/基础分/来源+时间点）、1-10 按钮、👍👎、跳过、自动下一张、unrated 优先 |
| `PairView.vue` | 对比模式（ADR-013）：A/B 并排点选更好一张、similar/random 策略、换一对、NO_PAIR 空状态 |
| `VideosView.vue` | 视频库：分数对照表（排序/搜索/状态筛选/分页）、行点击播放器弹窗（Range 流式） |
| `TasksView.vue` | 任务与设置：工作目录增删、扫描/处理触发、job 表 2 秒轮询 |

## 常用命令

```cmd
:: node 便携版（本机预置）
set PATH=<project-root>\tools\node-v22.23.2-win-x64;%PATH%

cd /d <project-root>\frontend
npm install          # 首次
npm run dev          # 开发模式（proxy /api -> 127.0.0.1:8000）
npm run build        # 构建到 dist/，由 FastAPI 托管
```

约定：所有请求走 `/api` 前缀（src/api.js 的 axios 实例）；接口契约以 `../docs/API_DESIGN.md` 为准。
