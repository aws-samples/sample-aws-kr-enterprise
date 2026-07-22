# Build Module

CodeBuild 기반 컨테이너 이미지 빌드 파이프라인.

## 생성되는 리소스

| Resource | Name Pattern | 용도 |
|----------|-------------|------|
| S3 Bucket | `{prefix}-codebuild-source-{account_id}-{region}` | CodeBuild 소스 아카이브 저장 (7일 만료) |
| CodeBuild Project (x86) | `{prefix}-build-x86` | platform-api, frontend 이미지 (amd64) |
| CodeBuild Project (arm64) | `{prefix}-build-arm64` | base-image, report-image (arm64, AgentCore Runtime) |
| IAM Role | `{prefix}-codebuild` | CodeBuild 서비스 역할 (ECR push + S3 read + CloudWatch Logs) |

## 아키텍처

```
scripts/build-images.sh
  ├── zip source → S3 upload
  ├── start-build (x86) → platform-api, frontend → ECR push
  └── start-build (arm64) → base-image, report-image → ECR push
```

## 입력 변수

| Variable | Type | Description |
|----------|------|-------------|
| `prefix` | string | 리소스 이름 prefix |
| `aws_region` | string | AWS 리전 |
| `account_id` | string | 12자리 AWS Account ID |
| `ecr_repo_arns` | map(string) | registry 모듈의 ECR repo ARN map |
| `tags` | map(string) | 공통 태그 |

## 출력

| Output | Description |
|--------|-------------|
| `codebuild_project_arm64` | arm64 CodeBuild 프로젝트 이름 |
| `codebuild_project_x86` | x86 CodeBuild 프로젝트 이름 |
| `source_bucket` | S3 소스 버킷 이름 |
| `codebuild_role_arn` | CodeBuild IAM role ARN |

## 배포

```bash
# Terraform으로 인프라 배포 후 이미지 빌드
cd iac/envs/dev
terraform apply

# 이미지 빌드 (CodeBuild 트리거)
cd scripts
./build-images.sh
```

## 삭제

```bash
cd iac/envs/dev
terraform destroy -target=module.build
```

전체 인프라를 삭제하는 경우 `terraform destroy`에 build 모듈이 포함됩니다.
S3 버킷은 `force_destroy = true`로 설정되어 있어 오브젝트와 함께 삭제됩니다.
