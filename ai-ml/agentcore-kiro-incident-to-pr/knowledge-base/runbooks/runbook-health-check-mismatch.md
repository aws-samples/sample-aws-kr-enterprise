# ALB 헬스체크 설정 오류 장애 대응

## 증상
- ALB 타겟 unhealthy, 사유 `Target.ResponseCodeMismatch` (헬스체크 응답이 404 등 200이 아닌 코드)
- `incident-to-pr-alb-unhealthy` 알람 발생
- EC2 인스턴스와 RDS는 정상이고 Security Group 규칙도 변하지 않음

## 진단 절차
1. ALB 타겟 그룹 헬스와 unhealthy 사유 확인
2. 타겟 그룹의 헬스체크 경로, 포트, 성공 코드 확인
3. 애플리케이션이 응답하는 헬스체크 경로 확인 (`app/app.py`의 `/health`)
4. 장애 직전에 끝난 파이프라인 실행과 그 커밋이 `lib/sample-app-stack.ts`의 `healthCheck` 설정을 바꿨는지 확인

## 진단 포인트
- 이 워크로드의 헬스체크 경로는 `/health`입니다(`healthCheck: { path: "/health", ... }`). 애플리케이션은 다른 경로에 404를 돌려줍니다.
- 타겟 그룹 설정 변경은 CloudFormation이 하므로, CloudTrail의 `ModifyTargetGroup` 호출 주체는 배포 파이프라인의 CDK 역할입니다.

## 조치
1. 원인 커밋이 바꾼 헬스체크 설정을 원래 값으로 되돌리는 수정 PR을 엽니다.
2. 사람이 PR을 리뷰하고 병합하면 파이프라인이 배포합니다.
3. 배포 후 ALB 타겟이 healthy로 돌아오고 알람이 OK가 되는지 확인합니다.

## 변경 관리
이 워크로드는 GitOps로 운영합니다. 인프라(CDK)와 애플리케이션 코드는 CodeCommit 저장소 `incident-to-pr-workload`에 있고, main에 병합된 변경은 파이프라인 `incident-to-pr-workload`가 배포합니다. 콘솔이나 AWS CLI로 리소스를 직접 바꾸지 않습니다.
