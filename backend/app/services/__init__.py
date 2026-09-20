"""业务服务层。

模块清单与职责（实现批次见 docs/DEVELOPMENT_PLAN.md，细节见 docs/ARCHITECTURE.md §2.1）：
    scanner.py        视频文件增量扫描（M2）
    jobs.py           后台任务 worker 与进度（M2）
    streamer.py       视频 Range 流式播放（M2）
    frame_sampler.py  ffmpeg 抽帧（M3）
    face_engine.py    检测/性别/质量/特征 ONNX 封装（M3）
    clustering.py     视频内聚类成面容 identity（M3）
    scorer.py         SCUT-FBP5500 基础颜值打分（M4）
    aggregator.py     视频综合分聚合与滚动更新（M4）
    personalizer.py   用户偏好接续训练（M6）
"""
