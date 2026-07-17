# agentcore-websearch-mcp

*English version: [README.en.md](README.en.md)*

**Amazon Bedrock AgentCore Gateway**의 내장 Web Search Tool을 이용해, 모든 MCP 클라이언트(Claude Code 등)에 **웹 검색 기능을 붙여 주는** 가볍고 의존성 적은 **MCP 서버**입니다.

두 가지 구성 요소로 이루어져 있습니다.

1. **`server.py`** — 로컬 stdio↔HTTPS MCP 프록시. 클라이언트가 기대하는 MCP stdio 전송을 그대로 받아, 각 요청을 여러분의 AgentCore Gateway로 HTTPS를 통해 전달하며, 일반 AWS 자격증명을 이용해 매 호출을 SigV4로 서명합니다.
2. **`cloudformation/agentcore-websearch-gateway.yaml`** — 게이트웨이, IAM 서비스 역할, web-search 커넥터 타깃을 한 번에 생성하는 인프라 템플릿. 배포하고, 출력값 하나만 복사하면 끝입니다.

```
MCP 클라이언트  ──stdio JSON-RPC──▶  server.py  ──SigV4 HTTPS──▶  AgentCore Gateway (Web Search Tool)
```

## 왜 필요한가

AgentCore Gateway는 웹 검색을 표준 MCP 도구로 노출하지만, **IAM으로 인증되는 Streamable HTTP** 엔드포인트로만 제공합니다. 반면 MCP 클라이언트는 **stdio**로 통신하며 SigV4 서명을 직접 할 수 없습니다. 이 프록시가 둘 사이를 이어 줍니다 — stdio로 받아 SigV4 서명된 HTTPS로 내보냅니다. 또한 게이트웨이가 비어 있는 설명(description)으로 반환하는 web-search 도구에 유용한 설명을 주입하여, 모델이 *언제* 이 도구를 써야 할지 알 수 있게 합니다.

- **하드코딩된 자격증명이나 프로파일이 없습니다.** 표준 AWS 자격증명 체인을 사용합니다 — 환경변수, `AWS_PROFILE`, 공유 config/credentials 파일, SSO, 인스턴스/컨테이너 역할이 모두 그대로 동작합니다.
- **서드파티 HTTP 라이브러리가 필요 없습니다.** `botocore`(자격증명 + SigV4)와 파이썬 표준 라이브러리만 사용합니다.
- **검색은 AWS 내부에서 처리됩니다.** 게이트웨이가 전용 웹 인덱스를 이용해 쿼리를 전부 AWS 안에서 처리합니다.

---

## 빠른 시작 (Quick Start)

> **MCP 클라이언트:** 이 문서의 모든 예제는 **Claude Code**를 기준으로 작성했지만, `server.py`는 표준 MCP stdio 서버이므로 **MCP를 지원하는 다른 앱(OpenAI Codex, Cursor, Cline 등)에서도 그대로 사용할 수 있습니다.** 각 앱의 MCP 설정에 `command`를 `python3`, `args`를 `server.py` 절대경로, `env`에 `AGENTCORE_GATEWAY_URL`(필요 시 `AWS_PROFILE`)을 지정하면 됩니다. `claude mcp add`처럼 Claude Code 전용 명령만 해당 앱의 등록 방식으로 바꿔 주세요.

### 사전 준비

- `botocore`가 설치된 Python 3.9+ (`pip install botocore`).
- 스택 배포 권한이 있는 AWS 자격증명 설정 (`aws configure`, `AWS_PROFILE`, SSO, 또는 역할).
- 아래 배포 단계를 위한 AWS CLI.

> **리전:** **2026년 7월 기준**, Web Search Tool 커넥터는 **`us-east-1`** (버지니아 북부)에서만 사용할 수 있습니다. 스택도 그 리전에 배포하세요.

### 0. 저장소 복제

먼저 저장소를 복제하고 이 프로젝트 디렉터리로 이동합니다. **`server.py`는 여기 계속 남아 있어야 하므로**(아래 참고), 지워지거나 옮겨지지 않을 위치에 복제하세요.

```bash
git clone https://github.com/aws-samples/sample-aws-kr-enterprise.git
cd sample-aws-kr-enterprise/developer-tools/agentcore-websearch-mcp
```

> **`server.py`는 일회성이 아니라 계속 사용됩니다.** 아래 등록 과정은 Claude Code가 **웹 검색을 할 때마다 실행할 로컬 명령**으로 `python3 /절대경로/server.py`를 저장합니다. 즉 `server.py`는 프록시로서 상주하며 매 검색마다 다시 실행됩니다. 배포가 끝난 뒤에도 **복제한 저장소(특히 `server.py`)를 삭제하거나 옮기지 마세요.** 옮겨야 한다면 MCP 등록의 경로도 함께 갱신해야 합니다(스크립트를 다시 실행하는 것이 가장 간단합니다).

### 자동 설치 (권장)

`agentcore-websearch.sh` 스크립트가 CloudFormation 배포와 Claude Code MCP 등록을 한 번에 처리합니다.

```bash
./agentcore-websearch.sh deploy     # 스택 배포 + MCP 서버 등록
./agentcore-websearch.sh destroy    # MCP 서버 등록 해제 + 스택 삭제
./agentcore-websearch.sh url        # 배포된 게이트웨이 MCP URL 출력
```

**`--profile NAME`** (또는 `-p NAME`) 로 AWS 프로파일을 지정할 수 있습니다. `deploy`에서 지정하면 AWS CLI에 쓰이는 것은 물론, **MCP 서버 env에도 자동으로 기록**되어 프록시가 같은 프로파일로 게이트웨이를 호출합니다.

```bash
# 게이트웨이 URL과 AWS_PROFILE이 모두 자동으로 MCP env에 기록됩니다
./agentcore-websearch.sh deploy --profile my-profile
```

`--profile`을 생략하면 `AWS_PROFILE` 환경변수를 사용합니다. 그것도 없으면 표준 AWS 자격증명 체인을 따르며, 이 경우 MCP env에는 `AWS_PROFILE`이 기록되지 않습니다.

> **참고:** 배포에 쓰는 자격증명과 게이트웨이 호출(실행)에 쓰는 자격증명이 다르다면(예: 배포는 관리자 역할, 실행은 `InvokeGateway` 권한만 가진 IAM 사용자), 실행용 프로파일을 `--profile`로 지정하세요. 인바운드 인증은 `AWS_IAM`이라 실제 IAM 자격증명이 필요합니다.

그 밖의 동작은 환경변수로 조정합니다(모두 선택): `REGION`(기본 `us-east-1`), `STACK_NAME`, `MCP_NAME`, `MCP_SCOPE`(기본 `user`), `PYTHON`.

`claude` CLI가 없으면 배포는 그대로 진행되고, 수동 등록용 설정을 출력합니다.

수동으로 각 단계를 직접 실행하려면 아래를 참고하세요.

### 1. 게이트웨이 배포

```bash
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name agentcore-websearch \
  --template-file cloudformation/agentcore-websearch-gateway.yaml \
  --capabilities CAPABILITY_IAM
```

스택 출력에서 MCP 엔드포인트 URL을 가져옵니다.

```bash
aws cloudformation describe-stacks \
  --region us-east-1 \
  --stack-name agentcore-websearch \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayMcpUrl'].OutputValue" \
  --output text
```

### 2. MCP 클라이언트에 서버 등록

1단계에서 얻은 URL을 `AGENTCORE_GATEWAY_URL`로 전달하면서, 클라이언트가 `server.py`를 가리키도록 등록합니다.

**Claude Code** (`claude mcp add`):

```bash
claude mcp add agentcore-websearch \
  --env AGENTCORE_GATEWAY_URL="https://<your-gateway>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp" \
  -- python3 /absolute/path/to/server.py
```

또는 MCP 클라이언트 설정에 직접 추가:

```json
{
  "mcpServers": {
    "agentcore-websearch": {
      "command": "python3",
      "args": ["/absolute/path/to/server.py"],
      "env": {
        "AGENTCORE_GATEWAY_URL": "https://<your-gateway>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
      }
    }
  }
}
```

특정 프로파일의 자격증명을 사용한다면 `env`에 `"AWS_PROFILE": "your-profile"`을 추가하세요(또는 셸에서 export). AWS SDK가 이해하는 방식이면 무엇이든 동작합니다.

### 3. 사용하기

최신 정보가 필요한 질문을 던져 보세요("X의 최신 릴리스가 뭐야?"). 클라이언트가 프록시를 통해 `WebSearch` 도구를 호출합니다.

### 클라이언트 없이 테스트 (선택)

프록시는 stdin에서 한 줄에 하나의 JSON-RPC 메시지를 읽고, 응답을 stdout으로 씁니다. 로그는 stderr로 나갑니다.

```bash
export AGENTCORE_GATEWAY_URL="https://<your-gateway>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3 server.py
```

`initialize` 결과에 이어, web-search 도구가 포함된 `tools/list` 결과가 나오면 정상입니다.

---

## 설정

모든 설정은 환경변수로 합니다.

| 변수 | 필수 | 설명 |
| --- | --- | --- |
| `AGENTCORE_GATEWAY_URL` | **예** | 게이트웨이 MCP URL (`GatewayMcpUrl` 스택 출력값). 설정되지 않으면 프록시가 시작 시 종료됩니다. |
| `AGENTCORE_SIGNING_REGION` | 아니오 | SigV4 서명 리전 재정의. 기본적으로 게이트웨이 URL에서 리전을 파싱하므로, 잘못된 `AWS_REGION`이 있어도 서명이 깨지지 않습니다. |
| AWS 자격증명 변수 | — | 표준 AWS 해석: `AWS_PROFILE`, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, SSO, 인스턴스/컨테이너 역할 등. |

호출자의 IAM 자격증명에는 게이트웨이 ARN에 대한 `bedrock-agentcore:InvokeGateway` 권한이 필요합니다(인바운드 인증은 `AWS_IAM`). 게이트웨이가 web-search 백엔드로 나가는 아웃바운드 호출은, CloudFormation 스택에서 생성한 서비스 역할이 인가합니다.

## 필요한 IAM 권한 (배포 vs 사용)

**배포 자격증명과 사용 자격증명은 서로 달라도 됩니다.** 배포는 관리자 역할로 한 번 하고, 실행에는 게이트웨이 호출 권한만 가진 최소 권한 사용자를 두는 구성을 권장합니다. 그럴 경우 `agentcore-websearch.sh deploy` 뒤에, MCP 서버 env의 `AWS_PROFILE`은 **사용용 프로파일**로 지정하세요.

### 배포할 때 (스택을 만들고 지우는 주체)

CloudFormation 스택과 그 리소스(게이트웨이, IAM 역할, 타깃)를 생성·삭제할 수 있어야 합니다. 최소한 다음 액션이 필요합니다.

- `cloudformation:CreateStack` / `DeleteStack` / `DescribeStacks` / `DescribeStackEvents` / `DescribeStackResources` / `GetTemplateSummary` (및 `deploy`가 쓰는 체인지셋: `CreateChangeSet` / `DescribeChangeSet` / `ExecuteChangeSet` / `DeleteChangeSet`)
- `bedrock-agentcore:CreateGateway` / `DeleteGateway` / `GetGateway` / `UpdateGateway`, `bedrock-agentcore:CreateGatewayTarget` / `DeleteGatewayTarget` / `GetGatewayTarget` / `ListGatewayTargets`
- `iam:CreateRole` / `DeleteRole` / `GetRole` / `PutRolePolicy` / `DeleteRolePolicy` / `PassRole` — 스택이 게이트웨이 서비스 역할을 만들기 때문 (`aws cloudformation deploy` 시 `--capabilities CAPABILITY_IAM` 필요)

> 실무에서는 배포자에게 이 조합을 개별 부여하기보다, 관리자 또는 위 리소스를 다룰 수 있는 배포용 역할을 쓰는 경우가 많습니다.

### 사용할 때 (MCP 프록시가 게이트웨이를 호출하는 주체)

프록시가 SigV4로 서명하는 IAM 자격증명(= MCP env의 `AWS_PROFILE`)에는 **딱 하나의 권한**만 있으면 됩니다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "bedrock-agentcore:InvokeGateway",
      "Resource": "arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:gateway/agentcore-websearch-*"
    }
  ]
}
```

- `Resource`는 특정 게이트웨이 ARN을 콕 집지 않고 `agentcore-websearch-*` 와일드카드를 쓰면, 재배포로 게이트웨이 ID가 바뀌어도 정책을 다시 고칠 필요가 없습니다.
- 이 사용자는 배포·조회 권한이 전혀 없어도 됩니다 — 오직 게이트웨이 호출(웹 검색 실행)만 하면 되기 때문입니다.
- 게이트웨이가 web-search 백엔드로 나가는 `bedrock-agentcore:InvokeWebSearch` 권한은 이 사용자가 아니라, CloudFormation이 만든 **게이트웨이 서비스 역할**이 갖습니다.

## CloudFormation 스택이 생성하는 것

- **`AWS::BedrockAgentCore::Gateway`** — MCP 프로토콜, `AWS_IAM` 인바운드 인증.
- **`AWS::IAM::Role`** — 게이트웨이의 아웃바운드 서비스 역할. `bedrock-agentcore.amazonaws.com`을 신뢰하며, `bedrock-agentcore:InvokeGateway`와, 서비스 소유 도구 ARN `arn:aws:bedrock-agentcore:us-east-1:aws:tool/web-search.v1`에 대한 `bedrock-agentcore:InvokeWebSearch` 권한을 부여합니다.
- **`AWS::BedrockAgentCore::GatewayTarget`** — 내장 `web-search` 커넥터 타깃. `GATEWAY_IAM_ROLE`로 인증합니다.

출력값: `GatewayMcpUrl`, `GatewayId`, `GatewayArn`, `GatewayServiceRoleArn`.

## 삭제 (Teardown)

```bash
aws cloudformation delete-stack --region us-east-1 --stack-name agentcore-websearch
```

## 비용

Web Search Tool과 AgentCore Gateway는 사용량 기반으로 과금됩니다(검색 요청 / 게이트웨이 호출). 현재 요율은 [Amazon Bedrock AgentCore 요금](https://aws.amazon.com/bedrock/agentcore/) 페이지를 참고하세요.
