# Task: Wireframe Generation (Stage 2)

## Goal
요구사항 분석 결과를 기반으로 와이어프레임 수준의 UI 구조를 생성합니다.

## Input
- `command`: 사용자의 수정 요청 (초기 생성 시 "Generate wireframe")
- `context.source_design`: Stage 1 결과 (화면 구조)
- `context.current_design`: 수정 시 현재 와이어프레임

## Output
```json
{
  "screens": [
    {
      "name": "Screen Name",
      "components": [
        {
          "id": "screen-name-component-role",
          "type": "ComponentType",
          "props": {"text": "Label", "placeholder": "Hint"},
          "style": {"width": "match_parent"},
          "children": []
        }
      ]
    }
  ]
}
```

## Rules
1. 컴포넌트 ID는 안정적이어야 함: `{screenName}-{componentRole}` 패턴
2. 와이어프레임은 구조와 배치에 집중 (색상/타이포는 Stage 3에서)
3. 배치 규칙 적용: 하단 액션, 24dp 마진, View/Interaction 분리
4. 수정 시 변경되지 않는 컴포넌트의 ID를 유지
5. selected_component_id가 있으면 해당 컴포넌트만 수정
