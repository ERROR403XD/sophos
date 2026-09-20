# PLAN v1.2.0（R6 修复批次）

> 来源：用户 R5.2 复检反馈 6 项。原则：后端小步修复 + 前端移动端体验整体翻新；
> 不改评分/训练的数学语义，只修链路与表述。

## T1 访问权限（一个密码，容器建立时可配置）

- `SOPHOS_ACCESS_PASSWORD`（空 = 不启用鉴权，保持单机局域网旧行为）。
- 后端新增 `app/api/auth.py` + ASGI 中间件：
  - `POST /api/auth/login` {password} → 常量时间比较；通过则种 `sophos_session`
    HttpOnly Cookie（值 = sha256("sophos:"+password)，改密码即全部失效）；
  - 中间件保护 `/api/*`（除 `/api/auth/*`、`/api/health`）与 `/docs` `/openapi.json`；
    静态 SPA 资源放行（无数据），`<img>/<video>` 同源自动带 Cookie，缩略图/播放无需改造；
  - 401 统一 `{code:"UNAUTHORIZED"}`。
- 前端：axios 401 拦截 → App 全局登录遮罩（毛玻璃卡片）→ 成功后整页刷新。
- 部署：docker-compose `SOPHOS_ACCESS_PASSWORD: ${SOPHOS_ACCESS_PASSWORD:-}`，
  `.env.example` 注释位。

## T2 播放失败兜底（直出也报"流加载失败或编码不支持"）

根因：错误提示来自 `<video>` error 事件且文案一刀切；direct 档失败的真实原因
无法自愈——h264 High10/非常规 profile 浏览器解不了、扫描期探测失败时"乐观直出"
实际编码不兼容、文件损坏等。修法 = **自动降级**：

- 后端 stream 端点支持 `?fallback=1`：direct/remux 决策改为 transcode（转码关闭时维持原档）；
- 前端 PlayerDialog：error 事件时 direct/remux 未兜底过 → 自动切
  `stream_url?fallback=1(&ss=)` 重试一次（提示"直出失败，已切换转码"）；
  仍失败按 MediaError.code 分型提示（2=网络 / 3=解码 / 4=源不存在或格式不支持）。

## T3 快速 seek 转圈 / 训练·应用模型时页面失去响应

两个独立根因，都修：

1. **转码并发失控**（R5.2"首块交付即释放"信号量语义的副作用）：快速拖动
   进度条 = 每 250ms 起一条新 ffmpeg 转码流，旧流靠看门狗 1s 后才杀——
   弱 CPU（NAS）上多条 1080p x265→h264 并行把整机打满，Web 服务一起僵死。
   修复：信号量恢复**流生命周期全程持有**（僵尸问题已由 R5.2 断连看门狗
   kill 兜底，泄漏窗口有界）；看门狗轮询 1s→0.35s。
2. **SQLite 写事务挤占读**：apply_version / recompute_all 分块连续提交，
   回滚日志模式下写锁间隙小，读请求排队（busy_timeout 30s）→ 页面加载
   全部卡住。修复：分块提交间 `time.sleep(0.02)` 留出读窗口（apply 全程
   约多 10s，可接受）；apply 块大小 2000 不变。

前端：PlayerDialog 换源竞态加固（seek 换源时忽略旧流的迟到 error）。

## T4 面容代表：宁缺毋滥 + 防同人碎裂 + 每视频保底一张

- **合并第三层（单样本同人）**：`merge_groups` 新增 `singleton_threshold`
  （默认 0.74，`SOPHOS_MERGE_SINGLETON_THRESHOLD`，0=关）：双方均单样本、
  余弦 ∈ [0.74, 0.78)、无同帧共现 → 合并。R4 谨慎层要求双方 ≥2 干净样本，
  单样本碎片（基线 78%）永远合不上——这是"同一个人识别成不同的人"的主因。
- **性别门保底（每视频 ≥1 张有效面容）**：gender/CLIP 双门把视频全部 cluster
  拒掉时，若存在均值 fp≥0.50 且 CLIP≥0.50（可用时）且 rep 非侧脸的 cluster，
  保底保留质量最优的 1 个（不凑数、只保 1）；确实无此类证据（如纯男性视频）
  → 维持 0 张，status_msg 说明。
- **不凑数**：cap_groups 上限语义本来就是"封顶不补齐"，维持；rep 优选
  （干净 > 正面 > 清晰）维持，`n_samples`/时间多样性由合并层自然收敛。

## T5 训练方向表述（澄清语义）

训练 = 用**全库用户的评分/对比**作为监督信号调整打分头参数，再用新模型
对全库重新**预测**分数；不是把某个视频的面容分数直接改写/规范化。
落点：TrainView 说明区重写 + README 特征行 + ARCHITECTURE §3.6 澄清段。

## T6 移动端 UI（响应式重构）

Element Plus 保留（桌面不动），移动端整体翻新：

- `App.vue`：桌面保持 border-card 页签；≤768px 切换为**顶栏 + 底部 TabBar**
  （图标+文字、safe-area 适配），内容区底部留白；
- 新增 `src/ui.js`：`isMobile` 响应式单例（matchMedia resize 监听）；
- VideosView：移动端表格 → **卡片列表**（文件名/分数徽标/状态/播放）；
- TasksView：移动端 job 表格 → 卡片；工作目录/设置排版纵排；
- TrainView/版本表与 Jobs 表：移动端横向滚动容器兜底；
- RateView/PairView：图片 `max-height: min(48vh,…)` 自适应、A/B 移动端纵排；
- PlayerDialog：移动端改为 flex 列布局（顶部信息条 + 播放器填满 + 底部错误条），
  用 `100dvh`，替代 `calc(100vh-120px)` 魔法数。

## 测试与验收

- pytest 新增：auth 6 项、stream fallback 2 项、merge singleton 2 项、
  rescue 2 项、（回归全绿）；
- 前端 `npm run build`；
- 文档：README 批次表/特性、HANDOFF R6 节、DECISIONS ADR-026（访问控制）、
  ADR-027（播放兜底与并发语义）、ADR-028（合并第三层与保底）、.env.example、
  docker-compose.yml、APP_VERSION=1.2.0。
