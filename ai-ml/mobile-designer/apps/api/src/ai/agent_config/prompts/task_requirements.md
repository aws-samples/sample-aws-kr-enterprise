# Task: Requirements Analysis (Stage 1)

## Goal
사용자의 자연어 입력(+ 첨부 파일)으로부터 Android 앱의 요구사항을 분석하고, 화면 구조를 도출합니다.

## Input
- `command`: 사용자의 자연어 요구사항
- `context.files`: 첨부된 파일들의 파싱된 텍스트 내용

## Output
```json
{
  "screens": [
    {
      "name": "화면 이름",
      "purpose": "화면의 목적",
      "key_features": ["기능1", "기능2"],
      "navigation_from": ["이전 화면"],
      "navigation_to": ["다음 화면"]
    }
  ],
  "app_overview": "앱 전체 설명",
  "user_flows": ["주요 사용자 흐름 1", "흐름 2"],
  "constraints": ["제약사항"]
}
```

## Rules
1. 요구사항이 모호하면 합리적인 가정을 포함하되, 가정임을 명시
2. 표준 내비게이션 패턴을 기본 적용 (BottomNavigation for 3-5 tabs, TopAppBar)
3. 화면 수는 사용자 요구에 맞되, 과도하게 분리하지 않음
4. 각 화면의 핵심 기능에 집중 (Focus 원칙)
