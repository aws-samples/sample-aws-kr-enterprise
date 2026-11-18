# Security Group 포트 차단 장애 대응

## 증상
- ALB 헬스체크 실패
- 5xx 에러 급증
- 타겟 인스턴스 unhealthy

## 진단 절차
1. ALB 타겟 그룹 헬스 확인
2. EC2 인스턴스 상태 확인 (running 여부)
3. Security Group 인바운드 규칙 확인
4. 필요한 포트(80, 443)가 열려있는지 확인

## 진단 포인트
- 장애 직전에 끝난 파이프라인 실행과 그 커밋이 `lib/sample-app-stack.ts`의 Security Group 규칙(`addIngressRule`)을 바꿨는지 확인합니다.
- EC2 Security Group은 ALB Security Group에서 오는 TCP 80만 허용해야 합니다(`ec2Sg.addIngressRule(albSg, ec2.Port.tcp(80))`). 애플리케이션은 80 포트에서 동작합니다.

## 조치
1. 원인 커밋이 바꾼 규칙을 되돌리는 수정 PR을 엽니다(포트와 소스를 원래 값으로).
2. 사람이 PR을 리뷰하고 병합하면 파이프라인이 배포합니다.
3. 배포 후 ALB 타겟이 healthy로 돌아오고 알람이 OK가 되는지 확인합니다.
4. 파이프라인 밖에서 바뀐 규칙이라면(배포 상태가 main과 다르면) 파이프라인을 다시 실행해 main 상태로 되돌립니다.

## 변경 관리
이 워크로드는 GitOps로 운영합니다. 인프라(CDK)와 애플리케이션 코드는 CodeCommit 저장소 `incident-to-pr-workload`에 있고, main에 병합된 변경은 파이프라인 `incident-to-pr-workload`가 배포합니다. 콘솔이나 AWS CLI로 리소스를 직접 바꾸지 않습니다.

