# Video transcode + beat tracking POC on AWS

用同一个视频，把每一种算力并排跑一遍，最后出一份耗时和成本报告：

| 路线 | 转码（720p H.264） | 节拍识别（[beat_this](https://github.com/CPJKU/beat_this) `final0`，MIT） |
|---|---|---|
| `mc` | AWS Elemental MediaConvert（默认开加速转码） | — |
| `fargate` | ECS Fargate：切 N 片 libx264 并行，再拼接 | Fargate 4 vCPU |
| `lambda-x86` / `lambda-arm64` | Lambda 10 GB：切 N 片并行，再拼接 | Lambda 10 GB |
| `gpu` | ECS on EC2 GPU：一块 GPU 上 NVDEC 解码 → `scale_cuda` 缩放 → NVENC 编码 | — （CPU 已足够便宜） |

HDR（PQ / HLG）源会先缩到 720p，再色调映射成 SDR BT.709，所以这一步的开销很小。

```
S3 ──▶ Step Functions ─┬─▶ MediaConvert
                       ├─▶ Fargate  (beat ∥ slices ▶ concat)
                       ├─▶ Lambda x86 / arm64 (beat ∥ slices ▶ concat)
                       └─▶ GPU EC2 ×每种机型
```

## 在 CloudShell 里跑

```bash
git clone https://github.com/quanquan1996/video-transcode-beat-poc.git && cd video-transcode-beat-poc

# 部署 + 构建镜像（约 15 分钟）。GPUS 写想对比的 GPU 机型，格式 type[:GPU 份额[:并行路数]]
GPUS=g4dn.xlarge,g6.xlarge,g6f.xlarge:0.125:2 ./deploy.sh us-west-2

./run.sh us-west-2 s3://<你的桶>/<样片>.mp4            # 所有路线各跑一遍并出报告
ROUTES=gpu,lambda-arm64 ./run.sh us-west-2 s3://...    # 只跑指定路线
GPU_TYPES=g6f.xlarge ROUTES=gpu ./run.sh us-west-2 s3://...  # 只测某几种 GPU
./run.sh us-west-2 s3://<你的桶>/<样片>.mp4 5          # 5 个视频并发

GPU_ON=0 GPUS=g4dn.xlarge,g6.xlarge,g6f.xlarge:0.125:2 SKIP_BUILD=1 ./deploy.sh us-west-2   # GPU 实例缩到 0，停止计费
./cleanup.sh us-west-2                                # 全部删除
```

- 换区域：每个区域各部署一套，用同一个样片跑。注意 us-west-1 只有 g4dn。
- g6f.xlarge 只有 3 GB 显存：4K HDR 最多 2 路（`g6f.xlarge:0.125:2`），8K 只能 1 路。
- 其他参数：`SEGMENTS`（Fargate 切片数，默认 8）、`LAMBDA_SEGMENTS`（默认 16）、`ACCEL=DISABLED`（MediaConvert 改回 Basic 档）。
- 生产触发：上传到 `input/` 前缀会自动跑 Fargate 路线。`run.sh` 用的是 `bench/` 前缀，并直接启动执行。
- 报告里的单价来自 AWS Price List API（2026-11-11，按需价），写在 `report.py` 里，报价前请对照官方价格页再核一遍。

## 输出（`s3://<stack 桶>/output/<id>/<路线>/`）

- `720p.mp4`、`beats.json`（`beats`、`downbeats`，单位秒，以及 `bpm_median`）
- `timing/*.json`：每个任务的阶段耗时
- `results/<region>-<ts>.md`：每条路线每个视频的成本、按每天 500 个折算的月成本，以及 GPU 常驻机群需要几台、每月多少钱

## 说明

- ffmpeg 用的是 [FFmpeg-Builds](https://github.com/FFmpeg-Builds/FFmpeg-Builds) 的固定版本静态构建（n8.1.3，GPL，x86_64 和 arm64，含 NVENC/NVDEC/`scale_cuda`/`zscale`）。
- 模型 checkpoint 在构建镜像时从 HuggingFace（`Audante/beat-this-final0`）下载，并校验 SHA-256。
- 计费口径：Fargate 和 Lambda 都把冷启动算进去；GPU 按常驻（暖）实例上任务的运行时间计。
