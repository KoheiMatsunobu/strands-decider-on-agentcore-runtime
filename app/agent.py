"""AgentCore Runtime（microVM）で Strands Decider を動かす最小のエージェント。

起動時の処理（モデルのファイルの取得・重みの読み込み・ウォームアップ）は、すべて app.run() の前に行う。
V2 ではこの状態がスナップショットに入る。
"""
import faulthandler
import os
import time
from pathlib import Path

PROCESS_STARTED_AT = time.time()

# 不正な命令（SIGILL）やメモリ違反（SIGSEGV）で落ちたときに、その旨とスタックをログに残す
faulthandler.enable(all_threads=True)

# MODEL_S3_URI があれば、Hugging Face のキャッシュ（snapshots/ と refs/）を S3 から取得して、
# Hugging Face には接続せずに読み込む。例: s3://[バケット名]/hf-cache/hub/
MODEL_S3_URI = os.environ.get("MODEL_S3_URI")
HF_HUB_DIR = Path(os.environ.get("HF_HOME", "/root/.cache/huggingface")) / "hub"


def cpu_info():
    """この microVM の CPU コアの種類（CPU part）と、bf16 / SVE の命令に対応しているか。"""
    part, features = None, set()
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                key, _, value = line.partition(":")
                if key.strip() == "CPU part" and part is None:
                    part = value.strip()
                elif key.strip() == "Features" and not features:
                    features = set(value.split())
    except OSError:
        pass
    return {"cpu_part": part, "bf16": "bf16" in features, "sve": "sve" in features}


def download_hf_cache_from_s3(uri, dest):
    import boto3
    from boto3.s3.transfer import TransferConfig

    bucket, _, prefix = uri.removeprefix("s3://").partition("/")
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "ap-northeast-1"))
    config = TransferConfig(max_concurrency=16, multipart_chunksize=64 * 1024 * 1024)
    total = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            path = dest / obj["Key"][len(prefix):]
            path.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, obj["Key"], str(path), Config=config)
            total += obj["Size"]
    return total


INIT_DOWNLOAD_S = None
if MODEL_S3_URI:
    # huggingface_hub は import 時にこの環境変数を読むので、import より前に設定する
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    t = time.time()
    size = download_hf_cache_from_s3(MODEL_S3_URI, HF_HUB_DIR)
    INIT_DOWNLOAD_S = time.time() - t
    print(f"[init] s3 download={INIT_DOWNLOAD_S:.1f}s size={size / 1024**3:.2f}GiB", flush=True)

import torch
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands_decider.infer import SystemOneEngine, load_engine
from strands_decider.schema import SystemOneRequest

MODEL_ID = os.environ.get("MODEL_ID", "StrandsAgents/strands-decider-2B-qwen3.5-v1-2610")

# CPU では torso が fp32 に変換される（約7GiB）。8GB の microVM に収めるため bf16 のまま動かす。
SystemOneEngine._upcast_torso_for_cpu = lambda self: None

torch.set_num_threads(int(os.environ.get("TORCH_THREADS", "2")))

INIT_CPU = {**cpu_info(), "torch_capability": torch.backends.cpu.get_cpu_capability(),
            "mkldnn": torch.backends.mkldnn.enabled}
print(f"[init] cpu {INIT_CPU}", flush=True)

WARMUP = {
    "state": "Help! My payouts have been failing for 3 days!",
    "questions": {"is_urgent": {"type": "noul", "instructions": "Does this convey urgency?"}},
}


def _meminfo_gib(key, path):
    with open(path) as f:
        for line in f:
            if line.startswith(key):
                return round(int(line.split()[1]) / 1024 / 1024, 2)
    return None


def memory():
    return {
        "rss_gib": _meminfo_gib("VmRSS:", "/proc/self/status"),
        "mem_available_gib": _meminfo_gib("MemAvailable:", "/proc/meminfo"),
    }


t = time.time()
ENGINE = load_engine(MODEL_ID, device="cpu")
INIT_LOAD_S = time.time() - t

# スナップショットの前に1回推論して、推論で使うメモリやコードを温めておく
t = time.time()
ENGINE.evaluate(SystemOneRequest.model_validate(WARMUP))
INIT_WARMUP_S = time.time() - t

# わざと起動時に作る値。V2 ではスナップショットに入るので、全セッションで同じ値になる（V1 ではセッションごとに違う）
INIT_ID = os.urandom(8).hex()
INIT_DONE_AT = time.time()
print(
    f"[init] load={INIT_LOAD_S:.1f}s warmup={INIT_WARMUP_S:.1f}s "
    f"total={INIT_DONE_AT - PROCESS_STARTED_AT:.1f}s {memory()}",
    flush=True,
)

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload):
    started = time.perf_counter()
    now_cpu = cpu_info()
    print(f"[req] cpu {now_cpu} (init: {INIT_CPU})", flush=True)
    body = {k: payload[k] for k in ("state", "questions") if k in payload} or WARMUP
    result = ENGINE.evaluate(SystemOneRequest.model_validate(body)).model_dump(mode="json")
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    result["diag"] = {
        "init_id": INIT_ID,
        "init_download_s": round(INIT_DOWNLOAD_S, 1) if INIT_DOWNLOAD_S is not None else None,
        "init_load_s": round(INIT_LOAD_S, 1),
        "init_warmup_s": round(INIT_WARMUP_S, 1),
        "init_total_s": round(INIT_DONE_AT - PROCESS_STARTED_AT, 1),
        # V2 では「スナップショットを取ってからの経過時間」になる
        "seconds_since_init": round(time.time() - INIT_DONE_AT, 1),
        **memory(),
        "init_cpu": INIT_CPU,
        "now_cpu": now_cpu,
    }
    return result


if __name__ == "__main__":
    # ここから先で /ping に応答できるようになる
    print(f"[init] app.run at {time.strftime('%H:%M:%S')} (process +{time.time() - PROCESS_STARTED_AT:.1f}s)", flush=True)
    app.run()
