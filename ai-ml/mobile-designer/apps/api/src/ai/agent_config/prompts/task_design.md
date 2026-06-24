# Task: UI Design Application (Stage 3)

## Goal
와이어프레임에 the mobile design system 디자인 시스템을 완전히 적용합니다.

## Input
- `command`: 사용자의 수정/트윅 요청
- `context.source_design`: Stage 2 결과 (와이어프레임)
- `context.current_design`: 수정 시 현재 디자인

## Output
```json
{
  "screens": [...],
  "tokens": {
    "colors": {
      "primary": "0xFF1A73E8",
      "onPrimary": "0xFFFFFFFF",
      "surface": "0xFFFFFFFF",
      "onSurface": "0xFF1F1F1F"
    },
    "typography": {
      "displayLarge": {"fontSize": 34, "fontWeight": "Bold"},
      "bodyMedium": {"fontSize": 14, "fontWeight": "Regular"}
    },
    "spacing": {
      "screenMargin": 24,
      "componentGap": 16,
      "cardPadding": 16
    }
  }
}
```

## Rules
1. 모든 컴포넌트에 디자인 스타일 적용:
   - Typography: 적절한 텍스트 크기 (12-34sp)
   - Color: 의미론적 컬러 토큰 사용
   - Spacing: 24dp 마진, 적절한 간격
2. Extend Title 패턴 적용 (메인 화면들)
3. 액션 버튼은 하단에 배치
4. 터치 영역 최소 48dp 보장
5. 컴포넌트 ID 유지 (Stage 2에서 변경 없음)
6. 트윅 요청 시 해당 속성만 변경
