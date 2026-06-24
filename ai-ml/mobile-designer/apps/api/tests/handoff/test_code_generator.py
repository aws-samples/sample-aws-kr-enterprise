from src.handoff.code_generator.generator import CodeGenerator
from src.handoff.code_generator.compose_spec import ComposeSpecMapper


class TestCodeGenerator:
    def setup_method(self) -> None:
        self.gen = CodeGenerator()

    def test_generate_empty_design(self) -> None:
        files = self.gen.generate({"screens": [], "tokens": {}})
        assert "app/build.gradle.kts" in files
        assert "settings.gradle.kts" in files
        assert "app/src/main/AndroidManifest.xml" in files
        assert "app/src/main/java/com/mdesigner/app/MainActivity.kt" in files

    def test_generate_with_screens(self) -> None:
        design = {
            "screens": [
                {"name": "Home", "components": [{"id": "home-title", "type": "TopAppBar", "props": {"text": "홈"}, "style": {}}]},
                {"name": "Settings", "components": []},
            ],
            "tokens": {"colors": {"primary": "0xFF0000"}},
        }
        files = self.gen.generate(design)
        assert "app/src/main/java/com/mdesigner/app/ui/screens/HomeScreen.kt" in files
        assert "app/src/main/java/com/mdesigner/app/ui/screens/SettingsScreen.kt" in files
        assert "HomeScreen" in files["app/src/main/java/com/mdesigner/app/MainActivity.kt"]
        assert "SettingsScreen" in files["app/src/main/java/com/mdesigner/app/MainActivity.kt"]

    def test_gradle_contains_compose_dependency(self) -> None:
        files = self.gen.generate({"screens": [], "tokens": {}})
        gradle = files["app/build.gradle.kts"]
        assert "compose" in gradle.lower()
        assert "material3" in gradle

    def test_manifest_has_main_activity(self) -> None:
        files = self.gen.generate({"screens": [], "tokens": {}})
        manifest = files["app/src/main/AndroidManifest.xml"]
        assert "MainActivity" in manifest
        assert "LAUNCHER" in manifest

    def test_theme_files_generated(self) -> None:
        files = self.gen.generate({"screens": [], "tokens": {"colors": {}}})
        assert "app/src/main/java/com/mdesigner/app/ui/theme/Color.kt" in files
        assert "app/src/main/java/com/mdesigner/app/ui/theme/Type.kt" in files
        assert "app/src/main/java/com/mdesigner/app/ui/theme/Theme.kt" in files


class TestComposeSpecMapper:
    def setup_method(self) -> None:
        self.mapper = ComposeSpecMapper()

    def test_map_screen_basic(self) -> None:
        screen = {
            "name": "Login",
            "components": [
                {"id": "login-title", "type": "TopAppBar", "props": {"text": "로그인"}, "style": {}, "children": []},
                {"id": "login-btn", "type": "Button", "props": {"text": "로그인"}, "style": {}},
            ],
        }
        files = self.mapper.map_screen(screen)
        assert "app/src/main/java/com/mdesigner/app/ui/screens/LoginScreen.kt" in files
        content = files["app/src/main/java/com/mdesigner/app/ui/screens/LoginScreen.kt"]
        assert "LoginScreen" in content
        assert "@Composable" in content
        assert "Scaffold" in content

    def test_map_screen_empty_components(self) -> None:
        screen = {"name": "Empty", "components": []}
        files = self.mapper.map_screen(screen)
        content = files["app/src/main/java/com/mdesigner/app/ui/screens/EmptyScreen.kt"]
        assert "EmptyScreen" in content
