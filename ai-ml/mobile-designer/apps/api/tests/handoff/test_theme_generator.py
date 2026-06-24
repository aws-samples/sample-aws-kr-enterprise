from src.handoff.code_generator.theme_generator import ThemeGenerator


class TestThemeGenerator:
    def setup_method(self) -> None:
        self.generator = ThemeGenerator()

    def test_generates_three_files(self) -> None:
        tokens = {"colors": {"primary": "0xFF0000FF"}, "typography": {}}
        files = self.generator.generate(tokens)
        assert "app/src/main/java/com/mdesigner/app/ui/theme/Color.kt" in files
        assert "app/src/main/java/com/mdesigner/app/ui/theme/Type.kt" in files
        assert "app/src/main/java/com/mdesigner/app/ui/theme/Theme.kt" in files

    def test_color_file_contains_package(self) -> None:
        files = self.generator.generate({})
        color_file = files["app/src/main/java/com/mdesigner/app/ui/theme/Color.kt"]
        assert "package com.mdesigner.app.ui.theme" in color_file

    def test_theme_uses_material3(self) -> None:
        files = self.generator.generate({})
        theme_file = files["app/src/main/java/com/mdesigner/app/ui/theme/Theme.kt"]
        assert "MaterialTheme" in theme_file

    def test_typography_includes_body(self) -> None:
        files = self.generator.generate({})
        type_file = files["app/src/main/java/com/mdesigner/app/ui/theme/Type.kt"]
        assert "bodyMedium" in type_file
