# AWS DevOps Agent Multi-Account Setup Guide

## Overview

DevOps Agent Space를 활용하여 Multi-Account 환경에서 리소스 격리를 구현하는 가이드입니다.

**목표 구조:**
```
Ops-Central Space (전체 계정 연결 → 운영팀용)
├── Management Account (monitor)
├── Team A Account (source)
└── Team B Account (source)

TeamA-Only Space (Team A 계정만 연결 → Team A 전용)
├── Management Account (monitor)
└── Team A Account (source)

TeamB-Only Space
├── Management Account (monitor)
└── Team B Account (source)
```

---

## Prerequisites

- AWS CLI 2.35.12 이상
- AWS Organizations 활성화
- 관리 계정에서 작업 (IAM 권한 필요: `aidevops:*`, `iam:*`, `organizations:*`)

---

## Step 1: Organizations & 계정 생성

```bash
# Organizations 생성 (없는 경우)
aws organizations create-organization --feature-set ALL

# 링크드 계정 생성
aws organizations create-account \
  --email "<EMAIL_A>" \
  --account-name "vd-team-a-test"

aws organizations create-account \
  --email "<EMAIL_B>" \
  --account-name "vd-team-b-test"

# 생성 확인
aws organizations list-accounts
```

> 계정 생성에 1-2분 소요. Status가 ACTIVE로 변경되면 완료.

---

## Step 2: DevOps Agent Space 생성

```bash
# Ops Space (전체 모니터링용)
aws devops-agent create-agent-space \
  --name "VD-Ops-Central" \
  --description "운영팀 전체 계정 모니터링" \
  --region us-east-1

# Team A 전용 Space
aws devops-agent create-agent-space \
  --name "VD-TeamA-Only" \
  --description "Team A 계정만 접근" \
  --region us-east-1

# Space ID 확인
aws devops-agent list-agent-spaces --region us-east-1
```

> 결과에서 `agentSpaceId` 메모해 둘 것.

---

## Step 3: 관리 계정에 DevOps Agent Role 생성

```bash
# Role 생성
aws iam create-role \
  --role-name DevOpsAgentRole \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Principal": {
          "Service": "aidevops.amazonaws.com"
        },
        "Action": "sts:AssumeRole",
        "Condition": {
          "StringEquals": {
            "aws:SourceAccount": "<MANAGEMENT_ACCOUNT_ID>"
          }
        }
      }
    ]
  }'

# ReadOnly 권한 부여
aws iam attach-role-policy \
  --role-name DevOpsAgentRole \
  --policy-arn "arn:aws:iam::aws:policy/ReadOnlyAccess"
```

---

## Step 4: 링크드 계정에 DevOps Agent Role 생성

각 링크드 계정에 관리 계정에서 assume하여 Role을 생성합니다.

### Team A 계정

```bash
# Team A 계정으로 전환
CREDS=$(aws sts assume-role \
  --role-arn "arn:aws:iam::<TEAM_A_ACCOUNT_ID>:role/OrganizationAccountAccessRole" \
  --role-session-name "setup-team-a" \
  --query 'Credentials' --output json)

export AWS_ACCESS_KEY_ID=$(echo $CREDS | python3 -c "import sys,json;print(json.load(sys.stdin)['AccessKeyId'])")
export AWS_SECRET_ACCESS_KEY=$(echo $CREDS | python3 -c "import sys,json;print(json.load(sys.stdin)['SecretAccessKey'])")
export AWS_SESSION_TOKEN=$(echo $CREDS | python3 -c "import sys,json;print(json.load(sys.stdin)['SessionToken'])")

# Role 생성
aws iam create-role \
  --role-name DevOpsAgentRole \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Principal": {
          "Service": "aidevops.amazonaws.com"
        },
        "Action": "sts:AssumeRole",
        "Condition": {
          "StringEquals": {
            "aws:SourceAccount": "<MANAGEMENT_ACCOUNT_ID>"
          }
        }
      }
    ]
  }'

aws iam attach-role-policy \
  --role-name DevOpsAgentRole \
  --policy-arn "arn:aws:iam::aws:policy/ReadOnlyAccess"

# 관리 계정으로 복귀
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
```

### Team B 계정

```bash
# Team B 계정으로 전환
CREDS=$(aws sts assume-role \
  --role-arn "arn:aws:iam::<TEAM_B_ACCOUNT_ID>:role/OrganizationAccountAccessRole" \
  --role-session-name "setup-team-b" \
  --query 'Credentials' --output json)

export AWS_ACCESS_KEY_ID=$(echo $CREDS | python3 -c "import sys,json;print(json.load(sys.stdin)['AccessKeyId'])")
export AWS_SECRET_ACCESS_KEY=$(echo $CREDS | python3 -c "import sys,json;print(json.load(sys.stdin)['SecretAccessKey'])")
export AWS_SESSION_TOKEN=$(echo $CREDS | python3 -c "import sys,json;print(json.load(sys.stdin)['SessionToken'])")

# Role 생성
aws iam create-role \
  --role-name DevOpsAgentRole \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Principal": {
          "Service": "aidevops.amazonaws.com"
        },
        "Action": "sts:AssumeRole",
        "Condition": {
          "StringEquals": {
            "aws:SourceAccount": "<MANAGEMENT_ACCOUNT_ID>"
          }
        }
      }
    ]
  }'

aws iam attach-role-policy \
  --role-name DevOpsAgentRole \
  --policy-arn "arn:aws:iam::aws:policy/ReadOnlyAccess"

# 관리 계정으로 복귀
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
```

---

## Step 5: Ops Space에 계정 연결

```bash
# 관리 계정 연결 (monitor 타입)
aws devops-agent associate-service --agent-space-id "<OPS_SPACE_ID>" --service-id "aws" --configuration '{"aws":{"assumableRoleArn":"arn:aws:iam::<MANAGEMENT_ACCOUNT_ID>:role/DevOpsAgentRole","accountId":"<MANAGEMENT_ACCOUNT_ID>","accountType":"monitor"}}' --region us-east-1

# Team A 연결 (source 타입)
aws devops-agent associate-service --agent-space-id "<OPS_SPACE_ID>" --service-id "aws" --configuration '{"sourceAws":{"assumableRoleArn":"arn:aws:iam::<TEAM_A_ACCOUNT_ID>:role/DevOpsAgentRole","accountId":"<TEAM_A_ACCOUNT_ID>","accountType":"source"}}' --region us-east-1

# Team B 연결 (source 타입)
aws devops-agent associate-service --agent-space-id "<OPS_SPACE_ID>" --service-id "aws" --configuration '{"sourceAws":{"assumableRoleArn":"arn:aws:iam::<TEAM_B_ACCOUNT_ID>:role/DevOpsAgentRole","accountId":"<TEAM_B_ACCOUNT_ID>","accountType":"source"}}' --region us-east-1
```

---

## Step 6: TeamA-Only Space에 계정 연결

```bash
# 관리 계정 연결 (monitor 타입 - 필수. monitor가 없으면 source 연결 불가)
aws devops-agent associate-service --agent-space-id "<TEAM_A_SPACE_ID>" --service-id "aws" --configuration '{"aws":{"assumableRoleArn":"arn:aws:iam::<MANAGEMENT_ACCOUNT_ID>:role/DevOpsAgentRole","accountId":"<MANAGEMENT_ACCOUNT_ID>","accountType":"monitor"}}' --region us-east-1

# Team A만 연결 (source 타입)
aws devops-agent associate-service --agent-space-id "<TEAM_A_SPACE_ID>" --service-id "aws" --configuration '{"sourceAws":{"assumableRoleArn":"arn:aws:iam::<TEAM_A_ACCOUNT_ID>:role/DevOpsAgentRole","accountId":"<TEAM_A_ACCOUNT_ID>","accountType":"source"}}' --region us-east-1
```

> **주의**: 각 Space에 monitor 계정이 먼저 연결되어 있어야 source 계정 연결이 가능합니다.

---

## Step 7: 검증

```bash
# 연결 상태 확인
aws devops-agent list-associations --agent-space-id "<OPS_SPACE_ID>" --region us-east-1
aws devops-agent list-associations --agent-space-id "<TEAM_A_SPACE_ID>" --region us-east-1
```

**예상 결과:**

| Space | 연결된 계정 |
|---|---|
| Ops-Central | Management(monitor) + Team A(source) + Team B(source) |
| TeamA-Only | Management(monitor) + Team A(source) |

---

## 검증 테스트 시나리오

| Space | 질문 | 기대 결과 |
|---|---|---|
| Ops-Central | "Team A 계정의 리소스 보여줘" | ✅ 응답 |
| Ops-Central | "Team B 계정의 리소스 보여줘" | ✅ 응답 |
| TeamA-Only | "Team A 계정의 리소스 보여줘" | ✅ 응답 |
| TeamA-Only | "Team B 계정의 리소스 보여줘" | ❌ 접근 불가 |


---

## 참고

- **CLI 최소 버전**: AWS CLI 2.35.12 이상
- **리전**: DevOps Agent는 us-east-1에서 운영
- **Role 구조**: monitor(관리계정) + source(링크드계정)가 1세트
- **다음 단계**: IAM Identity Center로 사용자별 Space 접근 제어 추가

---

## 대규모(320개 계정) 적용 시

```bash
# 1. StackSets로 전 계정에 DevOpsAgentRole 일괄 생성
# 2. for 루프로 associate-service 일괄 실행

for ACCOUNT_ID in $(aws organizations list-accounts --query 'Accounts[?Status==`ACTIVE`].Id' --output text); do
  # 관리계정은 스킵 (이미 monitor로 등록)
  if [ "$ACCOUNT_ID" = "<MANAGEMENT_ACCOUNT_ID>" ]; then continue; fi
  
  aws devops-agent associate-service \
    --agent-space-id "<SPACE_ID>" \
    --service-id "aws" \
    --configuration "{\"sourceAws\":{\"assumableRoleArn\":\"arn:aws:iam::${ACCOUNT_ID}:role/DevOpsAgentRole\",\"accountId\":\"${ACCOUNT_ID}\",\"accountType\":\"source\"}}" \
    --region us-east-1
  
  echo "Connected: $ACCOUNT_ID"
done
```
