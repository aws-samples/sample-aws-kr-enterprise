from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from src.handoff.code_generator.compose_spec import ComposeSpecMapper
from src.handoff.code_generator.llm_code_generator import LLMCodeGenerator
from src.handoff.code_generator.theme_generator import ThemeGenerator

logger = structlog.get_logger()

# Bundled standard Gradle wrapper (matches the 8.5 distribution referenced in
# gradle-wrapper.properties). Shipped so the generated project opens in Android
# Studio without a manual `gradle wrapper` step.
GRADLE_WRAPPER_JAR_ENTRY = "gradle/wrapper/gradle-wrapper.jar"


class CodeGenerator:
    def __init__(self) -> None:
        self._compose_mapper = ComposeSpecMapper()
        self._theme_generator = ThemeGenerator()
        self._llm_generator = LLMCodeGenerator()

    def generate(self, design_data: dict[str, Any]) -> dict[str, str]:
        """Synchronous template-based generation (legacy fallback)."""
        files: dict[str, str] = {}

        files.update(self._generate_gradle_files(design_data))

        theme_files = self._theme_generator.generate(design_data.get("tokens", {}))
        files.update(theme_files)

        screens = design_data.get("screens", [])
        for screen in screens:
            screen_files = self._compose_mapper.map_screen(screen)
            files.update(screen_files)

        files["app/src/main/java/com/mdesigner/app/MainActivity.kt"] = self._generate_main_activity(screens)
        files["app/src/main/AndroidManifest.xml"] = self._generate_manifest()
        files.update(self._generate_res_files())

        return files

    async def generate_with_llm(
        self, design_data: dict[str, Any], progress_callback: Callable[..., None] | None = None
    ) -> dict[str, str]:
        """LLM-based generation: screens via Claude, boilerplate via templates."""
        files: dict[str, str] = {}

        files.update(self._generate_gradle_files(design_data))

        tokens = design_data.get("tokens", {})
        theme_files = self._theme_generator.generate(tokens)
        files.update(theme_files)

        screens = design_data.get("screens", [])

        if screens:
            screen_files = await self._llm_generator.generate_all_screens(screens, tokens, progress_callback)
            files.update(screen_files)
        else:
            logger.warning("no_screens_for_llm_generation")

        files["app/src/main/java/com/mdesigner/app/MainActivity.kt"] = self._generate_main_activity_from_files(screen_files if screens else {})
        files["app/src/main/AndroidManifest.xml"] = self._generate_manifest()
        files.update(self._generate_res_files())

        return files

    def verify_structure(self, project_id: str, version_id: str) -> list[str]:
        errors: list[str] = []
        return errors

    def _generate_gradle_files(self, design_data: dict[str, Any]) -> dict[str, str]:
        build_gradle = '''plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.mdesigner.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.mdesigner.app"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
    }

    buildFeatures {
        compose = true
    }

    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.10"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2024.02.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.activity:activity-compose:1.8.2")
    implementation("androidx.navigation:navigation-compose:2.7.7")
    debugImplementation("androidx.compose.ui:ui-tooling")
}
'''

        root_build_gradle = '''plugins {
    id("com.android.application") version "8.2.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.22" apply false
}
'''

        settings_gradle = '''pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "MDesignerApp"
include(":app")
'''

        gradle_properties = '''android.useAndroidX=true
android.nonTransitiveRClass=true
kotlin.code.style=official
org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8
'''

        gradle_wrapper = '''distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
distributionUrl=https\\://services.gradle.org/distributions/gradle-8.5-bin.zip
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
'''

        return {
            "build.gradle.kts": root_build_gradle,
            "app/build.gradle.kts": build_gradle,
            "settings.gradle.kts": settings_gradle,
            "gradle.properties": gradle_properties,
            "gradle/wrapper/gradle-wrapper.properties": gradle_wrapper,
        }

    def gradle_wrapper_files(self) -> list[tuple[str, bytes, bool]]:
        """Gradle wrapper artifacts for the ZIP: (path, bytes, is_executable).

        gradlew/gradlew.bat and the wrapper jar are shipped verbatim from the
        bundled Gradle 8.5 distribution so Android Studio can bootstrap Gradle
        without a manual `gradle wrapper` invocation. gradlew needs the exec bit.
        """
        res = Path(__file__).parent / "resources"
        return [
            (GRADLE_WRAPPER_JAR_ENTRY, (res / "gradle-wrapper.jar").read_bytes(), False),
            ("gradlew", (res / "gradlew").read_bytes(), True),
            ("gradlew.bat", (res / "gradlew.bat").read_bytes(), False),
        ]

    def _generate_main_activity(self, screens: list[dict[str, Any]]) -> str:
        screen_imports = ""
        nav_routes = ""

        from src.handoff.code_generator.compose_spec import to_pascal_case

        seen_names: set[str] = set()
        for i, screen in enumerate(screens):
            name = to_pascal_case(screen.get("name", "Main"), i)
            if name in seen_names:
                name = f"{name}{i + 1}"
            seen_names.add(name)
            screen_imports += f"import com.mdesigner.app.ui.screens.{name}Screen\n"
            route = name.lower()
            nav_routes += f'            composable("{route}") {{ {name}Screen(onNavigate = {{ navController.navigate(it) }}) }}\n'

        if not screens:
            screen_imports = "import com.mdesigner.app.ui.screens.MainScreen\n"
            nav_routes = '            composable("main") { MainScreen() }\n'

        return f'''package com.mdesigner.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.mdesigner.app.ui.theme.MDesignerTheme
{screen_imports}

class MainActivity : ComponentActivity() {{
    override fun onCreate(savedInstanceState: Bundle?) {{
        super.onCreate(savedInstanceState)
        setContent {{
            MDesignerTheme {{
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background
                ) {{
                    val navController = rememberNavController()
                    NavHost(navController = navController, startDestination = "{to_pascal_case(screens[0].get('name', 'Main'), 0).lower() if screens else 'main'}") {{
{nav_routes}                    }}
                }}
            }}
        }}
    }}
}}
'''

    def _generate_main_activity_from_files(self, screen_files: dict[str, str]) -> str:
        """Generate MainActivity from actual LLM-generated screen files."""

        screen_names: list[str] = []
        for file_path in sorted(screen_files.keys()):
            if "/screens/" in file_path and file_path.endswith("Screen.kt"):
                filename = file_path.rsplit("/", 1)[-1]
                name = filename.replace("Screen.kt", "")
                screen_names.append(name)

        if not screen_names:
            return self._generate_main_activity([])

        screen_imports = ""
        nav_routes = ""
        for name in screen_names:
            screen_imports += f"import com.mdesigner.app.ui.screens.{name}Screen\n"
            route = name.lower()
            nav_routes += f'            composable("{route}") {{ {name}Screen(onNavigate = {{ navController.navigate(it) }}) }}\n'

        start_dest = screen_names[0].lower()

        return f'''package com.mdesigner.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.mdesigner.app.ui.theme.MDesignerTheme
{screen_imports}

class MainActivity : ComponentActivity() {{
    override fun onCreate(savedInstanceState: Bundle?) {{
        super.onCreate(savedInstanceState)
        setContent {{
            MDesignerTheme {{
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background
                ) {{
                    val navController = rememberNavController()
                    NavHost(navController = navController, startDestination = "{start_dest}") {{
{nav_routes}                    }}
                }}
            }}
        }}
    }}
}}
'''

    def _generate_manifest(self) -> str:
        return '''<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">

    <application
        android:allowBackup="true"
        android:label="Mobile Designer App"
        android:supportsRtl="true"
        android:theme="@style/Theme.MDesigner">
        <activity
            android:name=".MainActivity"
            android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>

</manifest>
'''

    def _generate_res_files(self) -> dict[str, str]:
        themes_xml = '''<?xml version="1.0" encoding="utf-8"?>
<resources>
    <style name="Theme.MDesigner" parent="android:Theme.Material.Light.NoActionBar">
        <item name="android:statusBarColor">@android:color/transparent</item>
        <item name="android:navigationBarColor">@android:color/transparent</item>
    </style>
</resources>
'''
        return {
            "app/src/main/res/values/themes.xml": themes_xml,
        }
