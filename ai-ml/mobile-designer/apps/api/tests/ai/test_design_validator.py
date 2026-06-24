from src.ai.design_validator import DesignValidator


class TestDesignValidator:
    def setup_method(self) -> None:
        self.validator = DesignValidator()

    def test_valid_design(self) -> None:
        design = {
            "components": [
                {"id": "comp-1", "type": "TopAppBar", "props": {}, "style": {"marginStart": 24}},
                {"id": "comp-2", "type": "Button", "props": {"text": "Submit"}, "style": {"fontSize": 14}},
            ]
        }
        result = self.validator.validate(design, "design")
        assert result["valid"]
        assert len(result["violations"]) == 0

    def test_invalid_component_type(self) -> None:
        design = {
            "components": [
                {"id": "comp-1", "type": "InvalidWidget", "props": {}, "style": {}},
            ]
        }
        result = self.validator.validate(design, "design")
        assert not result["valid"]
        assert any(v["rule"] == "COMPONENT_VALIDITY" for v in result["violations"])

    def test_margin_below_minimum(self) -> None:
        design = {
            "components": [
                {"id": "comp-1", "type": "Card", "props": {}, "style": {"marginStart": 8}},
            ]
        }
        result = self.validator.validate(design, "design")
        assert not result["valid"]
        assert any(v["rule"] == "MARGIN_MIN_24DP" for v in result["violations"])

    def test_margin_zero_is_allowed(self) -> None:
        design = {
            "components": [
                {"id": "comp-1", "type": "Card", "props": {}, "style": {"marginStart": 0}},
            ]
        }
        result = self.validator.validate(design, "design")
        assert result["valid"]

    def test_text_size_below_minimum(self) -> None:
        design = {
            "components": [
                {"id": "comp-1", "type": "Button", "props": {}, "style": {"fontSize": 10}},
            ]
        }
        result = self.validator.validate(design, "design")
        assert not result["valid"]
        assert any(v["rule"] == "TEXT_SIZE_MIN" for v in result["violations"])

    def test_text_size_above_maximum(self) -> None:
        design = {
            "components": [
                {"id": "comp-1", "type": "Button", "props": {}, "style": {"fontSize": 40}},
            ]
        }
        result = self.validator.validate(design, "design")
        assert not result["valid"]
        assert any(v["rule"] == "TEXT_SIZE_MAX" for v in result["violations"])

    def test_empty_components_is_valid(self) -> None:
        design = {"components": []}
        result = self.validator.validate(design, "wireframe")
        assert result["valid"]

    def test_no_components_key_is_valid(self) -> None:
        design = {}
        result = self.validator.validate(design, "design")
        assert result["valid"]
