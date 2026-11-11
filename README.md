# Video transcode + beat tracking POC on AWS

S3 收到一个视频，就自动并行跑三条路线，最后出一份耗时和成本报告：

| 路线 | 做什么 | 用什么 |
|---|---|---|
| A | 转成 720p H.264 MP4 | AWS Elemental MediaConvert（全托管，按输出分钟计费） |
| B | 节拍 / 强拍识别（[beat_this](https://github.com/CPJKU/beat_this) `final0`，MIT） | ECS Fargate CPU 任务：ffmpeg 抽音频 → beat_this → `beats.json` |
| C | 对照组：ffmpeg 切 N 片并行转码，再拼接 | ECS Fargate，每片一个任务，按 HTTP Range 只读自己那一段 |

```
S3 input/ ──EventBridge──▶ Step Functions ─┬─▶ A. MediaConvert ─────────────▶ output/<id>/mediaconvert/720p.mp4
                                            ├─▶ B. Fargate beat_this ────────▶ output/<id>/beats.json
                                            └─▶ C. Fargate ffmpeg ×N ─▶ concat ▶ output/<id>/ffmpeg/720p.mp4
```

## 在 CloudShell 里跑

```bash
git clone https://github.com/quanquan1996/video-transcode-beat-poc.git
cd video-transcode-beat-poc
./deploy.sh us-west-1                              # 建资源 + 用 CodeBuild 构建镜像，约 10 分钟
./run.sh us-west-1 s3://<你的桶>/<样片>.mp4          # 跑 1 个视频并出报告
./run.sh us-west-1 s3://<你的桶>/<样片>.mp4 10       # 10 个并发
./cleanup.sh us-west-1                             # 全部删除
```

- 换区域对比：把 `us-west-1` 换成 `us-west-2`，两边各部署一套，用同一个样片跑。
- `SEGMENTS=16 ./deploy.sh ...` 可以调整路线 C 的切片数。
- 报告的单价来自 AWS Price List API（2026-11-11，按需价），写在 `report.py` 里，报价前请对照官方价格页再核一遍。

## 输出

- `beats.json`：`beats`、`downbeats`（单位秒），以及 `bpm_median`。
- `timing/*.json`：每个任务的阶段耗时（探测、抽音频、模型加载、推理、转码、上传）。
- `results/<region>-<ts>.md`：每个视频的成本、按每天 500 个折算的月成本，以及端到端耗时。

## 说明

- 模型 checkpoint 在构建镜像时从 HuggingFace 下载（`Audante/beat-this-final0`，与 CPJKU 官方 `final0` 相同），并校验 SHA-256。
- Fargate 计时包含任务启动和拉镜像的时间，所以偏保守。
- S3 桶里的对象 14 天后自动删除。
