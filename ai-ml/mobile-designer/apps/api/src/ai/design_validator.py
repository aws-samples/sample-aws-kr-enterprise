from typing import Any

import structlog

logger = structlog.get_logger()

VALID_COMPONENT_TYPES = {
    "TopAppBar", "BottomNavigation", "FloatingActionButton", "Button", "IconButton",
    "TextField", "OutlinedTextField", "Card", "ElevatedCard", "OutlinedCard",
    "ListItem", "Checkbox", "RadioButton", "Switch", "Slider",
    "ProgressIndicator", "CircularProgressIndicator", "Dialog", "AlertDialog",
    "BottomSheet", "Snackbar", "Chip", "FilterChip", "Badge", "Divider",
    "Tab", "TabRow", "NavigationDrawer", "DropdownMenu", "Scaffold",
    "Column", "Row", "LazyColumn", "LazyRow", "Box", "Spacer", "Surface",
    "Image", "Icon",
}

MIN_MARGIN_DP = 24
MIN_TEXT_SIZE_SP = 12
MAX_TEXT_SIZE_SP = 34


class DesignValidator:
    def validate(self, design_data: dict[str, Any], stage: str) -> dict[str, Any]:
        violations: list[dict[str, str]] = []

        components = design_data.get("components", [])
        if not components:
            return {"valid": True, "violations": []}

        for component in components:
            self._validate_component_type(component, violations)
            self._validate_margins(component, violations)
            self._validate_text_size(component, violations)

        self._validate_action_placement(components, violations)
        self._validate_extend_title(components, stage, violations)

        valid = len(violations) == 0
        if not valid:
            logger.warning("design_validation_failed", violation_count=len(violations), stage=stage)

        return {"valid": valid, "violations": violations}

    def _validate_component_type(self, component: dict[str, Any], violations: list[dict[str, str]]) -> None:
        comp_type = component.get("type", "")
        if comp_type and comp_type not in VALID_COMPONENT_TYPES:
            violations.append({
                "rule": "COMPONENT_VALIDITY",
                "component_id": component.get("id", "unknown"),
                "message": f"Invalid component type: {comp_type}. Must be one of the approved Compose components.",
            })

    def _validate_margins(self, component: dict[str, Any], violations: list[dict[str, str]]) -> None:
        style = component.get("style", {})
        for prop in ("marginStart", "marginEnd", "paddingStart", "paddingEnd"):
            value = style.get(prop)
            if value is not None and isinstance(value, int | float) and value > 0 and value < MIN_MARGIN_DP:
                violations.append({
                    "rule": "MARGIN_MIN_24DP",
                    "component_id": component.get("id", "unknown"),
                    "message": f"{prop}={value}dp is less than the minimum {MIN_MARGIN_DP}dp.",
                })

    def _validate_text_size(self, component: dict[str, Any], violations: list[dict[str, str]]) -> None:
        style = component.get("style", {})
        font_size = style.get("fontSize")
        if font_size is not None and isinstance(font_size, int | float):
            if font_size < MIN_TEXT_SIZE_SP:
                violations.append({
                    "rule": "TEXT_SIZE_MIN",
                    "component_id": component.get("id", "unknown"),
                    "message": f"fontSize={font_size}sp is below minimum {MIN_TEXT_SIZE_SP}sp.",
                })
            if font_size > MAX_TEXT_SIZE_SP:
                violations.append({
                    "rule": "TEXT_SIZE_MAX",
                    "component_id": component.get("id", "unknown"),
                    "message": f"fontSize={font_size}sp exceeds maximum {MAX_TEXT_SIZE_SP}sp.",
                })

    def _validate_action_placement(self, components: list[dict[str, Any]], violations: list[dict[str, str]]) -> None:
        action_types = {"Button", "FloatingActionButton", "IconButton"}
        screen_height_bottom_threshold = 0.7

        for comp in components:
            if comp.get("type") in action_types:
                position = comp.get("style", {}).get("verticalPosition")
                if (
                    position is not None
                    and isinstance(position, int | float)
                    and position < screen_height_bottom_threshold
                ):
                    violations.append({
                        "rule": "ACTION_BOTTOM_PLACEMENT",
                        "component_id": comp.get("id", "unknown"),
                        "message": (
                            "Primary action buttons should be placed in the bottom area "
                            "of the screen (ergonomic guideline)."
                        ),
                    })

    def _validate_extend_title(
        self, components: list[dict[str, Any]], stage: str, violations: list[dict[str, str]]
    ) -> None:
        if stage == "wireframe":
            return

        has_top_app_bar = any(c.get("type") == "TopAppBar" for c in components)
        if not has_top_app_bar:
            return

        top_bars = [c for c in components if c.get("type") == "TopAppBar"]
        for bar in top_bars:
            props = bar.get("props", {})
            title_style = props.get("titleStyle", "")
            if title_style and "extendTitle" not in title_style.lower() and "extend" not in title_style.lower():
                pass
