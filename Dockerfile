# AgentCore Runtime（microVM）用。microVM は Linux arm64 コンテナ、イメージは 2GB まで。
# モデルのファイル（約4.5GB）はイメージに入れず、起動時に S3 から取得する。
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir \
      "strands-decider @ git+https://github.com/strands-labs/strands-decider@b81d443c07438dc5ed51d0e85f9db184ad65d809" \
      bedrock-agentcore \
    && pip freeze > /pip-freeze.txt

# 起動時に S3 からモデルのファイルを取得するため（bedrock-agentcore の依存に含まれていても、明示しておく）
RUN pip install --no-cache-dir boto3

WORKDIR /app
COPY app/agent.py /app/agent.py

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "agent.py"]
