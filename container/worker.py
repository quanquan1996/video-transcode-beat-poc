"""One worker for every compute option (ECS Fargate, ECS on GPU EC2, Lambda x86/arm64).

Modes:
  beat    - extract mono audio from the video in S3, run beat_this, write beats.json
  segment - CPU: transcode one time slice (SEG_INDEX of SEG_COUNT, 1-based) to 720p H.264 (libx264)
  concat  - stitch a route's slices into one MP4 (stream copy)
  gpu     - whole video on one NVIDIA GPU: GPU_STREAMS parallel slices, NVDEC -> scale_cuda -> NVENC, then concat
HDR sources (PQ / HLG) are tone-mapped to SDR BT.709 after downscaling to 720p, so the cost is small.
Each run writes output/<job>/<route>/timing/<name>.json; report.py prices them.

ECS: parameters come from environment variables. Lambda: from the event (handler below).
"""
import json, os, subprocess, time
from concurrent.futures import ThreadPoolExecutor

import boto3

s3 = boto3.client("s3")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
           "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def ncpu():
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count()


class Job:
    def __init__(self, p):
        self.p = p
        self.bucket, self.key, self.job = p["BUCKET"], p["KEY"], p["JOB_ID"]
        self.mode, self.route = p["MODE"], p.get("ROUTE", "fargate")
        self.out = f"output/{self.job}/{self.route}"
        self.t0 = time.time()
        self.timing = {"mode": self.mode, "route": self.route, "cpus": ncpu(), "started": self.t0}

    # ---------- helpers ----------
    def url(self):
        return s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": self.key}, ExpiresIn=6 * 3600)

    def probe(self, u):
        r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=codec_name,width,height,r_frame_rate,pix_fmt,color_transfer:format=duration",
                            "-of", "json", u], check=True, capture_output=True, text=True)
        j = json.loads(r.stdout)
        st = j["streams"][0]
        num, den = st["r_frame_rate"].split("/")
        return {"codec": st["codec_name"], "width": st["width"], "height": st["height"],
                "fps": round(int(num) / int(den), 3), "pix_fmt": st.get("pix_fmt"),
                "hdr": st.get("color_transfer") in HDR_TRANSFERS, "duration": float(j["format"]["duration"])}

    def put_json(self, key, obj):
        s3.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(obj, indent=1).encode(), ContentType="application/json")

    def lap(self, name, t):
        self.timing[name] = round(time.time() - t, 2)
        return time.time()

    def cpu_cmd(self, u, start, dur, out, p):
        vf = "scale=-2:720," + (TONEMAP if p["hdr"] else "format=yuv420p")
        return [FFMPEG, "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", u, "-vf", vf,
                "-c:v", "libx264", "-preset", self.p.get("X264_PRESET", "veryfast"), "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", out]

    def gpu_cmd(self, u, start, dur, out, p):
        # Decode and scale on the GPU. For HDR, download the 720p 10-bit frames and tone-map on CPU (cheap at 720p).
        if p["hdr"]:
            vf = "scale_cuda=1280:720:format=p010le,hwdownload,format=p010le," + TONEMAP
        else:
            vf = "scale_cuda=1280:720:format=nv12"
        return [FFMPEG, "-v", "error", "-y", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", u, "-vf", vf,
                "-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", "26",
                "-b:v", "2500k", "-maxrate", "4000k", "-bufsize", "8000k",
                "-c:a", "aac", "-b:a", "128k", out]

    def concat_files(self, files, out):
        with open("/tmp/list.txt", "w") as f:
            f.writelines(f"file '{x}'\n" for x in files)
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "/tmp/list.txt",
                        "-c", "copy", "-movflags", "+faststart", out], check=True)

    # ---------- modes ----------
    def beat(self):
        import numpy as np
        import torch
        torch.set_num_threads(ncpu())
        from beat_this.inference import Audio2Beats

        u = self.url()
        t = time.time()
        self.timing["probe"] = self.probe(u)
        t = self.lap("probe_s", t)
        sr = 22050
        pcm = subprocess.run([FFMPEG, "-v", "error", "-i", u, "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"],
                             check=True, capture_output=True).stdout
        signal = np.frombuffer(pcm, dtype=np.float32)
        t = self.lap("audio_extract_s", t)
        model = Audio2Beats(checkpoint_path=os.environ.get("CKPT", "/opt/model/final0.ckpt"), device="cpu", dbn=False)
        t = self.lap("model_load_s", t)
        beats, downbeats = model(signal, sr)
        self.lap("inference_s", t)
        beats, downbeats = [round(float(x), 3) for x in beats], [round(float(x), 3) for x in downbeats]
        bpm = round(60 / float(np.median(np.diff(beats))), 1) if len(beats) > 2 else None
        self.put_json(f"{self.out}/beats.json", {"beats": beats, "downbeats": downbeats, "bpm_median": bpm,
                                                 "audio_seconds": round(len(signal) / sr, 2)})
        self.timing["n_beats"] = len(beats)
        return "beat"

    def segment(self):
        i, n = int(self.p["SEG_INDEX"]) - 1, int(self.p["SEG_COUNT"])
        u = self.url()
        t = time.time()
        p = self.probe(u)
        t = self.lap("probe_s", t)
        d = p["duration"] / n
        out = f"/tmp/seg_{i:03d}.mp4"
        subprocess.run(self.cpu_cmd(u, i * d, d, out, p), check=True)  # HTTP range reads: only this slice
        t = self.lap("transcode_s", t)
        s3.upload_file(out, self.bucket, f"{self.out}/segments/seg_{i:03d}.mp4")
        os.remove(out)
        self.lap("upload_s", t)
        self.timing.update(seg_index=i, seg_count=n, seg_seconds=round(d, 2), probe=p)
        return f"segment_{i:03d}"

    def concat(self):
        t = time.time()
        prefix = f"{self.out}/segments/"
        keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix).get("Contents", []))
        files = []
        for k in keys:
            local = "/tmp/" + k.rsplit("/", 1)[1]
            s3.download_file(self.bucket, k, local)
            files.append(local)
        t = self.lap("download_s", t)
        self.concat_files(files, "/tmp/out.mp4")
        t = self.lap("concat_s", t)
        s3.upload_file("/tmp/out.mp4", self.bucket, f"{self.out}/720p.mp4")
        self.lap("upload_s", t)
        self.timing["segments"] = len(keys)
        return "concat"

    def gpu(self):
        n = int(self.p.get("GPU_STREAMS", "4"))
        u = self.url()
        t = time.time()
        p = self.probe(u)
        t = self.lap("probe_s", t)
        d = p["duration"] / n
        files = [f"/tmp/g_{i:03d}.mp4" for i in range(n)]
        with ThreadPoolExecutor(n) as ex:
            list(ex.map(lambda i: subprocess.run(self.gpu_cmd(u, i * d, d, files[i], p), check=True), range(n)))
        t = self.lap("transcode_s", t)
        self.concat_files(files, "/tmp/out.mp4")
        t = self.lap("concat_s", t)
        s3.upload_file("/tmp/out.mp4", self.bucket, f"{self.out}/720p.mp4")
        self.lap("upload_s", t)
        smi = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                             capture_output=True, text=True)
        self.timing.update(streams=n, probe=p, gpu=smi.stdout.strip(), instance=self.p.get("INSTANCE_TYPE"))
        return "gpu"

    def run(self):
        name = {"beat": self.beat, "segment": self.segment, "concat": self.concat, "gpu": self.gpu}[self.mode]()
        self.timing["total_s"] = round(time.time() - self.t0, 2)
        self.timing["ended"] = time.time()
        if "ENTERED" in self.p:  # Step Functions state entry time -> wall time incl. Lambda cold start
            self.timing["entered"] = self.p["ENTERED"]
        self.put_json(f"{self.out}/timing/{name}.json", self.timing)
        print(json.dumps(self.timing))
        return self.timing


def handler(event, context):  # AWS Lambda entry point
    os.environ.setdefault("TORCH_HOME", "/tmp")
    t = Job(event).run()
    t["memory_mb"] = int(context.memory_limit_in_mb)
    return t


if __name__ == "__main__":  # ECS entry point
    Job(dict(os.environ)).run()
