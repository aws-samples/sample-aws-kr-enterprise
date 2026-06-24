# Task: Compose Structure Decision (Handoff)

## Goal
디자인을 Jetpack Compose 프로젝트 구조로 변환하기 위한 구조를 결정합니다.

## Input
- `context.design`: Stage 3 최종 디자인
- `context.component_catalog`: 허용된 Compose 컴포넌트 카탈로그

## Output
```json
{
  "project_structure": {
    "screens": [
      {
        "name": "MainScreen",
        "file_path": "ui/screens/MainScreen.kt",
        "composables": ["MainContent", "MainTopBar", "MainBottomNav"]
      }
    ],
    "theme": {
      "color_scheme": "light",
      "custom_colors": {}
    },
    "navigation": {
      "type": "bottom_nav",
      "routes": [{"route": "home", "screen": "MainScreen"}]
    }
  }
}
```

## Rules
1. shared-component-catalog.md의 컴포넌트만 사용
2. Material 3 기반 Compose 구조
3. 화면별 @Composable 함수 분리
4. Navigation Compose 사용
5. Theme은 디자인 토큰에서 자동 매핑
