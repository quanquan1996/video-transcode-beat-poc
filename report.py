"""Wait for pipeline executions, then report latency and cost per route, per video and per month.

Prices: AWS Price List API, on-demand, pulled 2026-11-11. Re-check before quoting:
  https://aws.amazon.com/mediaconvert/pricing/  https://aws.amazon.com/fargate/pricing/
  https://aws.amazon.com/lambda/pricing/        https://aws.amazon.com/ec2/pricing/on-demand/
"""
import argparse, datetime as dt, json, math, statistics, time

import boto3

PRICES = {
    # mc_*: MediaConvert AVC/H.264 HD output (720p = HD), $ per output minute by fps band. Basic vs Professional tier.
    # vcpu/gb: Fargate Linux x86 on-demand per hour. lambda_*: per GB-second (tier 1). gpu: EC2 on-demand per hour.
    "us-west-1": {"mc_basic": {30: 0.017, 60: 0.0212, 120: 0.0255}, "mc_pro": {30: 0.0272, 60: 0.034, 120: 0.0408},
                  "vcpu": 0.04656, "gb": 0.00511, "lambda_x86": 0.0000166667, "lambda_arm64": 0.0000133334,
                  "gpu": {"g4dn.xlarge": 0.631, "g4dn.2xlarge": 0.902}},
    "us-west-2": {"mc_basic": {30: 0.015, 60: 0.0188, 120: 0.0225}, "mc_pro": {30: 0.024, 60: 0.03, 120: 0.036},
                  "vcpu": 0.04048, "gb": 0.004445, "lambda_x86": 0.0000166667, "lambda_arm64": 0.0000133334,
                  "gpu": {"g4dn.xlarge": 0.526, "g4dn.2xlarge": 0.752, "g5.xlarge": 1.006, "g6.xlarge": 0.8048,
                          "g6.2xlarge": 0.9776, "g6e.xlarge": 1.861, "g6f.large": 0.202, "g6f.xlarge": 0.2375,
                          "g6f.2xlarge": 0.475, "g6f.4xlarge": 0.95}},
}
FLEET_UTIL = 0.7  # plan always-on GPU fleets at 70 % busy


def ts(v):
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def fargate(task, p):
    secs = max(60.0, ts(task["stoppedAt"]) - ts(task["createdAt"]))  # billed from image pull, 1-minute minimum
    return secs / 3600 * (int(task["cpu"]) / 1024 * p["vcpu"] + int(task["memory"]) / 1024 * p["gb"]), secs


def lam(t, price):
    secs = t["ended"] - ts(t["entered"])  # wall from Step Functions state entry: includes cold start
    return secs * t["memory_mb"] / 1024 * price, secs, t


def band(fps):
    return 30 if fps <= 30 else 60 if fps <= 60 else 120


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--stack", default="beat-poc")
    ap.add_argument("--since", type=int, required=True)
    ap.add_argument("--until", type=int, default=0)
    ap.add_argument("--expect", type=int, default=1)
    ap.add_argument("--per-day", type=int, default=500)
    a = ap.parse_args()
    p = PRICES[a.region]
    cfn, sfn, s3 = (boto3.client(x, region_name=a.region) for x in ("cloudformation", "stepfunctions", "s3"))
    outs = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=a.stack)["Stacks"][0]["Outputs"]}
    sm, bucket = outs["StateMachineArn"], outs["BucketName"]

    print(f"== waiting for {a.expect} execution(s)")
    while True:
        ex = [e for e in sfn.list_executions(stateMachineArn=sm, maxResults=100)["executions"]
              if e["startDate"].timestamp() >= a.since - 5 and (not a.until or e["startDate"].timestamp() <= a.until)]
        done = [e for e in ex if e["status"] != "RUNNING"]
        print(f"   {len(done)}/{len(ex)} finished ({int(time.time() - a.since)} s)", flush=True)
        if len(ex) >= a.expect and len(done) == len(ex):
            break
        time.sleep(30)

    routes = {}  # name -> list of dicts(transcode_s, transcode_cost, beat_s, beat_cost, note)
    probe = None

    def add(name, **kw):
        routes.setdefault(name, []).append(kw)

    def timing(job, route, name):
        try:
            return json.loads(s3.get_object(Bucket=bucket, Key=f"output/{job}/{route}/timing/{name}.json")["Body"].read())
        except s3.exceptions.NoSuchKey:
            return {}

    for e in ex:
        d = sfn.describe_execution(executionArn=e["executionArn"])
        if d["status"] != "SUCCEEDED":
            print("FAILED:", e["name"], d.get("error"), (d.get("cause") or "")[:800])
            continue
        inp = json.loads(d["input"])
        job = inp["id"]
        mc, fg, lx, la, gpu = json.loads(d["output"])
        for nm, br in (("mediaconvert", mc), ("fargate", fg), ("lambda-x86", lx), ("lambda-arm64", la), ("gpu", gpu)):
            if isinstance(br, dict) and "failed" in br:
                print(f"ROUTE FAILED {nm}: {br['failed'].get('Error')} {str(br['failed'].get('Cause'))[:600]}")
        skip = lambda br: isinstance(br, dict) and ("skipped" in br or "failed" in br)

        if not skip(fg):
            beat_task, sl = fg
            bt = timing(job, "fargate", "beat")
            probe = probe or bt.get("probe")
            bc, bs = fargate(beat_task, p)
            seg = [fargate(t, p) for t in sl["segments"]]
            cc, _ = fargate(sl["concat"], p)
            secs = ts(sl["concat"]["stoppedAt"]) - min(ts(t["createdAt"]) for t in sl["segments"])
            add(f"Fargate x86 ({len(seg)} slices x {int(sl['segments'][0]['cpu']) // 1024} vCPU)",
                transcode_s=secs, transcode_cost=sum(c for c, _ in seg) + cc, beat_s=bs, beat_cost=bc)

        for out, arch in ((lx, "x86"), (la, "arm64")):
            if skip(out):
                continue
            beat_r, sl = out
            price = p[f"lambda_{arch}"]
            bc, bs, bt = lam(beat_r["t"], price)
            probe = probe or bt.get("probe")
            seg = [lam(x["t"], price) for x in sl["segments"]]
            cc, _, ct = lam(sl["concat"]["t"], price)
            secs = ct["ended"] - min(ts(x["t"]["entered"]) for x in sl["segments"])
            add(f"Lambda {arch} ({len(seg)} slices x 10 GB)", transcode_s=secs,
                transcode_cost=sum(c for c, _, _ in seg) + cc, beat_s=bs, beat_cost=bc)

        if not skip(gpu):
            for g in inp["gpus"]:
                t = timing(job, f"gpu-{g['type']}", "gpu")
                if not t:
                    add(f"GPU {g['type']}", transcode_s=float("nan"), transcode_cost=float("nan"), note="no result")
                    continue
                probe = probe or t.get("probe")
                hourly = p["gpu"].get(g["type"], float("nan"))
                secs = t["total_s"]  # warm instance: task run time (image cached after first run)
                add(f"GPU {g['type']} ({t.get('gpu', '').split(',')[0]})", transcode_s=secs,
                    transcode_cost=secs / 3600 * hourly, hourly=hourly, gpu_s=t.get("transcode_s"))

        if not skip(mc):
            fps = (probe or {}).get("fps", 30)
            mins = mc["outputs"][0]["OutputDetails"][0]["DurationInMs"] / 60000
            pro = mc.get("accelStatus") == "ACCELERATED" or (probe or {}).get("hdr")
            add(f"MediaConvert ({'Pro' if pro else 'Basic'}{', accelerated' if mc.get('accelStatus') == 'ACCELERATED' else ''})",
                transcode_s=ts(mc["timing"]["FinishTime"]) - ts(mc["timing"]["SubmitTime"]),
                transcode_cost=mins * p["mc_pro" if pro else "mc_basic"][band(fps)])

    if not routes:
        return
    monthly = a.per_day * 30
    m = lambda rows, k: statistics.mean(r[k] for r in rows if k in r and not math.isnan(r[k])) if any(
        k in r and not math.isnan(r[k]) for r in rows) else float("nan")
    lines = [f"# Results: {a.region}, {len(ex)} video(s) in parallel",
             f"Source: {probe['codec']} {probe['width']}x{probe['height']} @ {probe['fps']} fps"
             f"{' HDR' if probe.get('hdr') else ''}, {probe['duration'] / 60:.1f} min" if probe else "",
             "", f"| Route | transcode wall (s) | transcode $/video | beat wall (s) | beat $/video | $/month @ {a.per_day}/day (transcode+beat) |",
             "|---|---|---|---|---|---|"]
    for name, rows in routes.items():
        tc, bc, bs = m(rows, "transcode_cost"), m(rows, "beat_cost"), m(rows, "beat_s")
        tot = tc + (0 if math.isnan(bc) else bc)
        beat_s = "" if math.isnan(bs) else f"{bs:.0f}"
        beat_c = "" if math.isnan(bc) else f"{bc:.4f}"
        lines.append(f"| {name} | {m(rows, 'transcode_s'):.0f} | {tc:.4f} | {beat_s} | {beat_c} | {tot * monthly:,.0f} |")
    gpu_rows = {k: v for k, v in routes.items() if k.startswith("GPU") and not math.isnan(m(v, "transcode_s"))}
    if gpu_rows:
        lines += ["", f"GPU always-on fleet for {a.per_day}/day (warm instances, {int(FLEET_UTIL * 100)} % busy):",
                  "| GPU | videos/hour/instance | instances | $/month (730 h) |", "|---|---|---|---|"]
        for k, v in gpu_rows.items():
            vph = 3600 / m(v, "transcode_s")
            n = max(1, math.ceil(a.per_day / 24 / (vph * FLEET_UTIL)))
            lines.append(f"| {k} | {vph:.1f} | {n} | {n * v[0]['hourly'] * 730:,.0f} |")
    lines += ["", "Fargate: billed from image pull (cold start included). Lambda: wall from state entry (cold start included).",
              "GPU: task run time on a warm instance; beat_this stays on CPU (cheapest). MediaConvert: submit -> finish;"
              " HDR->SDR tone mapping assumed Professional tier.",
              "Not included: S3 storage, Step Functions transitions, CloudWatch logs - all small."]
    report = "\n".join(lines)
    print("\n" + report)
    key = f"results/{a.region}-{int(a.since)}.md"
    s3.put_object(Bucket=bucket, Key=key, Body=report.encode())
    print(f"\nsaved s3://{bucket}/{key}")


if __name__ == "__main__":
    main()
