# AWS DevOps Agent 

## What is AWS DevOps Agent?

[AWS DevOps Agent](https://aws.amazon.com/devops-agent/)는 인시던트를 해결/예방하고, 애플리케이션 신뢰성 및 성능을 최적화하며, AWS/멀티클라우드/온프레미스 환경 전반에서 온디맨드 SRE 태스크를 처리하는 **AI 운영 에이전트**입니다.

**핵심 기능:**
- **문제 신속 해결**: 24시간 자율적으로 인시던트 분류, 관찰성 도구/런북/코드 리포지토리/CI/CD 파이프라인을 활용한 근본 원인 분석
- **향후 인시던트 예방**: 과거 인시던트 패턴 분석을 통해 관찰성, 인프라 최적화, 배포 파이프라인 개선, 복원력 강화 권고
- **운영 인사이트 활용**: CloudWatch 등 기본 통합 + MCP 서버로 사용자 정의 도구 확장
- **온디맨드 SRE 태스크 가속화**: 자연어로 리소스 상태 조회, 차트/보고서 생성 및 공유

## Key Concepts

| 개념 | 설명 |
|---|---|
| **Agent Space** | DevOps Agent의 운영 단위. 연결된 리소스만 조회 가능 |
| **Monitor Account** | Space의 기본 계정. Source 계정 연결 전 필수 등록 |
| **Source Account** | 모니터링 대상이 되는 추가 계정 |

## Prerequisites

- AWS CLI **2.35.12** 이상
- AWS Organizations 활성화
- IAM 권한: `aidevops:*`, `iam:*`, `organizations:*`

## Quick Start

```bash
# 1. Space 생성
aws devops-agent create-agent-space \
  --name "My-Ops-Space" \
  --description "전체 계정 모니터링" \
  --region us-east-1

# 2. Monitor 계정 연결
aws devops-agent associate-service \
  --agent-space-id "<SPACE_ID>" \
  --service-id "aws" \
  --configuration '{"aws":{"assumableRoleArn":"arn:aws:iam::<MGMT_ACCOUNT>:role/DevOpsAgentRole","accountId":"<MGMT_ACCOUNT>","accountType":"monitor"}}' \
  --region us-east-1

# 3. Source 계정 연결
aws devops-agent associate-service \
  --agent-space-id "<SPACE_ID>" \
  --service-id "aws" \
  --configuration '{"sourceAws":{"assumableRoleArn":"arn:aws:iam::<SOURCE_ACCOUNT>:role/DevOpsAgentRole","accountId":"<SOURCE_ACCOUNT>","accountType":"source"}}' \
  --region us-east-1
```

## References

- [AWS DevOps Agent CLI Reference](https://docs.aws.amazon.com/cli/latest/reference/devops-agent/)
- [Production-Ready Autonomous Incident Resolution with AWS DevOps Agent (GA)](https://aws.amazon.com/blogs/devops/production-ready-autonomous-incident-resolution-with-aws-devops-agent-now-ga-and-datadog-mcp-server/)
- [How AWS DevOps Agent uses multi-agent reasoning to find root causes](https://aws.amazon.com/blogs/devops/how-aws-devops-agent-uses-multi-agent-reasoning-to-find-root-causes/)
