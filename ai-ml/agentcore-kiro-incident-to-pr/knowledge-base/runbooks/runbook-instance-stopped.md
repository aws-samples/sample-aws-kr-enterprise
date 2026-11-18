# EC2 인스턴스 중단 장애 대응

## 증상
- ALB 타겟 unhealthy
- 서비스 불가 또는 성능 저하
- EC2 StatusCheckFailed 알람

## 진단 절차
1. EC2 인스턴스 상태 확인 (stopped/terminated)
2. Auto Scaling Group 상태 확인
3. 인스턴스 중단 원인 확인 (수동/스팟 회수/시스템 오류)

## 진단 포인트
- 장애 직전 배포의 커밋이 `lib/sample-app-stack.ts`의 Auto Scaling Group 용량(`minCapacity`, `maxCapacity`)이나 Launch Template을 바꿨는지 확인합니다.

## 조치
1. 원인이 커밋이면 그 값을 되돌리는 수정 PR을 엽니다.
2. 사람이 PR을 리뷰하고 병합하면 파이프라인이 배포합니다.
3. 파이프라인 밖에서 인스턴스가 중단된 경우 Auto Scaling Group이 새 인스턴스로 교체하는지 확인하고, 교체되지 않으면 파이프라인을 다시 실행합니다.
4. ALB 헬스체크가 정상으로 돌아오는지 확인합니다.

## 변경 관리
이 워크로드는 GitOps로 운영합니다. 인프라(CDK)와 애플리케이션 코드는 CodeCommit 저장소 `incident-to-pr-workload`에 있고, main에 병합된 변경은 파이프라인 `incident-to-pr-workload`가 배포합니다. 콘솔이나 AWS CLI로 리소스를 직접 바꾸지 않습니다.

