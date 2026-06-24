# AI/ML

AWS의 AI/ML 서비스를 활용한 데모 및 샘플 코드 모음입니다. Amazon Bedrock을 통한 생성형 AI 애플리케이션 구축부터, Amazon SageMaker 기반의 머신러닝 모델 학습 및 배포, 그리고 Amazon Rekognition, Textract 등 사전 학습된 AI 서비스 활용까지 다양한 시나리오를 다룹니다. 엔터프라이즈 환경에서 AI/ML 워크로드를 효과적으로 구현하기 위한 아키텍처 패턴과 모범 사례를 제공합니다.

## 대표 서비스

- [Amazon Bedrock](https://aws.amazon.com/ko/bedrock/) - 파운데이션 모델을 활용한 생성형 AI 애플리케이션 및 에이전트 구축 플랫폼
- [Amazon Bedrock AgentCore](https://aws.amazon.com/ko/bedrock/agentcore/) - AI 에이전트를 프로덕션 환경에서 실행·관리하는 런타임 플랫폼
- [Amazon SageMaker](https://aws.amazon.com/ko/sagemaker/) - 데이터, 분석, AI를 위한 통합 머신러닝 플랫폼
- [Amazon Nova](https://aws.amazon.com/ko/nova/) - 빠르고 비용 효율적인 파운데이션 모델 포트폴리오
- [Amazon Rekognition](https://aws.amazon.com/ko/rekognition/) - ML 기반 이미지 인식 및 비디오 분석 서비스
- [Amazon Textract](https://aws.amazon.com/ko/textract/) - 문서에서 텍스트, 필기, 데이터를 자동 추출하는 ML 서비스

## 프로젝트 목록

| 프로젝트 | 설명 | 주요 서비스 |
|---------|------|------------|
| [bedrock-detailed-monitoring-dashboard](./bedrock-detailed-monitoring-dashboard/) | Amazon Bedrock Claude 모델 사용량과 비용을 실시간 모니터링하는 웹 대시보드. CloudWatch 메트릭 기반 7개 모델 추적, 4단계 집계(분/시/일/월), 캐시 절감 분석, 월말 비용 예측 | Amazon Bedrock, CloudFront, ECS Fargate, DynamoDB, Lambda |
| [claude-code-telemetry-aws](./claude-code-telemetry-aws/) | Claude Code OpenTelemetry 텔레메트리 수집·분석 관측성 플랫폼. 이중 파이프라인(Prometheus 메트릭 + Athena 이벤트)으로 6개 Grafana 대시보드(80패널) 제공. 비용·사용량·성능 통합 모니터링 | Amazon Managed Prometheus, Amazon Managed Grafana, Athena, ECS Fargate, Kinesis Data Firehose |
| [x86-to-graviton-with-aws-transform-custom](./x86-to-graviton-with-aws-transform-custom/) | AWS Transform Custom을 활용한 x86 Java 애플리케이션의 Graviton(ARM64) 전환 가이드. Spring Boot 3.2.1 샘플앱 대상, atx CLI v1.1.1 사용, 8단계 변환 프로세스 100% 완료 | AWS Transform Custom, Amazon EC2 (Graviton) |
| [agentcore-aiops-demo](./agentcore-aiops-demo/) | LLM 기반 멀티 에이전트 AIOps 플랫폼. CloudWatch 알람 발생 시 Reflexion 패턴(Collector→Writer→Reviewer) RCA Graph가 자동으로 근본 원인을 분석하고 한국어 리포트를 생성. 사용자 승인 후 Executor Agent가 자동 복구 수행. WebSocket 채팅으로 자연어 인프라 조사 지원 | Bedrock AgentCore, Strands Agents, Bedrock Knowledge Base, OpenSearch Serverless, DynamoDB, Lambda, CloudFront |
| [claude-code-bedrock-enterprise-blueprint](./claude-code-bedrock-enterprise-blueprint/) | Claude Code on Amazon Bedrock 엔터프라이즈 블루프린트. IAM Identity Center SSO 인증, LiteLLM Proxy 기반 LLM Gateway, Virtual Key 자동 생성/캐싱, 사용자별 예산 관리, CloudWatch 모니터링까지 엔드투엔드 인프라를 CDK NestedStack으로 구현 | Amazon Bedrock, IAM Identity Center, ECS Fargate, ALB, Aurora Serverless v2, DynamoDB, Lambda, API Gateway, CloudWatch |
| [mobile-designer](./mobile-designer/) | AI 기반 Android UI 디자인 도구. 자연어 대화와 파일 업로드로 앱 요구사항을 수집한 뒤, 요구사항→와이어프레임→디자인→핸드오프 4단계 워크플로우로 설계하고, Android Studio에서 바로 빌드 가능한 Jetpack Compose 프로젝트(ZIP)로 핸드오프. 단계 전파·실시간 디자인 토큰 트윅·코멘트/공유 협업 지원 | Amazon Bedrock, Strands Agents, ECS Fargate, ALB, DynamoDB, S3, CloudFront, Secrets Manager |

## 참고 리소스

- [AWS AI/ML 서비스 소개](https://aws.amazon.com/ko/ai/machine-learning/) - AWS 기계 학습 서비스 전체 소개 및 고객 사례
- [Amazon Bedrock 사용 설명서](https://docs.aws.amazon.com/ko_kr/bedrock/latest/userguide/what-is-bedrock.html) - Amazon Bedrock 공식 기술 문서
- [Amazon SageMaker 사용 설명서](https://docs.aws.amazon.com/ko_kr/sagemaker/latest/dg/whatis.html) - Amazon SageMaker 공식 기술 문서
