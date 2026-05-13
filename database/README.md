# Database

AWS 데이터베이스 서비스를 활용한 데모 및 샘플 코드 모음입니다. 관계형 데이터베이스(Aurora, RDS)부터 NoSQL(DynamoDB, DocumentDB), 인메모리 데이터베이스(ElastiCache, MemoryDB)까지 다양한 데이터베이스 유형에 대한 실전 가이드를 제공합니다. 마이그레이션, 파라미터 튜닝, 고가용성 구성 등 엔터프라이즈 환경에서 자주 만나는 시나리오를 중심으로 모범 사례와 샘플 코드를 다룹니다.

## 대표 서비스

- [Amazon Aurora](https://aws.amazon.com/ko/rds/aurora/) - MySQL 및 PostgreSQL 호환의 고성능 관계형 데이터베이스
- [Amazon DynamoDB](https://aws.amazon.com/ko/dynamodb/) - 모든 규모에서 밀리초 미만의 성능을 제공하는 서버리스 NoSQL 데이터베이스
- [Amazon RDS](https://aws.amazon.com/ko/rds/) - 총 소유 비용에 최적화된 완전관리형 관계형 데이터베이스 서비스
- [Amazon ElastiCache](https://aws.amazon.com/ko/elasticache/) - 마이크로초 지연 시간의 서버리스 완전관리형 캐싱 서비스
- [Amazon MemoryDB](https://aws.amazon.com/ko/memorydb/) - Valkey 및 Redis OSS 호환의 초고속 인메모리 데이터베이스
- [Amazon DocumentDB](https://aws.amazon.com/ko/documentdb/) - MongoDB 호환 완전관리형 문서 데이터베이스 서비스

## 프로젝트 목록

| 프로젝트 | 설명 | 주요 서비스 |
|---------|------|------------|
| [aurora-mysql-parameter-group-tuning](./aurora-mysql-parameter-group-tuning/) | Aurora MySQL 3.10.3(LTS) Parameter Group 통합 튜닝 가이드. db.r7g.8xlarge(Graviton3) 대상 IDC 호환성 변경 및 Aurora 전용 파라미터 최적화 | Amazon Aurora MySQL |
| [valkey-migration](./valkey-migration/) | Redis 7.4.6에서 ElastiCache for Valkey 8.2로의 온라인 마이그레이션 테스트. CDK TypeScript 인프라 자동화, 마이그레이션 테스트 결과 및 대안 방법 분석 포함 | Amazon ElastiCache, Redis, Valkey |
| [elasticache-for-realtime-leaderboard](./elasticache-for-realtime-leaderboard/) | ElastiCache for Valkey 8.0 기반 실시간 게임 리더보드 레퍼런스 아키텍처. SQS + Lambda + Valkey ZSET + DynamoDB 조합으로 5,000 TPS 수집, p95 읽기 100ms 미만, 메시지 손실 0% 달성. Apple 스타일 데모 웹 + 부하 생성기 + CloudWatch 대시보드 포함 | Amazon ElastiCache, AWS Lambda, Amazon SQS, Amazon DynamoDB |

## 참고 리소스

- [AWS 데이터베이스 서비스 소개](https://aws.amazon.com/ko/products/databases/) - AWS 클라우드 데이터베이스 서비스 전체 소개 및 선택 가이드
- [Amazon Aurora 사용 설명서](https://docs.aws.amazon.com/ko_kr/AmazonRDS/latest/AuroraUserGuide/CHAP_AuroraOverview.html) - Amazon Aurora 공식 기술 문서
- [Amazon DynamoDB 개발자 가이드](https://docs.aws.amazon.com/ko_kr/amazondynamodb/latest/developerguide/Introduction.html) - Amazon DynamoDB 공식 기술 문서
