# DB 연결 차단 장애 대응

## 증상
- 백엔드 DB 연결 타임아웃
- 500 에러 응답
- RDS DatabaseConnections = 0

## 진단 절차
1. RDS 인스턴스 상태 확인 (available 여부)
2. RDS Security Group 인바운드 규칙 확인
3. EC2에서 RDS로 가는 네트워크 경로 확인
4. 5432 포트가 EC2 Security Group에서 허용되는지 확인

## 진단 포인트
- 장애 직전 배포의 커밋이 `lib/sample-app-stack.ts`의 RDS Security Group 규칙(`rdsSg.addIngressRule(ec2Sg, ec2.Port.tcp(5432))`)이나 DB 설정을 바꿨는지 확인합니다.

## 조치
1. 원인 커밋이 바꾼 규칙이나 설정을 되돌리는 수정 PR을 엽니다.
2. 사람이 PR을 리뷰하고 병합하면 파이프라인이 배포합니다.
3. 배포 후 DB 연결과 헬스체크가 정상으로 돌아오는지 확인합니다.
4. 파이프라인 밖에서 바뀐 규칙이라면 파이프라인을 다시 실행해 main 상태로 되돌립니다.

## 변경 관리
이 워크로드는 GitOps로 운영합니다. 인프라(CDK)와 애플리케이션 코드는 CodeCommit 저장소 `incident-to-pr-workload`에 있고, main에 병합된 변경은 파이프라인 `incident-to-pr-workload`가 배포합니다. 콘솔이나 AWS CLI로 리소스를 직접 바꾸지 않습니다.

