# 视频转码 + 音乐节拍识别：AWS 方案验证（POC）

把一段视频转成 **720p H.264**，同时用 [beat_this](https://github.com/CPJKU/beat_this)（ISMIR 2024，MIT 许可）识别音乐的**节拍和强拍**。同一个视频会并排跑 5 种 AWS 算力方案，最后自动出一份**耗时 + 成本**对比报告。

- **实测结果和月成本**（us-west-1 / us-west-2，4K60 HDR 和 8K 样片）：见 [docs/RESULTS.md](docs/RESULTS.md)
- 在 **AWS CloudShell** 里直接运行，本地不需要装 Docker、Python 或任何依赖
- 部署一条命令，测试一条命令，删除一条命令

## 架构

```
            ┌──────────────────────── Step Functions（每个视频一次执行）────────────────────────┐
S3 视频 ──▶ │ ① MediaConvert（全托管）                                                          │ ──▶ S3 output/<id>/<方案>/
            │ ② ECS Fargate：节拍识别 ∥ ffmpeg 切 N 片并行 ▶ 拼接                               │       720p.mp4
            │ ③ Lambda x86_64：节拍识别 ∥ ffmpeg 切 N 片并行 ▶ 拼接                             │       beats.json
            │ ④ Lambda arm64（Graviton）：同上                                                  │       timing/*.json
            │ ⑤ ECS on EC2 GPU：NVDEC 解码 → GPU 缩放 → NVENC 编码（可同时对比多种 GPU 机型）   │
            └───────────────────────────────────────────────────────────────────────────────────┘
```

| 方案 | 转码 | 节拍识别 | 适合 |
|---|---|---|---|
| ① MediaConvert | 全托管，可开加速转码 | — | 不想运维、对延迟不敏感 |
| ② Fargate | 切片后用 libx264 并行转码 | Fargate CPU | 无服务器容器，按秒计费 |
| ③④ Lambda | 切 16 片，每片 10 GB（约 6 vCPU） | Lambda | 延迟最低（约 1 分钟），没有视频时不花钱，适合扛高峰 |
| ⑤ GPU | 整片放在一块 GPU 上，多路并行 | — | 成本最低，适合常驻机群扛日常量 |

- 每个方案都可以单独开关，一个方案失败不影响其他方案。
- HDR（PQ / HLG）源会自动做色调映射，转成 SDR BT.709：先缩到 720p 再映射，开销很小，各方案颜色一致。
- 节拍识别用 CPU 就够了（每个视频不到 1 美分），所以 GPU 方案只负责转码。

## 快速开始（AWS CloudShell）

```bash
git clone https://github.com/quanquan1996/video-transcode-beat-poc.git
cd video-transcode-beat-poc

# 1. 部署 + 构建镜像（约 15 分钟）。GPUS 写想对比的 GPU 机型，格式 type[:GPU 份额[:并行路数]]
GPUS=g4dn.xlarge,g6.xlarge,g6f.xlarge:0.125:2 ./deploy.sh us-west-2

# 2. 把样片放到任意 S3 桶，所有方案各跑一遍并出报告
./run.sh us-west-2 s3://<你的桶>/<样片>.mp4

# 3. 测完先关掉 GPU 实例（停止计费），不用了就全部删除
GPU_ON=0 GPUS=g4dn.xlarge,g6.xlarge,g6f.xlarge:0.125:2 SKIP_BUILD=1 ./deploy.sh us-west-2
./cleanup.sh us-west-2
```

### 常用参数

| 用途 | 写法 |
|---|---|
| 只跑部分方案 | `ROUTES=gpu,lambda-arm64 ./run.sh ...`（可选值：`mc`、`fargate`、`lambda-x86`、`lambda-arm64`、`gpu`） |
| 只测部分 GPU | `GPU_TYPES=g6f.xlarge ROUTES=gpu ./run.sh ...` |
| 并发压测 | `./run.sh us-west-2 s3://.../a.mp4 5`（同时跑 5 个视频） |
| 切片数 | `SEGMENTS=8`（Fargate，默认 8）、`LAMBDA_SEGMENTS=16`（Lambda，默认 16） |
| MediaConvert 档位 | 默认开加速转码（Professional 档）；`ACCEL=DISABLED` 改回 Basic 档 |
| 换区域对比 | 每个区域各部署一套，用同一个样片跑（注意：us-west-1 只有 g4dn） |

- 分片 GPU（g6f）显存比较小：g6f.xlarge 只有 3 GB，4K HDR 最多并行 2 路（`g6f.xlarge:0.125:2`），8K 只能 1 路。
- 生产接入：视频上传到桶的 `input/` 前缀会自动触发 Fargate 方案；`run.sh` 用的是 `bench/` 前缀，并直接启动执行。

## 输出

在 `s3://<stack 桶>/output/<执行 id>/<方案>/` 下：

- `720p.mp4`：转码结果
- `beats.json`：`beats`、`downbeats`（单位秒），以及 `bpm_median`
- `timing/*.json`：每个任务的阶段耗时（探测、抽音频、推理、转码、上传）
- `results/<region>-<时间>.md`：成本报告，包括每个方案每个视频的成本、按每天 500 个折算的月成本，以及 GPU 常驻机群需要几台、每月多少钱

## 计费口径

- 单价取自 AWS Price List API（按需价，2026-11-11），写在 [`report.py`](report.py) 里。正式报价前请对照官方价格页再核一遍。
- Fargate 从拉镜像开始计费，Lambda 从状态进入时计时，两者都包含冷启动。
- GPU 按常驻（暖）实例上任务的实际运行时间计。机群规模按平均到达量、利用率 70% 估算。
- MediaConvert 处理 HDR→SDR 按 Professional 档计。
- 不含 S3 存储、Step Functions 状态转换和 CloudWatch 日志的费用，这几项都很小。

## 组件和许可

| 组件 | 来源 |
|---|---|
| ffmpeg | [FFmpeg-Builds](https://github.com/FFmpeg-Builds/FFmpeg-Builds) n8.1.3 静态构建（GPL；x86_64 / arm64；含 NVENC、NVDEC、`scale_cuda`、`zscale`） |
| 节拍模型 | CPJKU beat_this `final0`（MIT），构建时从 HuggingFace `Audante/beat-this-final0` 下载，并校验 SHA-256 |
| 本仓库代码 | MIT-0，见 [LICENSE](LICENSE) |

> 本仓库是方案验证用的示例代码，没有经过生产环境的安全和高可用加固。用于生产前，请按需补充 VPC 私有子网、加密、告警、重试 / 死信队列以及多可用区容量规划。
