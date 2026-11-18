# 보안 통제와 한계

이 문서는 [README](../README.md)의 보안 경계를 자세히 설명합니다.

## 적용된 통제

| 영역 | 통제 |
|------|------|
| 웹 인증 | `incident-to-pr/web-password` 시크릿의 패스워드로 로그인하면 12시간 유효한 토큰을 받습니다. 토큰은 시크릿의 `signing_key`로 서명하므로 토큰에서 패스워드를 추측할 수 없습니다. REST는 모든 요청, WebSocket은 연결과 메시지마다 토큰을 확인합니다. 시크릿 값을 바꾸면 기존 토큰은 60초 안에 무효가 됩니다. 로그인은 초당 1회로 throttle합니다. |
| AWS 변경 | AgentCore Runtime 역할에는 AWS 리소스를 바꾸는 권한이 없습니다. 조사 도구는 Describe, Get, List, 로그 조회 API만 씁니다. 모든 변경은 워크로드 저장소의 PR과 배포 파이프라인을 거칩니다. |
| 저장소 쓰기 | Runtime 역할은 워크로드 저장소에 브랜치와 PR을 만들 수 있고(CreateBranch, CreatePullRequest), 파일은 `incident/` 브랜치에만 쓸 수 있습니다(`codecommit:References` 조건이 붙은 PutFile). 브랜치 이름의 `incident/` 접두사는 IAM이 아니라 도구가 정합니다(IAM 조건 키가 CreateBranch를 지원하지 않습니다). 병합, push, 커밋 직접 생성, 브랜치와 파일 삭제, PR 승인 변경은 명시적으로 거부합니다. |
| PR 검사 | PR 검사 CodeBuild는 병합 전 PR의 코드(npm, CDK 앱)를 실행하므로, 그 역할은 워크로드 스택 템플릿 조회와 PR 댓글 작성만 할 수 있습니다. CDK bootstrap 역할을 맡지 않고 AWS를 바꾸지 못합니다. synth에 필요한 가용 영역 조회(`ec2:DescribeAvailabilityZones`) 하나만 추가로 허용합니다. `cdk diff`는 change set 없이 템플릿만 비교합니다. 댓글의 diff는 PR 코드가 만든 결과이므로, 리뷰는 PR의 파일 변경 자체를 기준으로 합니다. IaC는 그 자체가 코드이므로 `lib/`의 변경은 검사 중에 실행됩니다. 이 실행을 막는 것은 경로 제한이 아니라 역할의 권한이 없다는 사실입니다. |
| 배포 | CDK bootstrap 역할로 배포하는 작업은 main을 배포하는 파이프라인 하나뿐입니다. 기본 bootstrap의 CloudFormation 실행 역할은 AdministratorAccess이므로, 운영에서는 `cdk bootstrap --cloudformation-execution-policies`로 워크로드에 필요한 권한만 주세요. |
| PR 범위 | 수정 PR 도구는 리포트마다 Runtime이 만드는 Fixer 세션에서만 동작하고, 세션당 PR 1건만 엽니다. `lib/`(IaC)와 `app/`(애플리케이션) 아래의 이미 있는 파일만 최대 5개까지 바꿀 수 있습니다. `package.json`, lockfile, `cdk.json`, `bin/` 같은 빌드 설정과 경로 탈출(`..`, 절대 경로), 변경 없는 파일은 거부합니다. 이 제한은 리뷰 범위를 좁히기 위한 것이고, PR 검사의 코드 실행을 막는 통제가 아닙니다. 리포트에 저장하는 PR 정보는 모델의 응답이 아니라 도구가 남긴 기록으로 정하고, Writer 응답에서는 요약, 타임라인, 원인, 영향, 권장 조치만 받습니다. |
| 결과 판정 | 리포트는 PR이 병합되고, 성공한 파이프라인 실행이 빌드한 커밋이 병합 커밋이거나 그 뒤의 커밋일 때만 "배포 완료"(`RESOLVED`)이며, 그 실행 ID와 커밋을 리포트에 기록합니다. 나중의 롤백은 반영하지 않습니다. Reviewer 판정이 없거나, 읽을 수 없거나, PASS와 FAIL이 섞여 있거나, 3회 모두 FAIL이면 리포트는 `FAILED`이고 PR을 만들지 않습니다. |
| 알람 처리 | 등록한 워크로드의 alarm prefix와 맞는 알람만 분석합니다. 리포트 ID는 EventBridge 이벤트에서 만들고 조건부로 저장하므로, SQS가 같은 이벤트를 다시 전달해도 분석은 한 번입니다. 같은 알람이 다시 울리면 새 리포트와 PR이 생길 수 있습니다. |
| 지식 문서 | 업로드는 presigned POST로 1MB 이하, 워크로드당 20개 텍스트 파일만 받습니다. 20개에는 아직 업로드하지 않은 유효한 업로드 티켓도 포함됩니다(워크로드 항목에 만료 시각과 함께 기록). 파일 이름과 workload ID는 경로 문자를 쓸 수 없습니다. Runtime은 동기화 목록 전체를 먼저 검증하고, 목록에 기록된 객체 버전(ETag)만 크기 상한 안에서 받아 한 번에 교체합니다. 실패하면 기존 지식을 유지합니다. |
| Slack | `https://hooks.slack.com/services/...` 형식만 저장하고, 리디렉션을 따르지 않습니다. API 응답에는 Webhook URL을 돌려주지 않고 설정 여부만 표시합니다. |
| 브라우저 | CloudFront가 Content-Security-Policy(이 사이트, 채팅 WebSocket, 지식 버킷 업로드만 허용), HSTS, `X-Frame-Options: DENY`, `nosniff`, Referrer-Policy를 붙입니다. 에이전트가 쓴 Markdown의 이미지는 불러오지 않고 텍스트로 표시합니다. |

## 남는 한계

- 데모는 PR 병합에 필요한 승인 규칙을 만들지 않습니다. 운영에서는 CodeCommit 승인 규칙 템플릿으로 리뷰어 승인을 강제하고, 병합 권한을 리뷰어로 제한하세요.
- 사용자 신원이 없어 Web UI에서 누가 무엇을 했는지 기록하지 않습니다. 채팅의 장기 기억(Kiro memory)도 모든 사용자가 공유합니다(마지막 변경 후 30일 보존).
- API Gateway throttling은 순간 요청을 제한할 뿐 사용량 상한이 아닙니다. Kiro 구독 사용량과 AWS 비용은 따로 확인하세요.
- 데이터는 스택 삭제와 함께 지워지며 백업하지 않습니다(DynamoDB, S3, RDS, CodeCommit 저장소).
- 사람이 PR을 리뷰하는 것이 최종 경계입니다. 별도 에이전트가 Fixer의 코드 변경 자체를 검증하지는 않습니다.
- 샘플 워크로드는 root로 Gunicorn을 실행하고, RDS 마스터 계정으로 접속하며, ALB는 HTTP만 엽니다. 운영 워크로드의 예로 쓰지 마세요.
- 지식 동기화의 디렉터리 교체는 두 번의 rename이고 동시 실행 잠금이 없어, 교체 도중 실패에 대한 완전한 원자성은 보장하지 않습니다.
- Web UI 의존성 중 브라우저에 배포되는 것은 `npm audit --omit=dev`로 확인합니다. 빌드 도구(`react-scripts`의 webpack, jest 등)에는 advisory가 많이 남아 있지만 빌드에만 쓰이고 배포물에는 들어가지 않습니다. 배포 대상에 남은 `react-router` advisory 2건은 외부에서 받은 경로를 `Link`나 `navigate`에 넘길 때와 서버 사이드 렌더링에서 성립하는데, 이 앱은 내부 경로만 쓰고 SSR을 쓰지 않습니다. 6.x에는 수정 버전이 없어 그대로 두었습니다.
- 파이프라인 밖에서 바뀐 리소스(콘솔에서 직접 바꾼 경우)는 PR로 고칠 수 없습니다. 에이전트는 배포 상태가 main과 다르다고 보고하고, 파이프라인을 다시 실행해 main 상태로 되돌리도록 권장합니다.
