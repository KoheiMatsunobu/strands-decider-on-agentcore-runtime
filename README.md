# strands-decider-on-agentcore-runtime

判定専用の小さなモデル [Strands Decider 2B](https://strandsagents.com/blog/introducing-strands-decider/) を、Amazon Bedrock AgentCore Runtime（microVM）の V1 / V2 に同じコンテナで載せ、新しいセッションの立ち上がりを比べるための検証コードです。

- 記事：[タイトル未定](https://builder.aws.com/[記事のURL])
- リージョン：東京（ap-northeast-1）
- 構成：VPC（NAT ゲートウェイなし）＋ VPC エンドポイント。モデルのファイルは Hugging Face から S3 にコピーし、実行時は Hugging Face に接続しない

> ⚠️ 本リポジトリの内容は筆者個人の見解であり、所属する組織および AWS の公式な見解を示すものではありません。2026年10月時点の情報です。

> ⚠️ 検証の結果、この構成の **V2 はランタイムの作成に失敗します**（作成時に Graviton3 と Graviton2 の両方で起動処理が走り、bf16 の命令がない Graviton2 でウォームアップが120秒に間に合わないため）。詳しくは記事をご覧ください。

## 構成

```
.
├── Dockerfile              # microVM 用（linux/arm64）。ベースイメージは ECR Public
├── app/agent.py            # エージェント本体（起動時に S3 から取得 → bf16 で読み込み → ウォームアップ）
├── scripts/hf_to_s3.py     # Hugging Face から S3 へ、版を固定してコピー（CloudShell で実行）
└── scripts/measure.py      # 新しいセッションを同時に N 個呼び出して計測
```

## 前提条件
- Mac OSで作業します(Apple siliconを想定しています)
- AWS CLIの`create-agent-runtime`が`--platform-version`に対応していること(今回はAWS CLI 2.38.5を使用。2.36.41では未対応でした)
- Docker Desktopがインストールされていること。ローカルで動作確認する場合は、**Settings → Resources → Memoryを10GB以上**にしておきます(コンテナに8GBを割り当てるため)
- Python 3.12と、boto3(1.43.116で確認)がインストールされていること
- リージョンは東京(ap-northeast-1)を使います
- 角括弧の部分(例：[アカウントID])は、ご自身の環境の値に置き換えてください

AWS CLIが`--platform-version`に対応しているかは、次のコマンドで確認できます。

```bash
aws bedrock-agentcore-control create-agent-runtime help | col -b | grep -A2 platform-version
```

`--platform-version`の説明が表示されれば対応しています。何も表示されない場合は、AWS CLIを最新版に更新してください。

> 📖 公式ドキュメント：[Installing or updating to the latest version of the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)

このあとのコマンドで使う値を、変数に入れておきます。**ステップ1(CloudShell)を除き、最後まで同じターミナルで実行してください。**

```bash
export AWS_REGION=ap-northeast-1
```

```bash
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
```

```bash
export MODEL_BUCKET=decider-models-${ACCOUNT_ID}-apne1 && echo $MODEL_BUCKET
```

> ⚠️ zshでは、`$ACCOUNT_ID:repository`のように変数の直後に`:r`や`:l`が続くと、変数を加工する指示として解釈され、`:r`や`:l`が消えてしまいます(実際にこれでIAMポリシーのARNが壊れ、`MalformedPolicyDocument`になりました)。この記事のコマンドでは、`${ACCOUNT_ID}:`のように波かっこで囲んでいます。

## ステップ1：モデルのファイルをS3にコピーする
モデルのファイルを置くS3バケット(以下、モデル用のバケット)を作り、バージョニングを有効にします(上書きや削除をしても、前の版が残るようにするため)。

```bash
aws s3api create-bucket --bucket $MODEL_BUCKET --region $AWS_REGION --create-bucket-configuration LocationConstraint=$AWS_REGION
```

```bash
aws s3api put-bucket-versioning --bucket $MODEL_BUCKET --versioning-configuration Status=Enabled
```

> 💡 新しく作るバケットは、パブリックアクセスのブロックと暗号化(SSE-S3)が最初から有効になっています。

モデルのファイルのコピーは、**AWS CloudShell(東京リージョン)**で行います。CloudShellなら、Hugging FaceからS3へ、AWSの中で直接コピーできます。

CloudShellで、このリポジトリを取得します。

```bash
git clone https://github.com/KoheiMatsunobu/strands-decider-on-agentcore-runtime.git && cd strands-decider-on-agentcore-runtime
```

コピー用のスクリプトは`scripts/hf_to_s3.py`です。ポイントは3つです。

- **版を固定する**：Hugging Faceのコミットを指定して取得し、S3にはHugging Faceのキャッシュと同じ構成(`models--[モデル名]/snapshots/[コミットID]/...`)で置きます。エージェント側は、これをそのままキャッシュとして読み込めます
- **ディスクを使わない**：Hugging Faceから読んだデータを、そのままS3に流し込みます。CloudShellのディスク容量(1GB)を気にしなくて済みます
- **ハッシュを照合する**：コピーしながら、LFSのファイルはsha256、それ以外はgitのハッシュを計算し、Hugging Faceが公開している値と照合します。結果はS3のメタデータ(`hf-repo`、`hf-revision`、`hf-oid`)にも残します

> 💡 最初はPythonの標準ライブラリ(urllib)でダウンロードしていましたが、CDNからの受信が途中で途切れても、エラーにならずに終わってしまうことがありました。ハッシュの照合でそれに気づけたので、ダウンロードをcurlに任せる形に直しています。取り込み時の照合は、「壊れたモデルのファイルで起動してしまう」事故を防ぐ保険になります。

コピーを実行します。

```bash
python3 scripts/hf_to_s3.py --bucket decider-models-$(aws sts get-caller-identity --query Account --output text)-apne1
```

以下のような出力結果が表示されます。

```
OK Qwen/Qwen3.5-2B-Base/LICENSE 0.0MiB 0.5s (0MiB/s)
OK Qwen/Qwen3.5-2B-Base/config.json 0.0MiB 0.3s (0MiB/s)
OK Qwen/Qwen3.5-2B-Base/model.safetensors-00001-of-00001.safetensors 4337.5MiB 193.4s (22MiB/s)
OK Qwen/Qwen3.5-2B-Base/model.safetensors.index.json 0.1MiB 0.3s (0MiB/s)
OK StrandsAgents/strands-decider-2B-qwen3.5-v1-2610/.gitattributes 0.0MiB 0.3s (0MiB/s)
...
OK StrandsAgents/strands-decider-2B-qwen3.5-v1-2610/lora/adapter_model.safetensors 192.5MiB 13.7s (14MiB/s)
...
OK StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 refs/main -> 216d1ccb46a9e9b036eb9187b248db98480f77d4

完了: 4.45GiB、226秒
```

4.45GiBを、約4分でコピーできました。

> 📖 公式ドキュメント：[AWS CloudShell User Guide](https://docs.aws.amazon.com/cloudshell/latest/userguide/welcome.html) / [put-bucket-versioning(AWS CLI Command Reference)](https://docs.aws.amazon.com/cli/latest/reference/s3api/put-bucket-versioning.html)

## ステップ2：エージェントのコードを用意する
ここからは、手元のMacのターミナルで作業します。このリポジトリを取得します。

```bash
git clone https://github.com/KoheiMatsunobu/strands-decider-on-agentcore-runtime.git && cd strands-decider-on-agentcore-runtime
```

エージェントのコードは`app/agent.py`です。

このコードのポイントは4つです。

**ポイント①：起動時の処理は、すべて`app.run()`の前に書く**

S3からの取得、モデルの読み込み(`load_engine`)、ウォームアップの推論は、関数の外(モジュールの読み込み時)に書いています。AgentCore Pythonの`BedrockAgentCoreApp`は、`app.run()`を呼ぶまでポート8080で待ち受けを始めません。つまり、起動時の処理が終わるまで`/ping`は成功せず、V2のスナップショットは「読み込みもウォームアップも済んだ状態」で取られます。これは、公式ドキュメントで案内されている書き方そのままです。

**ポイント②：S3からモデルのファイルを取得して、Hugging Faceには接続しない**

環境変数`MODEL_S3_URI`があれば、S3からHugging Faceのキャッシュと同じ構成でファイルを取得し、`HF_HUB_OFFLINE=1`で読み込みます。`huggingface_hub`はimportするときにこの環境変数を読むので、importより前に設定しているのがポイントです。

**ポイント③：CPUでのfp32への変換を止めて、bf16のまま動かす**

`SystemOneEngine._upcast_torso_for_cpu`を何もしない関数に差し替えています。Deciderは、CPUで動かすとベースモデルを自動でfp32に変換しますが、そうすると重みだけで約7GiBになり、8GBのmicroVMでは余裕がありません。bf16のままなら約3.5GiBで済みます。

> ⚠️ `_upcast_torso_for_cpu`は、先頭に`_`が付いた内部向けのメソッドです。今回は検証のための割り切りとして差し替えていますが、Deciderのバージョンが変わると動かなくなる可能性があります。今回は動作を確認したコミットに固定してインストールしています。

**ポイント④：`init_id`とCPUの情報で、何が起きたかを確かめる**

`INIT_ID`は、起動時に1回だけ作る乱数です。V1では新しいセッションのたびに起動時の処理が走るので、セッションごとに違う値になります。V2では、起動時の処理の結果がスナップショットごと引き継がれるので、すべてのセッションで同じ値になるはずです。本来、乱数は起動時に作ってはいけない値の代表例ですが(公式ドキュメントでも、リクエストの処理の中で作るよう案内されています)、今回はその性質を逆手に取って、復元されたかどうかの目印にしています。

あわせて、起動時とリクエストのたびに、`/proc/cpuinfo`から、CPUコアの種類を表す番号(`CPU part`)と、bf16の命令に対応しているかをログに出しています。**この仕掛けが、記事で紹介する原因の特定に決め手になりました。**

> 📖 公式ドキュメント：[Optimize your agent for AgentCore Runtime V2](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html)

## ステップ3：Dockerfileを用意してビルドする
Dockerfileは、リポジトリ直下の`Dockerfile`です。

> 💡 ベースイメージは、Docker Hubではなく**Amazon ECR Public**から取得しています。Docker公式イメージのミラー(`public.ecr.aws/docker/library/...`)で、Docker Hubのダウンロード回数の制限を受けません。digest(`@sha256:...`)まで指定しているので、タグの中身が更新されても同じイメージでビルドできます(このdigestは、Docker Hubの`python:3.12-slim`と同じものでした)。

イメージをビルドします。torchのダウンロードを含むため、10分前後かかります。

```bash
docker build -t decider-agentcore:latest .
```

> 💡 ビルドの途中に`FromPlatformFlagConstDisallowed`という警告が出ますが、microVMに合わせてarm64に固定するため、意図して`--platform=linux/arm64`と書いています。動作には影響しません。

イメージのサイズを確認します。

```bash
docker image ls decider-agentcore:latest
```

以下のような出力結果が表示されます。

```
IMAGE                 ID             DISK USAGE   CONTENT SIZE   EXTRA
decider-agentcore:latest   [イメージID]       1.83GB          393MB
```

モデルのファイルを含まない状態で1.83GB(圧縮後393MB)で、AgentCore Runtimeの上限である2GBに収まっています。

## ステップ4：ローカルで動作確認する(任意)
AWSに載せる前に、microVMと同じ**2vCPU・8GB(スワップなし)**、かつ**ネットワークなし**の条件で、手元で動くかを確かめておきます。S3にコピーしたモデルのファイルを、Macにダウンロードします(約4.5GB)。

```bash
aws s3 sync s3://$MODEL_BUCKET/hf-cache/ ~/.cache/decider-offline/
```

ネットワークを切った状態(`--network none`)で起動します。Hugging Faceに接続しようとするとエラーになるので、S3と同じ構成のキャッシュだけで読み込めるかを確かめられます。

```bash
docker run --rm --network none --cpus 2 --memory 8g --memory-swap 8g -e HF_HUB_OFFLINE=1 -v "$HOME/.cache/decider-offline:/root/.cache/huggingface" decider-agentcore:latest
```

`[init] cpu ...`、`[init] load=... warmup=... total=...`、`[init] app.run at ...`の3行が出れば成功です(出力の例は記事を参照)。

ネットワークなしでも読み込めて、メモリ(RSS)は3.43GiBと、8GBに十分収まっています。確認できたら`Ctrl + C`で止めます。

> 💡 `mem_available_gib`は、Docker Desktopの仮想マシン全体の空きメモリです。コンテナに割り当てた8GBとは別の値なので、ここでは参考にしません。

> 💡 ネットワークありで起動して`curl -s localhost:8080/invocations -H 'content-type: application/json' -d '{}'`を呼ぶと、判定(`"noul": 0.8571`)が返ってきます。Macの2vCPUでは、推論に8.6秒かかりました。

## ステップ5：Amazon ECRにpushする
Amazon ECR(以下、ECR)にリポジトリを作成します。

```bash
aws ecr create-repository --repository-name decider-agentcore --region $AWS_REGION
```

ECRにログインします。

```bash
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com
```

`Login Succeeded`と表示されれば成功です。

> 💡 このコマンドは、何かをダウンロードしているわけではありません。ECRから12時間有効な一時パスワードを受け取り、手元のDockerに渡しているだけです。

タグを付けて、pushします。

```bash
docker tag decider-agentcore:latest ${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/decider-agentcore:latest
```

```bash
docker push ${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/decider-agentcore:latest
```

最後に`latest: digest: sha256:... size: ...`と表示されれば完了です。pushでは、ベースイメージ(Python)の層も含めて、すべての層がECRに送られます。AgentCore RuntimeはECRからイメージを取得するので、起動のたびに外部のレジストリへ取りに行くことはありません。

> 📖 公式ドキュメント：[Pushing a Docker image to an Amazon ECR private repository](https://docs.aws.amazon.com/AmazonECR/latest/userguide/docker-push-ecr-image.html)

## ステップ6：VPCを作る
インターネットへの出口(NATゲートウェイ)を持たないVPCを作り、AWSのサービスにはVPCエンドポイントで接続します。

| 作るもの | 内容 |
| --- | --- |
| VPC | 10.80.0.0/16 |
| プライベートサブネット ×2 | 10.80.1.0/24(AZ ID：apne1-az4)、10.80.2.0/24(AZ ID：apne1-az1) |
| セキュリティグループ ×2 | エージェント用、VPCエンドポイント用 |
| ゲートウェイ型エンドポイント | S3 |
| インターフェース型エンドポイント | `ecr.api`、`ecr.dkr`、`logs` |

> ⚠️ AgentCore RuntimeをVPCで使えるAZは決まっています。東京リージョンでは、AZ IDが`apne1-az1`、`apne1-az2`、`apne1-az4`のサブネットを使います。AZ名(`ap-northeast-1a`など)とAZ IDの対応はアカウントごとに違うので、AZ IDで指定するのが確実です。

VPCを作り、DNSホスト名を有効にします(インターフェース型エンドポイントのプライベートDNSを使うため)。

```bash
export VPC_ID=$(aws ec2 create-vpc --region $AWS_REGION --cidr-block 10.80.0.0/16 --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=decider-agentcore-vpc}]' --query Vpc.VpcId --output text) && echo $VPC_ID
```

```bash
aws ec2 modify-vpc-attribute --region $AWS_REGION --vpc-id $VPC_ID --enable-dns-hostnames '{"Value":true}'
```

プライベートサブネットを2つ作ります。

```bash
export SUBNET_A=$(aws ec2 create-subnet --region $AWS_REGION --vpc-id $VPC_ID --cidr-block 10.80.1.0/24 --availability-zone-id apne1-az4 --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=decider-private-1}]' --query Subnet.SubnetId --output text) && echo $SUBNET_A
```

```bash
export SUBNET_C=$(aws ec2 create-subnet --region $AWS_REGION --vpc-id $VPC_ID --cidr-block 10.80.2.0/24 --availability-zone-id apne1-az1 --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=decider-private-2}]' --query Subnet.SubnetId --output text) && echo $SUBNET_C
```

VPCを作ると自動でできるメインのルートテーブルを、S3のゲートウェイ型エンドポイントで使います。

```bash
export RTB_ID=$(aws ec2 describe-route-tables --region $AWS_REGION --filters Name=vpc-id,Values=$VPC_ID Name=association.main,Values=true --query 'RouteTables[0].RouteTableId' --output text) && echo $RTB_ID
```

セキュリティグループを2つ作ります。

```bash
export SG_AGENT=$(aws ec2 create-security-group --region $AWS_REGION --vpc-id $VPC_ID --group-name decider-agent-sg --description "Decider AgentCore Runtime ENI" --query GroupId --output text) && echo $SG_AGENT
```

```bash
export SG_VPCE=$(aws ec2 create-security-group --region $AWS_REGION --vpc-id $VPC_ID --group-name decider-vpce-sg --description "VPC endpoints for Decider" --query GroupId --output text) && echo $SG_VPCE
```

VPCエンドポイント用は、エージェントからの443だけを受け付けます。

```bash
aws ec2 authorize-security-group-ingress --region $AWS_REGION --group-id $SG_VPCE --protocol tcp --port 443 --source-group $SG_AGENT
```

エージェント用は、既定の「外向きはすべて許可」を消して、VPCエンドポイントとS3への443だけを許可します。S3の宛先には、東京リージョンのS3のプレフィックスリストを使います。

```bash
export S3_PL=$(aws ec2 describe-prefix-lists --region $AWS_REGION --filters Name=prefix-list-name,Values=com.amazonaws.${AWS_REGION}.s3 --query 'PrefixLists[0].PrefixListId' --output text) && echo $S3_PL
```

```bash
aws ec2 revoke-security-group-egress --region $AWS_REGION --group-id $SG_AGENT --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'
```

```bash
aws ec2 authorize-security-group-egress --region $AWS_REGION --group-id $SG_AGENT --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":443,\"ToPort\":443,\"UserIdGroupPairs\":[{\"GroupId\":\"${SG_VPCE}\"}]},{\"IpProtocol\":\"tcp\",\"FromPort\":443,\"ToPort\":443,\"PrefixListIds\":[{\"PrefixListId\":\"${S3_PL}\"}]}]"
```

S3のゲートウェイ型エンドポイントを作ります。エンドポイントポリシーで、通れる先を「モデル用のバケット」と「ECRがイメージの層を置いているバケット」の2つに絞ります。

```bash
cat <<EOF > s3-endpoint-policy.json
{
  "Statement": [
    {"Sid": "AllowECRLayerAccess", "Effect": "Allow", "Principal": "*",
     "Action": ["s3:GetObject"],
     "Resource": ["arn:aws:s3:::prod-${AWS_REGION}-starport-layer-bucket/*"]},
    {"Sid": "AllowModelBucketRead", "Effect": "Allow", "Principal": "*",
     "Action": ["s3:GetObject", "s3:ListBucket"],
     "Resource": ["arn:aws:s3:::${MODEL_BUCKET}", "arn:aws:s3:::${MODEL_BUCKET}/*"]}
  ]
}
EOF
```

```bash
export VPCE_S3=$(aws ec2 create-vpc-endpoint --region $AWS_REGION --vpc-id $VPC_ID --vpc-endpoint-type Gateway --service-name com.amazonaws.${AWS_REGION}.s3 --route-table-ids $RTB_ID --policy-document file://s3-endpoint-policy.json --query VpcEndpoint.VpcEndpointId --output text) && echo $VPCE_S3
```

インターフェース型エンドポイント(`ecr.api`、`ecr.dkr`、`logs`)を作ります。

```bash
for svc in ecr.api ecr.dkr logs; do aws ec2 create-vpc-endpoint --region $AWS_REGION --vpc-id $VPC_ID --vpc-endpoint-type Interface --service-name com.amazonaws.${AWS_REGION}.${svc} --subnet-ids $SUBNET_A $SUBNET_C --security-group-ids $SG_VPCE --private-dns-enabled --query 'VpcEndpoint.[ServiceName,VpcEndpointId,State]' --output text; done
```

1〜2分で、すべて`available`になります。

```bash
aws ec2 describe-vpc-endpoints --region $AWS_REGION --filters Name=vpc-id,Values=$VPC_ID --query 'VpcEndpoints[].[ServiceName,VpcEndpointType,State]' --output text
```

以下のような出力結果が表示されます。

```
com.amazonaws.ap-northeast-1.s3	Gateway	available
com.amazonaws.ap-northeast-1.ecr.api	Interface	available
com.amazonaws.ap-northeast-1.ecr.dkr	Interface	available
com.amazonaws.ap-northeast-1.logs	Interface	available
```

> ⚠️ インターフェース型エンドポイントは、置いておくだけで「エンドポイントの数 × AZの数 × 時間」で課金されます。検証が終わったら、クリーンアップで必ず消してください。

> 📖 公式ドキュメント：[Configure Amazon Bedrock AgentCore Runtime and tools for VPC](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html)

## ステップ7：実行ロールとバケットポリシーを作成する
AgentCore Runtimeがエージェントを動かすときに引き受けるIAMロール(実行ロール)を作ります。このロールを作らずにランタイムを作成すると、「role cannot be found」や「failed to assume role」といったエラーで失敗します。

信頼ポリシーを作成します。AgentCore Runtimeだけが、このロールを引き受けられるようにします。

```bash
cat <<EOF > trust-policy.json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AssumeRolePolicy",
    "Effect": "Allow",
    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": {"aws:SourceAccount": "${ACCOUNT_ID}"},
      "ArnLike": {"aws:SourceArn": "arn:aws:bedrock-agentcore:${AWS_REGION}:${ACCOUNT_ID}:*"}
    }
  }]
}
EOF
```

権限ポリシーを作成します。ECRからのイメージの取得、ログの書き込み、トレースとメトリクスの送信を許可します。今回のエージェントはAmazon Bedrockのモデルを呼び出さないので、公式ドキュメントの例からBedrockとワークロードIDの権限を外しています。

```bash
cat <<EOF > permissions-policy.json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "ECRImageAccess", "Effect": "Allow",
     "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
     "Resource": ["arn:aws:ecr:${AWS_REGION}:${ACCOUNT_ID}:repository/decider-agentcore"]},
    {"Sid": "ECRTokenAccess", "Effect": "Allow",
     "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
    {"Effect": "Allow",
     "Action": ["logs:DescribeLogStreams", "logs:CreateLogGroup"],
     "Resource": ["arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/runtimes/*"]},
    {"Effect": "Allow",
     "Action": ["logs:DescribeLogGroups"],
     "Resource": ["arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:*"]},
    {"Effect": "Allow",
     "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
     "Resource": ["arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"]},
    {"Effect": "Allow",
     "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"],
     "Resource": ["*"]},
    {"Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
     "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}}
  ]
}
EOF
```

モデル用のバケットの`hf-cache/`配下の読み取りだけを許可するポリシーを作成します。

```bash
cat <<EOF > s3-read-policy.json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": "s3:GetObject",
     "Resource": "arn:aws:s3:::${MODEL_BUCKET}/hf-cache/*"},
    {"Effect": "Allow", "Action": "s3:ListBucket",
     "Resource": "arn:aws:s3:::${MODEL_BUCKET}",
     "Condition": {"StringLike": {"s3:prefix": "hf-cache/*"}}}
  ]
}
EOF
```

ロールを作成して、2つのポリシーを付けます。

```bash
aws iam create-role --role-name DeciderAgentCoreRuntimeRole --assume-role-policy-document file://trust-policy.json
```

```bash
aws iam put-role-policy --role-name DeciderAgentCoreRuntimeRole --policy-name DeciderAgentCoreRuntimePolicy --policy-document file://permissions-policy.json
```

```bash
aws iam put-role-policy --role-name DeciderAgentCoreRuntimeRole --policy-name DeciderModelBucketRead --policy-document file://s3-read-policy.json
```

最後に、バケットポリシーで「実行ロールは、S3のVPCエンドポイント経由のときだけ読める」ようにします。実行ロールの認証情報が万一外に漏れても、VPCの外からはモデルのファイルを読めません。自分(管理者)やCloudShellからの操作は制限しません。

```bash
cat <<EOF > bucket-policy.json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "RuntimeRoleOnlyViaVpce",
    "Effect": "Deny",
    "Principal": "*",
    "Action": ["s3:GetObject", "s3:ListBucket"],
    "Resource": ["arn:aws:s3:::${MODEL_BUCKET}", "arn:aws:s3:::${MODEL_BUCKET}/*"],
    "Condition": {
      "ArnEquals": {"aws:PrincipalArn": "arn:aws:iam::${ACCOUNT_ID}:role/DeciderAgentCoreRuntimeRole"},
      "StringNotEquals": {"aws:SourceVpce": "${VPCE_S3}"}
    }
  }]
}
EOF
```

```bash
aws s3api put-bucket-policy --bucket $MODEL_BUCKET --policy file://bucket-policy.json
```

> 📖 公式ドキュメント：[IAM Permissions for AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)

## ステップ8：V1とV2のランタイムを作成する
同じイメージ・同じネットワーク設定で、`--platform-version`だけを変えた2つのランタイムを作ります。環境変数`MODEL_S3_URI`で、モデルのファイルの置き場所を渡します。

```bash
export ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/DeciderAgentCoreRuntimeRole
```

```bash
export IMAGE_URI=${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/decider-agentcore:latest
```

```bash
export NETCONF="{\"networkMode\": \"VPC\", \"networkModeConfig\": {\"subnets\": [\"${SUBNET_A}\", \"${SUBNET_C}\"], \"securityGroups\": [\"${SG_AGENT}\"]}}" && echo $NETCONF
```

V1のランタイムを作成します。

```bash
aws bedrock-agentcore-control create-agent-runtime --region $AWS_REGION --agent-runtime-name decider_vpc_v1 --role-arn $ROLE_ARN --agent-runtime-artifact "{\"containerConfiguration\": {\"containerUri\": \"${IMAGE_URI}\"}}" --network-configuration "$NETCONF" --environment-variables MODEL_S3_URI=s3://${MODEL_BUCKET}/hf-cache/hub/ --platform-version V1
```

V2のランタイムを作成します。違うのは、名前と`--platform-version`だけです。

```bash
aws bedrock-agentcore-control create-agent-runtime --region $AWS_REGION --agent-runtime-name decider_vpc_v2 --role-arn $ROLE_ARN --agent-runtime-artifact "{\"containerConfiguration\": {\"containerUri\": \"${IMAGE_URI}\"}}" --network-configuration "$NETCONF" --environment-variables MODEL_S3_URI=s3://${MODEL_BUCKET}/hf-cache/hub/ --platform-version V2
```

どちらも、以下のような出力結果が表示されます。`agentRuntimeId`と`agentRuntimeArn`を控えておきます。

```json
{
    "agentRuntimeArn": "arn:aws:bedrock-agentcore:ap-northeast-1:[アカウントID]:runtime/decider_vpc_v2-[ランダムな文字列]",
    "workloadIdentityDetails": {
        "workloadIdentityArn": "arn:aws:bedrock-agentcore:ap-northeast-1:[アカウントID]:workload-identity-directory/default/workload-identity/decider_vpc_v2-[ランダムな文字列]"
    },
    "agentRuntimeId": "decider_vpc_v2-[ランダムな文字列]",
    "agentRuntimeVersion": "1",
    "createdAt": "2026-10-24T15:42:05.951546+00:00",
    "status": "CREATING"
}
```

状態を確認します。VPCモードのV1は、ネットワークインターフェースの作成のため、`READY`になるまで約4〜5分かかりました。V2は、スナップショットを準備するため、さらに時間がかかります(今回は毎回7〜9分ほどで結果が出ました)。

```bash
aws bedrock-agentcore-control get-agent-runtime --region $AWS_REGION --agent-runtime-id [agentRuntimeId] --query '[status,platformVersion,failureReason]'
```

V1は、以下のような出力結果が表示されます。

```json
[
    "READY",
    "V1",
    null
]
```

一方、**V2は今回、何度試しても次の結果になりました。**

```json
[
    "CREATE_FAILED",
    "V2",
    "The runtime failed its health check. Ensure your container serves the configured health endpoint."
]
```

この原因は、記事で詳しく紹介しています。

> 💡 VPCモードのランタイムを作ると、AgentCoreがサブネットにネットワークインターフェース(ENI)を作ります。これは「AWSのサービスが管理するリソース」なので、EC2コンソールでは既定で非表示になっています。`aws ec2 modify-managed-resource-visibility --region ap-northeast-1 --default-visibility visible`で表示されるようになり、Interface Typeが`agentic_ai`のENIとして確認できます(アカウント全体の、リージョンごとの設定です)。

> 📖 公式ドキュメント：[Platform versions(Enable V2)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html#runtime-platform-versions) / [create-agent-runtime(AWS CLI Command Reference)](https://docs.aws.amazon.com/cli/latest/reference/bedrock-agentcore-control/create-agent-runtime.html) / [Managed resource visibility settings](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/amazon-ec2-managed-instances.html#managed-resource-visibility-settings)

## ステップ9：新しいセッションを同時に呼び出して計測する
計測スクリプトは`scripts/measure.py`です。新しいセッションIDで、指定した数のセッションを同時に呼び出し、結果をCSVに保存します。

> 💡 V1で新しく起動するセッションは数十秒かかるため、boto3の読み取りのタイムアウトを900秒に延ばしています。既定の60秒のままだと、応答が返る前にタイムアウトしてしまいます。また、測り終えたセッションは`stop_runtime_session`で止めて、アイドル中の課金を避けています。

V1のランタイムを、新しいセッションで1回呼び出します。

```bash
python3 scripts/measure.py --arn [V1のagentRuntimeArn] --label vpc-v1-single --n 1
```

続けて、新しいセッションを15個、同時に呼び出します。

```bash
python3 scripts/measure.py --arn [V1のagentRuntimeArn] --label vpc-v1-burst --n 15
```

セッションごとに、かかった時間・起動処理の時間・CPUの種類が表示され、結果は`results/`にCSVで保存されます(出力の例と読み方は記事を参照)。

起動時のログは、Amazon CloudWatch Logs Insightsで確認できます。ロググループに`/aws/bedrock-agentcore/runtimes/[agentRuntimeId]-DEFAULT`を選んで、次のクエリを実行します。

```
fields @timestamp, @logStream, @message
| filter @message like /\[init\] cpu|app.run at|mkldnn_matmul failed/
| sort @timestamp asc
```

> 📖 公式ドキュメント：[InvokeAgentRuntime(API Reference)](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeAgentRuntime.html) / [Analyzing log data with CloudWatch Logs Insights](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/AnalyzingLogData.html)

## クリーンアップ
検証が終わったら、利用する側から順に削除します。**時間課金のあるインターフェース型エンドポイントは、最初に消しておくのがおすすめです。**

ランタイムを削除します。

```bash
aws bedrock-agentcore-control delete-agent-runtime --region $AWS_REGION --agent-runtime-id [V1のagentRuntimeId]
```

```bash
aws bedrock-agentcore-control delete-agent-runtime --region $AWS_REGION --agent-runtime-id [V2のagentRuntimeId]
```

VPCエンドポイントを削除します。

```bash
aws ec2 delete-vpc-endpoints --region $AWS_REGION --vpc-endpoint-ids $(aws ec2 describe-vpc-endpoints --region $AWS_REGION --filters Name=vpc-id,Values=$VPC_ID --query 'VpcEndpoints[].VpcEndpointId' --output text)
```

AgentCoreが作ったENIは、ランタイムを削除してから**最大8時間**残ります。ENIが消えたことを確認してから、セキュリティグループ、サブネット、VPCの順に削除します。

```bash
aws ec2 describe-network-interfaces --region $AWS_REGION --filters Name=vpc-id,Values=$VPC_ID --query 'NetworkInterfaces[].[NetworkInterfaceId,InterfaceType]' --output text
```

何も表示されなくなったら、削除します。

```bash
aws ec2 revoke-security-group-ingress --region $AWS_REGION --group-id $SG_VPCE --protocol tcp --port 443 --source-group $SG_AGENT && aws ec2 delete-security-group --region $AWS_REGION --group-id $SG_AGENT && aws ec2 delete-security-group --region $AWS_REGION --group-id $SG_VPCE
```

```bash
aws ec2 delete-subnet --region $AWS_REGION --subnet-id $SUBNET_A && aws ec2 delete-subnet --region $AWS_REGION --subnet-id $SUBNET_C && aws ec2 delete-vpc --region $AWS_REGION --vpc-id $VPC_ID
```

ECRリポジトリを削除します。

```bash
aws ecr delete-repository --repository-name decider-agentcore --region $AWS_REGION --force
```

S3バケットは、バージョニングを有効にしているため、すべての版と削除マーカーを消してからバケットを削除します。

```bash
aws s3api delete-objects --bucket $MODEL_BUCKET --delete "$(aws s3api list-object-versions --bucket $MODEL_BUCKET --output json --query '{Objects: [Versions[].{Key:Key,VersionId:VersionId}, DeleteMarkers[].{Key:Key,VersionId:VersionId}][] | [?Key]}')"
```

```bash
aws s3api delete-bucket --bucket $MODEL_BUCKET --region $AWS_REGION
```

IAMロールを削除します(ポリシーを消してから、ロールを消します)。

```bash
aws iam delete-role-policy --role-name DeciderAgentCoreRuntimeRole --policy-name DeciderAgentCoreRuntimePolicy && aws iam delete-role-policy --role-name DeciderAgentCoreRuntimeRole --policy-name DeciderModelBucketRead && aws iam delete-role --role-name DeciderAgentCoreRuntimeRole
```

最後に、ロググループを削除します。

```bash
aws logs delete-log-group --region $AWS_REGION --log-group-name /aws/bedrock-agentcore/runtimes/[V1のagentRuntimeId]-DEFAULT
```

```bash
aws logs delete-log-group --region $AWS_REGION --log-group-name /aws/bedrock-agentcore/runtimes/[V2のagentRuntimeId]-DEFAULT
```

> 💡 V2のスナップショットは、ランタイムを削除すると自動で削除されます(最大8時間かかることがあります)。ユーザーが直接削除する操作はありません。

> 📖 公式ドキュメント：[delete-agent-runtime(AWS CLI Command Reference)](https://docs.aws.amazon.com/cli/latest/reference/bedrock-agentcore-control/delete-agent-runtime.html) / [Deleting a bucket](https://docs.aws.amazon.com/AmazonS3/latest/userguide/delete-bucket.html)

## ライセンス

[MIT-0](LICENSE)
