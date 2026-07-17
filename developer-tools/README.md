# Developer Tools

AWS 개발자 도구 및 DevOps 서비스를 활용한 데모 및 샘플 코드 모음입니다. Kiro를 활용한 AI 기반 개발 워크플로우, AWS CDK와 CloudFormation을 통한 인프라스트럭처 자동화, 그리고 CodePipeline/CodeBuild/CodeDeploy로 구성하는 CI/CD 파이프라인까지 현대적인 소프트웨어 개발 라이프사이클 전반을 다룹니다. 개발 생산성 향상과 운영 자동화를 위한 실전 패턴과 가이드를 제공합니다.

## 대표 서비스

- [Kiro](https://kiro.dev/) - 스펙 기반 개발을 지원하는 에이전틱 AI IDE 및 CLI
- [AWS CDK](https://aws.amazon.com/ko/cdk/) - 프로그래밍 언어로 클라우드 인프라를 정의하는 오픈소스 프레임워크
- [AWS CloudFormation](https://aws.amazon.com/ko/cloudformation/) - 코드형 인프라(IaC)로 클라우드 프로비저닝을 자동화하는 서비스
- [AWS CodePipeline](https://aws.amazon.com/ko/codepipeline/) - 빠르고 안정적인 업데이트를 위한 지속적 전달 파이프라인 자동화 서비스
- [AWS CodeBuild](https://aws.amazon.com/ko/codebuild/) - 자동 크기 조정을 지원하는 완전관리형 빌드 서비스
- [AWS CodeDeploy](https://aws.amazon.com/ko/codedeploy/) - 코드 배포를 자동화하여 애플리케이션 가동 시간을 유지하는 서비스
- [Amazon CodeCatalyst](https://aws.amazon.com/ko/codecatalyst/) - AWS에서 대규모로 빠르게 앱을 구축하고 제공하는 통합 개발 서비스

## 프로젝트 목록

| 프로젝트 | 설명 | 주요 서비스 |
|---------|------|------------|
| [structured-ai-dev-workflow](./structured-ai-dev-workflow/) | Kiro Subagent를 활용한 구조화된 AI 개발 워크플로우. Multi-agent 시스템(Dev, Review, QA, Docs) 설계, Anthropic 연구 기반 프롬프트 엔지니어링 가이드 | Kiro |
| [agentcore-websearch-mcp](./agentcore-websearch-mcp/) | Amazon Bedrock AgentCore Gateway로 웹 검색 MCP 서버를 구축하는 CloudFormation 템플릿과 배포 스크립트. 게이트웨이·IAM 역할·web-search 커넥터를 한 번에 프로비저닝하고 Claude Code에 MCP 서버로 자동 등록 | Amazon Bedrock AgentCore, AWS CloudFormation |

## 참고 리소스

- [AWS CDK 개발자 가이드](https://docs.aws.amazon.com/ko_kr/cdk/v2/guide/home.html) - AWS CDK v2 공식 기술 문서
- [AWS CloudFormation 사용 설명서](https://docs.aws.amazon.com/ko_kr/AWSCloudFormation/latest/UserGuide/Welcome.html) - AWS CloudFormation 공식 기술 문서
- [Kiro 공식 문서](https://kiro.dev/docs/) - Kiro IDE 설치 및 활용 가이드
