from src.ai.design_guide import DesignGuideService, GUIDE_SECTIONS


class TestDesignGuideService:
    def test_retrieve_section_by_keyword(self) -> None:
        svc = DesignGuideService()
        result = svc.retrieve_section("typography")
        assert "타이포" in result or "typography" in result.lower()

    def test_retrieve_section_spacing(self) -> None:
        svc = DesignGuideService()
        result = svc.retrieve_section("spacing margin")
        assert "간격" in result or "margin" in result.lower() or "spacing" in result.lower()

    def test_retrieve_unknown_query_returns_default(self) -> None:
        svc = DesignGuideService()
        result = svc.retrieve_section("xyznonexistent")
        assert result != ""

    def test_get_system_context(self) -> None:
        svc = DesignGuideService()
        ctx = svc.get_system_context()
        assert "Design Principles" in ctx
        assert "focus" in ctx.lower()
        assert "natural" in ctx.lower()

    def test_with_pdf_content(self) -> None:
        svc = DesignGuideService(pdf_content="This is a test PDF about typography rules and spacing.")
        result = svc.retrieve_section("typography")
        assert "PDF Reference" in result

    def test_guide_sections_populated(self) -> None:
        assert len(GUIDE_SECTIONS) >= 10
        assert "focus" in GUIDE_SECTIONS
        assert "navigation" in GUIDE_SECTIONS
