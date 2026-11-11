"""Wait for pipeline executions, then report latency and cost per video and per month.

Prices: AWS Price List API, on-demand, pulled 2026-11-11. Re-check before quoting:
  https://aws.amazon.com/mediaconvert/pricing/   https://aws.amazon.com/fargate/pricing/
"""
import argparse, datetime as dt, json, statistics, time

import boto3

PRICES = {
    # MediaConvert Basic tier, AVC/H.264 HD output (720p counts as HD), single pass, $ per output minute by fps band
    # Fargate Linux/x86 on-demand: $ per vCPU-hour, $ per GB-hour
    "us-west-1": {"mc_hd": {30: 0.017, 60: 0.0212, 120: 0.0255}, "vcpu": 0.04656, "gb": 0.00511},
    "us-west-2": {"mc_hd": {30: 0.015, 60: 0.0188, 120: 0.0225}, "vcpu": 0.04048, "gb": 0.004445},
}


def ts(v):
    """Step Functions returns ECS times as epoch ms and MediaConvert times as ISO strings or epoch s."""
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def fargate_cost(task, p):
    secs = max(60.0, ts(task["stoppedAt"]) - ts(task["createdAt"]))  # 1-minute minimum
    return secs / 3600 * (int(task["cpu"]) / 1024 * p["vcpu"] + int(task["memory"]) / 1024 * p["gb"]), secs


def fps_band(fps):
    return 30 if fps <= 30 else 60 if fps <= 60 else 120


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--stack", default="beat-poc")
    ap.add_argument("--since", type=int, required=True, help="epoch seconds; only executions started after this")
    ap.add_argument("--expect", type=int, default=1)
    ap.add_argument("--per-day", type=int, default=500)
    a = ap.parse_args()
    p = PRICES[a.region]
    cfn, sfn, s3 = (boto3.client(x, region_name=a.region) for x in ("cloudformation", "stepfunctions", "s3"))
    outs = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=a.stack)["Stacks"][0]["Outputs"]}
    sm, bucket = outs["StateMachineArn"], outs["BucketName"]

    print(f"== waiting for {a.expect} execution(s) on {sm.split(':')[-1]}")
    while True:
        ex = [e for e in sfn.list_executions(stateMachineArn=sm, maxResults=100)["executions"]
              if e["startDate"].timestamp() >= a.since - 5]
        done = [e for e in ex if e["status"] != "RUNNING"]
        print(f"   {len(done)}/{len(ex)} finished ({int(time.time() - a.since)} s)", flush=True)
        if len(ex) >= a.expect and len(done) == len(ex):
            break
        time.sleep(20)

    rows = []
    for e in ex:
        d = sfn.describe_execution(executionArn=e["executionArn"])
        if d["status"] != "SUCCEEDED":
            print("FAILED:", e["name"], d.get("error"), d.get("cause", "")[:500])
            continue
        inp, (mc, beat, ff) = json.loads(d["input"]), json.loads(d["output"])
        job = inp["id"]
        tim = lambda n: json.loads(s3.get_object(Bucket=bucket, Key=f"output/{job}/timing/{n}.json")["Body"].read())
        bt = tim("beat")
        probe = bt["probe"]
        mins = mc["outputs"][0]["OutputDetails"][0]["DurationInMs"] / 60000
        mc_cost = mins * p["mc_hd"][fps_band(probe["fps"])]
        mc_secs = ts(mc["timing"]["FinishTime"]) - ts(mc["timing"]["SubmitTime"])
        beat_cost, beat_secs = fargate_cost(beat, p)
        seg = [fargate_cost(t, p) for t in ff["segments"]]
        cc, _ = fargate_cost(ff["concat"], p)
        ff_cost = sum(c for c, _ in seg) + cc
        ff_secs = ts(ff["concat"]["stoppedAt"]) - min(ts(t["createdAt"]) for t in ff["segments"])
        rows.append(dict(job=job[:8], e2e=(d["stopDate"] - d["startDate"]).total_seconds(), mc=mc_secs, mc_cost=mc_cost,
                         beat=beat_secs, beat_infer=bt["inference_s"], beat_audio=bt["audio_extract_s"],
                         beat_cost=beat_cost, ff=ff_secs, ff_cost=ff_cost, probe=probe, mins=mins,
                         n_beats=bt.get("n_beats"), segs=len(seg), beat_vcpu=int(beat["cpu"]) // 1024))
    if not rows:
        return
    m = lambda k: statistics.mean(r[k] for r in rows)
    pr = rows[0]["probe"]
    monthly = a.per_day * 30
    lines = [
        f"# Results: {a.region}, {len(rows)} video(s) in parallel",
        f"Source: {pr['codec']} {pr['width']}x{pr['height']} @ {pr['fps']} fps, {pr['duration'] / 60:.1f} min",
        "",
        "| Step | avg wall time (s) | $ / video | $ / month @ %d/day |" % a.per_day,
        "|---|---|---|---|",
        f"| A. MediaConvert 720p (Basic) | {m('mc'):.0f} | {m('mc_cost'):.4f} | {m('mc_cost') * monthly:,.0f} |",
        f"| B. beat_this on Fargate {rows[0]['beat_vcpu']} vCPU (CPU only) | {m('beat'):.0f} (audio {m('beat_audio'):.0f}, model {m('beat_infer'):.0f}) | {m('beat_cost'):.4f} | {m('beat_cost') * monthly:,.0f} |",
        f"| C. ffmpeg x{rows[0]['segs']} slices + concat on Fargate | {m('ff'):.0f} | {m('ff_cost'):.4f} | {m('ff_cost') * monthly:,.0f} |",
        f"| End-to-end (A, B, C in parallel) | {m('e2e'):.0f} | | |",
        "",
        f"Recommended path A+B: ${m('mc_cost') + m('beat_cost'):.4f}/video, ${(m('mc_cost') + m('beat_cost')) * monthly:,.0f}/month.",
        "Fargate times include task start-up (image pull); MediaConvert time is submit -> finish.",
        "Not included: S3 storage, Step Functions transitions (~$0.025 per 1k), CloudWatch logs - all small.",
    ]
    report = "\n".join(lines)
    print("\n" + report)
    for r in rows:
        print(f"  {r['job']}: e2e {r['e2e']:.0f}s | MC {r['mc']:.0f}s | beat {r['beat']:.0f}s ({r['n_beats']} beats) | ffmpeg {r['ff']:.0f}s")
    key = f"results/{a.region}-{int(a.since)}.md"
    s3.put_object(Bucket=bucket, Key=key, Body=report.encode())
    print(f"\nsaved s3://{bucket}/{key}")


if __name__ == "__main__":
    main()
