from typing import Any

import structlog

logger = structlog.get_logger()


class ThemeGenerator:
    def generate(self, tokens: dict[str, Any]) -> dict[str, str]:
        files: dict[str, str] = {}

        files["app/src/main/java/com/mdesigner/app/ui/theme/Color.kt"] = self._generate_color(tokens)
        files["app/src/main/java/com/mdesigner/app/ui/theme/Type.kt"] = self._generate_type(tokens)
        files["app/src/main/java/com/mdesigner/app/ui/theme/Theme.kt"] = self._generate_theme(tokens)
        files["app/src/main/java/com/mdesigner/app/ui/theme/AppTokens.kt"] = self._generate_tokens(tokens)

        return files

    def _hex_to_compose(self, hex_val: str) -> str:
        """Convert #RRGGBB or #RRGGBBAA to 0xAARRGGBB format."""
        hex_val = hex_val.strip().lstrip("#")
        if len(hex_val) == 6:
            return f"0xFF{hex_val.upper()}"
        elif len(hex_val) == 8:
            # CSS format is #RRGGBBAA, Compose needs 0xAARRGGBB
            rgb = hex_val[:6]
            alpha = hex_val[6:]
            return f"0x{alpha.upper()}{rgb.upper()}"
        return f"0xFF{hex_val.upper()}"

    def _generate_color(self, tokens: dict[str, Any]) -> str:
        colors = tokens.get("colors", {})

        lines: list[str] = []
        for name, value in colors.items():
            if isinstance(value, str) and value.startswith("#"):
                compose_name = name[0].lower() + name[1:]
                compose_val = self._hex_to_compose(value)
                lines.append(f"    val {compose_name} = Color({compose_val})")

        if not lines:
            lines = [
                "    val primary = Color(0xFF0381FE)",
                "    val onPrimary = Color(0xFFFFFFFF)",
                "    val primaryContainer = Color(0xFFC2E7FF)",
                "    val surface = Color(0xFFFFFFFF)",
                "    val onSurface = Color(0xFF1C1B1F)",
                "    val background = Color(0xFFFAFAFA)",
                "    val error = Color(0xFFD93025)",
                "    val onError = Color(0xFFFFFFFF)",
            ]

        colors_str = "\n".join(lines)

        return f'''package com.mdesigner.app.ui.theme

import androidx.compose.ui.graphics.Color

object AppColors {{
{colors_str}
}}
'''

    def _generate_type(self, tokens: dict[str, Any]) -> str:
        typography = tokens.get("typography", {})

        type_entries: list[str] = []

        type_map = {
            "extendTitle": ("displayLarge", "34", "Light"),
            "dialogTitle": ("headlineMedium", "20", "Medium"),
            "title": ("titleLarge", "19", "Normal"),
            "mainList": ("bodyLarge", "18", "Normal"),
            "textButton": ("labelLarge", "17", "Normal"),
            "body": ("bodyMedium", "16", "Normal"),
            "raisedButton": ("labelMedium", "15", "Normal"),
            "subHeader": ("titleSmall", "14", "Medium"),
            "subList": ("bodySmall", "13", "Normal"),
        }

        for token_key, (role, default_size, default_weight) in type_map.items():
            type_data = typography.get(token_key, {})
            if isinstance(type_data, dict):
                size = type_data.get("fontSize", f"{default_size}sp").replace("sp", "")
                weight_str = type_data.get("fontWeight", default_weight)
            else:
                size = default_size
                weight_str = default_weight

            weight_map = {
                "Light": "FontWeight.Light",
                "Normal": "FontWeight.Normal",
                "Regular": "FontWeight.Normal",
                "Medium": "FontWeight.Medium",
                "SemiBold": "FontWeight.SemiBold",
                "Bold": "FontWeight.Bold",
            }
            weight = weight_map.get(weight_str, "FontWeight.Normal")

            type_entries.append(f"""    {role} = TextStyle(
        fontSize = {size}.sp,
        fontWeight = {weight},
        lineHeight = {int(float(size)) + 6}.sp,
    )""")

        type_str = ",\n".join(type_entries)

        return f'''package com.mdesigner.app.ui.theme

import androidx.compose.material3.Typography
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

val AppTypography = Typography(
{type_str}
)
'''

    def _generate_tokens(self, tokens: dict[str, Any]) -> str:
        spacing = tokens.get("spacing", {})
        shapes = tokens.get("shapes", {})
        elevation = tokens.get("elevation", {})
        animation = tokens.get("animation", {})

        spacing_lines: list[str] = []
        for name, value in spacing.items():
            dp_val = str(value).replace("dp", "").replace("px", "")
            try:
                float(dp_val)
                spacing_lines.append(f"    val {name} = {dp_val}.dp")
            except ValueError:
                logger.warning("theme_token_skipped", token_type="spacing", name=name, value=value)

        shape_lines: list[str] = []
        for name, value in shapes.items():
            if isinstance(value, dict):
                radius = str(value.get("cornerRadius", "12")).replace("dp", "")
            else:
                radius = str(value).replace("dp", "")
            try:
                float(radius)
                shape_lines.append(f"    val {name} = RoundedCornerShape({radius}.dp)")
            except ValueError:
                logger.warning("theme_token_skipped", token_type="shape", name=name, value=value)

        elevation_lines: list[str] = []
        for name, value in elevation.items():
            if isinstance(value, dict):
                dp_val = str(value.get("elevation", "0")).replace("dp", "")
            else:
                dp_val = str(value).replace("dp", "")
            try:
                float(dp_val)
                elevation_lines.append(f"    val {name} = {dp_val}.dp")
            except ValueError:
                logger.warning("theme_token_skipped", token_type="elevation", name=name, value=value)

        motion_lines: list[str] = []
        if animation:
            easing = animation.get("easing", {})
            if isinstance(easing, dict):
                standard = easing.get("standard", "")
                if standard:
                    motion_lines.append(f'    val standardEasing = "{standard}"')
            durations = animation.get("duration", animation.get("durations", {}))
            if isinstance(durations, dict):
                for name, value in durations.items():
                    ms_val = str(value).replace("ms", "")
                    try:
                        int(ms_val)
                        motion_lines.append(f"    val duration{name[0].upper()}{name[1:]} = {ms_val}")
                    except ValueError:
                        logger.warning("theme_token_skipped", token_type="duration", name=name, value=value)

        sections: list[str] = []
        if spacing_lines:
            sections.append("    // Spacing\n" + "\n".join(spacing_lines))
        if shape_lines:
            sections.append("    // Shapes\n" + "\n".join(shape_lines))
        if elevation_lines:
            sections.append("    // Elevation\n" + "\n".join(elevation_lines))
        if motion_lines:
            sections.append("    // Motion\n" + "\n".join(motion_lines))

        if not sections:
            sections = [
                "    val screenMargin = 24.dp",
                "    val cardGap = 12.dp",
                "    val sectionGap = 32.dp",
                "    val touchTarget = 48.dp",
            ]

        body = "\n\n".join(sections)

        return f'''package com.mdesigner.app.ui.theme

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.unit.dp

object AppTokens {{
{body}
}}
'''

    def _generate_theme(self, tokens: dict[str, Any]) -> str:
        colors = tokens.get("colors", {})

        # Default Material3 fallback colors
        defaults = {
            "primary": "0xFF0381FE",
            "onPrimary": "0xFFFFFFFF",
            "primaryContainer": "0xFFC2E7FF",
            "secondary": "0xFF0381FE",
            "surface": "0xFFFFFFFF",
            "onSurface": "0xFF000000",
            "background": "0xFFFAFAFA",
            "error": "0xFFD93025",
            "onError": "0xFFFFFFFF",
            "outline": "0xFFE0E0E0",
        }

        def color_ref(key: str) -> str:
            if key in colors:
                compose_name = key[0].lower() + key[1:]
                return f"AppColors.{compose_name}"
            return f"Color({defaults.get(key, '0xFF000000')})"

        primary_ref = color_ref("primary")
        on_primary_ref = color_ref("onPrimary")
        primary_container_ref = color_ref("primaryContainer")
        color_ref("secondary")
        surface_ref = color_ref("surface")
        on_surface_ref = color_ref("onSurface")
        background_ref = color_ref("background")
        error_ref = color_ref("error")
        on_error_ref = color_ref("onError")
        color_ref("outline")
        text_secondary_ref = color_ref("textSecondary")
        divider_ref = color_ref("divider")

        return f'''package com.mdesigner.app.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val LightColorScheme = lightColorScheme(
    primary = {primary_ref},
    onPrimary = {on_primary_ref},
    primaryContainer = {primary_container_ref},
    onPrimaryContainer = {primary_ref},
    secondary = {primary_ref},
    onSecondary = {on_primary_ref},
    secondaryContainer = Color.Transparent,
    onSecondaryContainer = {primary_ref},
    tertiary = {primary_ref},
    onTertiary = {on_primary_ref},
    tertiaryContainer = Color.Transparent,
    onTertiaryContainer = {primary_ref},
    surface = {surface_ref},
    onSurface = {on_surface_ref},
    surfaceVariant = {surface_ref},
    onSurfaceVariant = {text_secondary_ref},
    surfaceTint = Color.Transparent,
    inverseSurface = Color(0xFF303030),
    inverseOnSurface = Color(0xFFF5F5F5),
    inversePrimary = {primary_ref},
    background = {background_ref},
    onBackground = {on_surface_ref},
    error = {error_ref},
    onError = {on_error_ref},
    errorContainer = Color(0xFFFCE4EC),
    onErrorContainer = Color(0xFF410002),
    outline = {divider_ref},
    outlineVariant = {divider_ref},
    scrim = Color(0xFF000000),
    surfaceContainerHighest = {surface_ref},
    surfaceContainerHigh = {surface_ref},
    surfaceContainer = {surface_ref},
    surfaceContainerLow = {surface_ref},
    surfaceContainerLowest = {surface_ref},
    surfaceBright = {surface_ref},
    surfaceDim = {background_ref},
)

@Composable
fun MDesignerTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit
) {{
    MaterialTheme(
        colorScheme = LightColorScheme,
        typography = AppTypography,
        content = content,
    )
}}
'''
