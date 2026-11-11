"""One container, three modes (env MODE):
  beat    - extract mono audio from the video in S3, run beat_this, write beats.json
  segment - transcode one time slice (SEG_INDEX of SEG_COUNT) to 720p H.264 with ffmpeg
  concat  - stitch the slices into one MP4 (stream copy)
Every mode writes a timing JSON next to its output so report.py can price it.
"""
import json, os, subprocess, time

import boto3

s3 = boto3.client("s3")
BUCKET, KEY, JOB = os.environ["BUCKET"], os.environ["KEY"], os.environ["JOB_ID"]
MODE = os.environ["MODE"]
OUT = f"output/{JOB}"
T0 = time.time()
timing = {"mode": MODE, "cpus": os.cpu_count(), "started": T0}


def url():
    return s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": KEY}, ExpiresIn=6 * 3600)


def probe(u):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,width,height,r_frame_rate:format=duration", "-of", "json", u],
                       check=True, capture_output=True, text=True)
    j = json.loads(r.stdout)
    st = j["streams"][0]
    num, den = st["r_frame_rate"].split("/")
    return {"codec": st["codec_name"], "width": st["width"], "height": st["height"],
            "fps": round(int(num) / int(den), 3), "duration": float(j["format"]["duration"])}


def put_json(key, obj):
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(obj, indent=1).encode(), ContentType="application/json")


def lap(name, t):
    timing[name] = round(time.time() - t, 2)
    return time.time()


def beat():
    import numpy as np
    import torch
    torch.set_num_threads(os.cpu_count())
    from beat_this.inference import Audio2Beats

    u = url()
    t = time.time()
    timing["probe"] = probe(u)
    t = lap("probe_s", t)
    sr = 22050
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", u, "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"],
                         check=True, capture_output=True).stdout
    signal = np.frombuffer(pcm, dtype=np.float32)
    t = lap("audio_extract_s", t)
    model = Audio2Beats(checkpoint_path=os.environ["CKPT"], device="cpu", dbn=False)
    t = lap("model_load_s", t)
    beats, downbeats = model(signal, sr)
    t = lap("inference_s", t)
    beats, downbeats = [round(float(x), 3) for x in beats], [round(float(x), 3) for x in downbeats]
    bpm = round(60 / float(np.median(np.diff(beats))), 1) if len(beats) > 2 else None
    put_json(f"{OUT}/beats.json", {"beats": beats, "downbeats": downbeats, "bpm_median": bpm,
                                   "audio_seconds": round(len(signal) / sr, 2)})
    timing["n_beats"] = len(beats)


def segment():
    i, n = int(os.environ["SEG_INDEX"]) - 1, int(os.environ["SEG_COUNT"])  # SEG_INDEX is 1-based
    u = url()
    t = time.time()
    p = probe(u)
    t = lap("probe_s", t)
    d = p["duration"] / n
    start = i * d
    out = f"/tmp/seg_{i:03d}.mp4"
    # Input seeking over HTTP range requests: each task only reads its own slice from S3.
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{d:.3f}", "-i", u,
           "-vf", "scale=-2:720", "-c:v", "libx264", "-preset", os.environ.get("X264_PRESET", "veryfast"),
           "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", out]
    subprocess.run(cmd, check=True)
    t = lap("transcode_s", t)
    s3.upload_file(out, BUCKET, f"{OUT}/ffmpeg/segments/seg_{i:03d}.mp4")
    lap("upload_s", t)
    timing.update(seg_index=i, seg_count=n, seg_seconds=round(d, 2), probe=p)


def concat():
    t = time.time()
    prefix = f"{OUT}/ffmpeg/segments/"
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", []))
    with open("/tmp/list.txt", "w") as f:
        for k in keys:
            local = "/tmp/" + k.rsplit("/", 1)[1]
            s3.download_file(BUCKET, k, local)
            f.write(f"file '{local}'\n")
    t = lap("download_s", t)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "/tmp/list.txt",
                    "-c", "copy", "-movflags", "+faststart", "/tmp/out.mp4"], check=True)
    t = lap("concat_s", t)
    s3.upload_file("/tmp/out.mp4", BUCKET, f"{OUT}/ffmpeg/720p.mp4")
    lap("upload_s", t)
    timing["segments"] = len(keys)


{"beat": beat, "segment": segment, "concat": concat}[MODE]()
timing["total_s"] = round(time.time() - T0, 2)
name = MODE if MODE != "segment" else f"segment_{int(os.environ['SEG_INDEX']) - 1:03d}"
put_json(f"{OUT}/timing/{name}.json", timing)
print(json.dumps(timing))
