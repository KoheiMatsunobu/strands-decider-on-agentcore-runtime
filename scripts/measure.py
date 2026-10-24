"""AgentCore Runtime に載せた Decider を、新しいセッションで同時に N 個呼び出して計測する。

使い方:
  python3 measure.py --arn [ランタイムARN] --label v1 --n 15
"""
import argparse
import csv
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from botocore.config import Config

QUESTION = {
    "state": "Help! My payouts have been failing for 3 days!",
    "questions": {"is_urgent": {"type": "noul", "instructions": "Does this convey urgency?"}},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arn", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--n", type=int, default=15)
    ap.add_argument("--region", default="ap-northeast-1")
    args = ap.parse_args()

    client = boto3.client(
        "bedrock-agentcore", region_name=args.region,
        config=Config(read_timeout=900, connect_timeout=30, retries={"max_attempts": 0},
                      max_pool_connections=max(10, args.n)),
    )

    def one(i):
        sid = f"decider-{uuid.uuid4().hex}"  # runtimeSessionId は33文字以上
        t0 = time.perf_counter()
        try:
            res = client.invoke_agent_runtime(
                agentRuntimeArn=args.arn, runtimeSessionId=sid,
                payload=json.dumps(QUESTION).encode(),
                contentType="application/json", accept="application/json",
            )
            body = json.loads(res["response"].read())
            diag = body["diag"]
            row = {"i": i, "client_ms": round((time.perf_counter() - t0) * 1000, 1),
                   "server_ms": body["latency_ms"], "init_total_s": diag["init_total_s"],
                   "seconds_since_init": diag["seconds_since_init"], "init_id": diag["init_id"],
                   "cpu_part": diag["now_cpu"]["cpu_part"], "error": ""}
        except Exception as e:
            row = {"i": i, "client_ms": None, "error": str(e)[:200]}
        finally:
            # 測り終えたセッションは止めて、アイドル中の課金を避ける
            try:
                client.stop_runtime_session(agentRuntimeArn=args.arn, runtimeSessionId=sid)
            except Exception:
                pass
        return row

    print(f"{args.n} 個のセッションを同時に呼び出します...")
    with ThreadPoolExecutor(max_workers=args.n) as pool:
        rows = sorted(pool.map(one, range(args.n)), key=lambda r: 1e12 if r["client_ms"] is None else r["client_ms"])
    for r in rows:
        if r["error"]:
            print(f"  #{r['i']}: エラー {r['error']}")
        else:
            # since_init が大きい＝前もって起動処理を終えていた microVM、小さい＝この呼び出しのために起動した microVM
            print(f"  #{r['i']}: {r['client_ms']:.0f} ms（server {r['server_ms']} ms）"
                  f"init_total={r['init_total_s']}s since_init={r['seconds_since_init']}s cpu={r['cpu_part']}")

    out = Path("results") / f"{args.label}-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    out.parent.mkdir(exist_ok=True)
    fields = ["i", "client_ms", "server_ms", "init_total_s", "seconds_since_init", "init_id", "cpu_part", "error"]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, restval="")
        w.writeheader()
        w.writerows(rows)
    print(f"\n保存: {out}")


if __name__ == "__main__":
    main()
