"""Hugging Face のモデルのファイルを、版（コミット）を固定したまま S3 にコピーする（AWS CloudShell で実行）。

ローカルのディスクを使わず、Hugging Face から読んだデータをそのまま S3 に流し込む。
コピーしながらハッシュを計算し、Hugging Face が公開している値と一致するかを確かめる。
S3 には Hugging Face のキャッシュと同じ構成（models--*/snapshots/[コミット]/...、refs/main）で置く。

S3 に同じものがすでにあるファイルは飛ばすので、何度実行してもよい。

使い方:
  python3 hf_to_s3.py --bucket [バケット名] --prefix hf-cache/hub/
"""
import argparse
import hashlib
import json
import subprocess
import time
import urllib.request

import boto3
from boto3.s3.transfer import TransferConfig

# Decider の読み込み処理が実際に使うファイルだけをコピーする
MANIFEST = {
    "Qwen/Qwen3.5-2B-Base": {
        "revision": "b1485b2fa6dfa1287294f269f5fb618e03d52d7c",
        "files": [
            "LICENSE",
            "config.json",
            "model.safetensors-00001-of-00001.safetensors",
            "model.safetensors.index.json",
        ],
        "refs": {},
    },
    "StrandsAgents/strands-decider-2B-qwen3.5-v1-2610": {
        "revision": "216d1ccb46a9e9b036eb9187b248db98480f77d4",
        "files": [
            ".gitattributes", "LICENSE.md", "MANIFEST.sha256", "README.md", "chat_template.jinja",
            "eval/internal/eval.log", "eval/internal/recipe_heldout.json",
            "eval/jevbench-w4096/health.json", "eval/jevbench-w4096/manifest.json",
            "eval/jevbench-w4096/paired.json", "eval/jevbench-w4096/paired.txt",
            "eval/jevbench-w4096/results.jsonl", "eval/jevbench-w4096/run_meta.json",
            "eval/jevbench-w4096/summary.json", "eval/summary.json",
            "head.safetensors", "lora/adapter_config.json", "lora/adapter_model.safetensors",
            "provenance.json", "strands_decider_config.json", "tokenizer.json", "tokenizer_config.json",
            "train_config.json", "training/configs/v1-qwen3.5-2b-s0.yaml",
            "training/configs/v1-qwen3.5-2b-s1.yaml", "training/configs/v1-qwen3.5-2b-s2.yaml",
            "training/data_sha256.txt", "training/sources.md", "training/stages.jsonl",
        ],
        "refs": {"main": "216d1ccb46a9e9b036eb9187b248db98480f77d4"},
    },
}


class HashingReader:
    """読みながら sha256（LFS のファイル用）と git の blob ハッシュ（それ以外用）を計算する。"""

    def __init__(self, raw, size):
        self.raw = raw
        self.sha256 = hashlib.sha256()
        self.git_sha1 = hashlib.sha1(f"blob {size}\0".encode())
        self.read_bytes = 0

    def read(self, n=-1):
        data = self.raw.read(n)
        self.sha256.update(data)
        self.git_sha1.update(data)
        self.read_bytes += len(data)
        return data


def already_copied(s3, bucket, key, size, expected_oid):
    """S3 に同じサイズのものがあり、記録したハッシュが一致すれば（記録がなければサイズだけで）コピー済みとみなす。"""
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except s3.exceptions.ClientError:
        return False
    recorded = head.get("Metadata", {}).get("hf-oid")
    return head["ContentLength"] == size and recorded in (None, expected_oid)


def tree(repo, revision):
    url = f"https://huggingface.co/api/models/{repo}/tree/{revision}?recursive=true"
    with urllib.request.urlopen(url, timeout=60) as res:
        return {e["path"]: e for e in json.load(res) if e["type"] == "file"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="hf-cache/hub/")
    ap.add_argument("--region", default="ap-northeast-1")
    args = ap.parse_args()

    s3 = boto3.client("s3", region_name=args.region)
    config = TransferConfig(multipart_chunksize=64 * 1024 * 1024, max_concurrency=8)
    total, started = 0, time.time()

    for repo, spec in MANIFEST.items():
        rev = spec["revision"]
        base = f"{args.prefix}models--{repo.replace('/', '--')}/"
        entries = tree(repo, rev)
        for path in spec["files"]:
            entry = entries[path]
            key = f"{base}snapshots/{rev}/{path}"
            expected = entry["lfs"]["oid"] if "lfs" in entry else entry["oid"]
            if already_copied(s3, args.bucket, key, entry["size"], expected):
                print(f"SKIP {repo}/{path}（S3 に同じものがあります）", flush=True)
                continue
            url = f"https://huggingface.co/{repo}/resolve/{rev}/{path}"
            metadata = {"hf-repo": repo, "hf-revision": rev, "hf-oid": expected}
            t = time.time()
            for attempt in range(1, 4):
                # ダウンロードは curl に任せる（リダイレクト先の CDN からの受信が安定するため）
                with subprocess.Popen(["curl", "-sfL", url], stdout=subprocess.PIPE) as proc:
                    reader = HashingReader(proc.stdout, entry["size"])
                    s3.upload_fileobj(reader, args.bucket, key, Config=config, ExtraArgs={"Metadata": metadata})
                if "lfs" in entry:
                    ok = reader.sha256.hexdigest() == entry["lfs"]["oid"]
                else:
                    ok = reader.git_sha1.hexdigest() == entry["oid"]
                if proc.returncode == 0 and reader.read_bytes == entry["size"] and ok:
                    break
                print(f"NG {repo}/{path}（{attempt}回目：{reader.read_bytes}/{entry['size']} bytes）。やり直します", flush=True)
            else:
                # 一致しないものを最新版として残さない（バージョニングにより、前の版は残る）
                s3.delete_object(Bucket=args.bucket, Key=key)
                raise SystemExit(f"3回やり直しても一致しません: {repo}/{path}")
            sec = time.time() - t
            total += entry["size"]
            print(f"OK {repo}/{path} {entry['size'] / 1024**2:.1f}MiB {sec:.1f}s "
                  f"({entry['size'] / 1024**2 / max(sec, 0.001):.0f}MiB/s)", flush=True)
        for name, value in spec["refs"].items():
            s3.put_object(Bucket=args.bucket, Key=f"{base}refs/{name}", Body=value.encode())
            print(f"OK {repo} refs/{name} -> {value}", flush=True)

    print(f"\n完了: {total / 1024**3:.2f}GiB、{time.time() - started:.0f}秒")


if __name__ == "__main__":
    main()
