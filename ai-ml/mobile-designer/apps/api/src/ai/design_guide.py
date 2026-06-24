import asyncio

import structlog

from src.files.chunking import chunk_for_context

logger = structlog.get_logger()

GUIDE_SECTIONS = {
    "focus": "Focus 원칙: 핵심 콘텐츠에 집중, 불필요한 요소 제거",
    "natural": "Natural 원칙: 자연스러운 인터랙션, 부드러운 전환",
    "essential": "Essential 원칙: 필수 기능만 노출, 단순한 구조",
    "typography": "타이포그래피: 본문 14sp, 제목 20-34sp, 최소 12sp",
    "color": "컬러 시스템: Primary/Secondary/Surface/Error 토큰",
    "spacing": "간격: 기본 마진 24dp, 컴포넌트 간 간격 8/16dp",
    "navigation": "내비게이션: Bottom Navigation(3-5항목), TopAppBar(Extend Title)",
    "interaction": "인터랙션: 터치 영역 48dp 최소, 하단 액션 배치",
    "extend_title": "Extend Title: 스크롤 시 축소되는 대형 타이틀 패턴",
    "view_interaction": "View/Interaction 분리: 정보 표시(View)와 조작(Interaction) 영역 분리",
}


class DesignGuideService:
    def __init__(self, pdf_content: str | None = None) -> None:
        self._pdf_sections: dict[str, str] = {}
        self._loaded = False
        if pdf_content:
            self._index_content(pdf_content)

    def _index_content(self, content: str) -> None:
        self._full_content = content
        self._loaded = True

    async def load_from_file(self, file_path: str) -> None:
        def _read() -> str:
            try:
                from pypdf import PdfReader

                reader = PdfReader(file_path)
                pages = []
                for page in reader.pages:
                    text = page.extract_text()
                    if text:
                        pages.append(text)
                return "\n\n".join(pages)
            except Exception as e:
                logger.error("design_guide_load_error", error=str(e))
                return ""

        content = await asyncio.to_thread(_read)
        self._index_content(content)

    def retrieve_section(self, query: str) -> str:
        query_lower = query.lower()

        matching_sections: list[str] = []
        for key, description in GUIDE_SECTIONS.items():
            if key in query_lower or any(word in query_lower for word in description.split()):
                matching_sections.append(f"## {key}\n{description}")

        if self._loaded and hasattr(self, "_full_content"):
            relevant_chunk = chunk_for_context(self._full_content, max_total_size=4000)
            matching_sections.append(f"\n## PDF Reference\n{relevant_chunk}")

        return "\n\n".join(matching_sections) if matching_sections else GUIDE_SECTIONS.get("focus", "")

    def get_system_context(self) -> str:
        lines = ["# Design Principles\n"]
        for key, desc in GUIDE_SECTIONS.items():
            lines.append(f"- **{key}**: {desc}")
        return "\n".join(lines)
