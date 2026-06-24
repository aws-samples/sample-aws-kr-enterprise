import re
from typing import Any

import structlog

logger = structlog.get_logger()


def escape_kotlin_string(text: str) -> str:
    """Escape text for use in Kotlin string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "").replace("\t", "\\t")


def to_pascal_case(name: str, index: int = 0) -> str:
    """Convert screen name to valid Kotlin identifier (ASCII only)."""
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", name)
    if not cleaned:
        return f"Screen{index + 1}" if index > 0 else "Main"
    if cleaned[0].isdigit():
        cleaned = "Screen" + cleaned
    return cleaned[0].upper() + cleaned[1:]


def dp_val(value: Any) -> str:
    s = str(value).replace("dp", "").replace("px", "").replace("sp", "")
    try:
        return f"{int(float(s))}.dp"
    except (ValueError, TypeError):
        return "0.dp"


def sp_val(value: Any) -> str:
    s = str(value).replace("sp", "").replace("dp", "").replace("px", "")
    try:
        return f"{int(float(s))}.sp"
    except (ValueError, TypeError):
        return "16.sp"


def hex_to_compose_color(hex_val: str) -> str:
    """Convert #RRGGBB or #RRGGBBAA to Color(0xAARRGGBB)."""
    h = hex_val.strip().lstrip("#")
    if len(h) == 6:
        return f"Color(0xFF{h.upper()})"
    elif len(h) == 8:
        return f"Color(0x{h[6:].upper()}{h[:6].upper()})"
    return f"Color(0xFF{h.upper()})"


ICON_MAP = {
    "home": "Home", "menu": "Menu", "arrow_back": "ArrowBack", "close": "Close",
    "search": "Search", "settings": "Settings", "delete": "Delete", "done": "Done",
    "add": "Add", "edit": "Edit", "share": "Share", "more_vert": "MoreVert",
    "notifications": "Notifications", "person": "Person", "star": "Star",
    "favorite": "Favorite", "info": "Info", "dashboard": "Dashboard",
    "tune": "Tune", "filter_list": "FilterList", "sort": "Sort",
    "refresh": "Refresh", "visibility": "Visibility", "lock": "Lock",
    "email": "Email", "phone": "Phone", "send": "Send", "chat": "Chat",
    "place": "Place", "calendar_today": "CalendarToday", "event": "Event",
    "smart_toy": "SmartToy", "business": "Business", "analytics": "Analytics",
    "trending_up": "TrendingUp", "bar_chart": "BarChart",
}


def icon_ref(icon_name: str) -> str:
    kotlin_name = ICON_MAP.get(icon_name, "MoreVert")
    return f"Icons.Outlined.{kotlin_name}"


class ComposeSpecMapper:
    def map_screen(self, screen: dict[str, Any]) -> dict[str, str]:
        name = to_pascal_case(screen.get("name", "Main"))
        components = screen.get("components", [])

        compose_code = self._generate_screen(name, components)
        file_path = f"app/src/main/java/com/mdesigner/app/ui/screens/{name}Screen.kt"

        return {file_path: compose_code}

    def _generate_screen(self, name: str, components: list[dict[str, Any]]) -> str:
        # Check if top-level is Scaffold
        scaffold = None
        if components and components[0].get("type") == "Scaffold":
            scaffold = components[0]

        if scaffold:
            return self._generate_scaffold_screen(name, scaffold)
        else:
            body = self._render_children(components, 12)
            return self._wrap_screen(name, body)

    def _generate_scaffold_screen(self, name: str, scaffold: dict[str, Any]) -> str:
        children = scaffold.get("children", [])

        top_bar = None
        bottom_nav = None
        fab = None
        body_components = []

        for child in children:
            t = child.get("type", "")
            if t in ("TopAppBar", "LargeTopAppBar"):
                top_bar = child
            elif t == "BottomNavigation":
                bottom_nav = child
            elif t in ("FAB", "FloatingActionButton", "ExtendedFloatingActionButton"):
                fab = child
            else:
                body_components.append(child)

        # Generate top bar code
        top_bar_code = ""
        if top_bar:
            top_bar_code = self._render_top_bar(top_bar)

        # Generate bottom bar code
        bottom_bar_code = ""
        if bottom_nav:
            bottom_bar_code = self._render_bottom_nav(bottom_nav)

        # Generate FAB code
        fab_code = ""
        if fab:
            fab_code = self._render_fab(fab)

        # Generate body
        body = self._render_children(body_components, 16)

        scaffold_params = []
        if top_bar_code:
            scaffold_params.append(f"        topBar = {{\n{top_bar_code}\n        }}")
        if bottom_bar_code:
            scaffold_params.append(f"        bottomBar = {{\n{bottom_bar_code}\n        }}")
        if fab_code:
            scaffold_params.append(f"        floatingActionButton = {{\n{fab_code}\n        }}")

        params_str = ",\n".join(scaffold_params)
        if params_str:
            params_str = ",\n" + params_str

        return f'''package com.mdesigner.app.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mdesigner.app.ui.theme.AppColors

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun {name}Screen(onNavigate: (String) -> Unit = {{}}) {{
    Scaffold(
        modifier = Modifier.fillMaxSize(){params_str}
    ) {{ innerPadding ->
        LazyColumn(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
                .padding(horizontal = 24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {{
{body}
        }}
    }}
}}
'''

    def _wrap_screen(self, name: str, body: str) -> str:
        return f'''package com.mdesigner.app.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mdesigner.app.ui.theme.AppColors

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun {name}Screen(onNavigate: (String) -> Unit = {{}}) {{
    Scaffold(modifier = Modifier.fillMaxSize()) {{ innerPadding ->
        LazyColumn(
            modifier = Modifier.fillMaxSize().padding(innerPadding).padding(horizontal = 24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {{
{body}
        }}
    }}
}}
'''

    def _render_top_bar(self, comp: dict[str, Any]) -> str:
        props = comp.get("props", {})
        title = escape_kotlin_string(props.get("title", ""))
        actions = props.get("actions", [])
        nav_icon = props.get("navigationIcon", "")

        actions_code = ""
        if actions and isinstance(actions, list):
            icons = "\n".join([f"                IconButton(onClick = {{}}) {{ Icon({icon_ref(a)}, contentDescription = \"{a}\") }}" for a in actions[:3]])
            actions_code = f''',
            actions = {{
{icons}
            }}'''

        nav_code = ""
        if nav_icon:
            nav_code = f''',
            navigationIcon = {{ IconButton(onClick = {{}}) {{ Icon({icon_ref(nav_icon)}, contentDescription = "{nav_icon}") }} }}'''

        return f'''            LargeTopAppBar(
                title = {{
                    Text(
                        "{title}",
                        fontWeight = FontWeight.Light,
                        fontSize = 28.sp
                    )
                }},
                colors = TopAppBarDefaults.largeTopAppBarColors(
                    containerColor = AppColors.surface,
                    scrolledContainerColor = AppColors.surface
                ){nav_code}{actions_code}
            )'''

    def _render_bottom_nav(self, comp: dict[str, Any]) -> str:
        items = comp.get("children", [])
        items_code = []
        for i, item in enumerate(items):
            props = item.get("props", {})
            label = escape_kotlin_string(props.get("label", "Tab"))
            icon = props.get("icon", "home")
            navigate_to = escape_kotlin_string(props.get("navigate_to", ""))
            route = to_pascal_case(navigate_to).lower() if navigate_to else ""
            selected = "true" if i == 0 else "false"
            on_click = f'{{ onNavigate("{route}") }}' if route else "{}"
            items_code.append(f'''                NavigationBarItem(
                    selected = {selected},
                    onClick = {on_click},
                    icon = {{ Icon({icon_ref(icon)}, contentDescription = "{label}") }},
                    label = {{ Text("{label}") }}
                )''')
        items_str = "\n".join(items_code)
        return f'''            NavigationBar {{
{items_str}
            }}'''

    def _render_fab(self, comp: dict[str, Any]) -> str:
        props = comp.get("props", {})
        text = escape_kotlin_string(props.get("text", props.get("contentDescription", "")))
        icon = props.get("icon", "add")
        style = comp.get("style", {})
        bg = style.get("backgroundColor", "")

        color_param = f", containerColor = {hex_to_compose_color(bg)}" if bg else ""

        if text:
            return f'''            ExtendedFloatingActionButton(
                onClick = {{}}{color_param},
                icon = {{ Icon({icon_ref(icon)}, contentDescription = "{text}") }},
                text = {{ Text("{text}") }}
            )'''
        return f'''            FloatingActionButton(onClick = {{}}{color_param}) {{
                Icon({icon_ref(icon)}, contentDescription = "Add")
            }}'''

    def _render_children(self, components: list[dict[str, Any]], indent: int) -> str:
        lines = []
        pad = " " * indent
        for comp in components:
            code = self._render_component(comp, indent)
            if code:
                lines.append(f"{pad}item {{")
                lines.append(code)
                lines.append(f"{pad}}}")
        return "\n".join(lines)

    def _render_component(self, comp: dict[str, Any], indent: int) -> str:
        pad = " " * (indent + 4)
        comp_type = comp.get("type", "")
        props = {k: escape_kotlin_string(str(v)) if isinstance(v, str) else v for k, v in comp.get("props", {}).items()}
        style = comp.get("style", {})
        children = comp.get("children", [])

        match comp_type:
            case "Row":
                child_lines = []
                for ch in children:
                    child_code = self._render_inline(ch, indent + 8)
                    # Add weight to cards in a row
                    if ch.get("type") == "Card":
                        child_lines.append(self._render_card_weighted(ch, indent + 8))
                    else:
                        child_lines.append(child_code)
                children_str = "\n".join(child_lines)
                return f'''{pad}Row(
{pad}    modifier = Modifier.fillMaxWidth(),
{pad}    horizontalArrangement = Arrangement.spacedBy(12.dp)
{pad}) {{
{children_str}
{pad}}}'''

            case "Column":
                child_lines = [self._render_inline(ch, indent + 8) for ch in children]
                children_str = "\n".join(child_lines)
                return f'''{pad}Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {{
{children_str}
{pad}}}'''

            case "Card":
                return self._render_card(comp, indent + 4)

            case "LazyColumn":
                child_lines = [self._render_inline(ch, indent + 8) for ch in children]
                children_str = "\n".join(child_lines)
                return f'''{pad}Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {{
{children_str}
{pad}}}'''

            case "Text":
                return self._render_text(comp, indent + 4)

            case "ListItem":
                return self._render_list_item(comp, indent + 4)

            case "Divider" | "HorizontalDivider":
                return f"{pad}HorizontalDivider()"

            case "Spacer":
                h = dp_val(style.get("height", "16dp"))
                return f"{pad}Spacer(modifier = Modifier.height({h}))"

            case "TextButton":
                text = props.get("text", props.get("label", ""))
                return f'{pad}TextButton(onClick = {{}}) {{ Text("{text}", color = AppColors.primary) }}'

            case "Surface":
                child_lines = [self._render_inline(ch, indent + 8) for ch in children]
                children_str = "\n".join(child_lines)
                radius = dp_val(style.get("cornerRadius", "12dp"))
                return f'''{pad}Surface(shape = RoundedCornerShape({radius}), tonalElevation = 1.dp) {{
{pad}    Column {{
{children_str}
{pad}    }}
{pad}}}'''

            case "TabRow":
                tabs = []
                for i, ch in enumerate(children):
                    label = escape_kotlin_string(ch.get("props", {}).get("label", f"Tab {i+1}"))
                    selected = "true" if i == 0 else "false"
                    tabs.append(f'{pad}    Tab(selected = {selected}, onClick = {{}}, text = {{ Text("{label}") }})')
                tabs_str = "\n".join(tabs)
                return f'''{pad}TabRow(selectedTabIndex = 0) {{
{tabs_str}
{pad}}}'''

            case "Chip" | "FilterChip" | "AssistChip":
                label = props.get("label", props.get("text", "Chip"))
                return f'{pad}AssistChip(onClick = {{}}, label = {{ Text("{label}") }})'

            case "Icon":
                icon = props.get("icon", props.get("name", "info"))
                return f'{pad}Icon({icon_ref(icon)}, contentDescription = "{icon}")'

            case "Box":
                text = props.get("text", "")
                if text:
                    bg = style.get("backgroundColor", "#F5F5F5")
                    return f'''{pad}Box(modifier = Modifier.clip(RoundedCornerShape(8.dp)).background({hex_to_compose_color(bg)}).padding(horizontal = 8.dp, vertical = 4.dp)) {{
{pad}    Text("{text}", fontSize = 12.sp)
{pad}}}'''
                child_lines = [self._render_inline(ch, indent + 8) for ch in children]
                return f'''{pad}Box {{
{chr(10).join(child_lines)}
{pad}}}'''

            case _:
                logger.warning("compose_unknown_component_type", comp_type=comp_type)
                text = props.get("text", props.get("title", ""))
                if text:
                    return f'{pad}Text("{text}")'
                if children:
                    child_lines = [self._render_inline(ch, indent + 8) for ch in children]
                    return f'''{pad}Column {{
{chr(10).join(child_lines)}
{pad}}}'''
                return f"{pad}// Unsupported component type: {comp_type}"

    def _render_inline(self, comp: dict[str, Any], indent: int) -> str:
        """Render a component inline (without item {} wrapper)."""
        return self._render_component(comp, indent - 4)

    def _render_card(self, comp: dict[str, Any], indent: int) -> str:
        pad = " " * indent
        style = comp.get("style", {})
        children = comp.get("children", [])
        radius = dp_val(style.get("cornerRadius", "16dp"))
        bg = style.get("backgroundColor", "#FFFFFF")

        child_lines = [self._render_inline(ch, indent + 8) for ch in children]
        children_str = "\n".join(child_lines)

        return f'''{pad}Card(
{pad}    colors = CardDefaults.cardColors(containerColor = {hex_to_compose_color(bg)}),
{pad}    shape = RoundedCornerShape({radius}),
{pad}    elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
{pad}    modifier = Modifier.fillMaxWidth()
{pad}) {{
{pad}    Column(modifier = Modifier.padding(16.dp)) {{
{children_str}
{pad}    }}
{pad}}}'''

    def _render_card_weighted(self, comp: dict[str, Any], indent: int) -> str:
        pad = " " * indent
        style = comp.get("style", {})
        children = comp.get("children", [])
        radius = dp_val(style.get("cornerRadius", "16dp"))
        bg = style.get("backgroundColor", "#FFFFFF")

        child_lines = [self._render_inline(ch, indent + 8) for ch in children]
        children_str = "\n".join(child_lines)

        return f'''{pad}Card(
{pad}    colors = CardDefaults.cardColors(containerColor = {hex_to_compose_color(bg)}),
{pad}    shape = RoundedCornerShape({radius}),
{pad}    elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
{pad}    modifier = Modifier.weight(1f)
{pad}) {{
{pad}    Column(modifier = Modifier.padding(12.dp)) {{
{children_str}
{pad}    }}
{pad}}}'''

    def _render_text(self, comp: dict[str, Any], indent: int) -> str:
        pad = " " * indent
        props = {k: escape_kotlin_string(str(v)) if isinstance(v, str) else v for k, v in comp.get("props", {}).items()}
        style = comp.get("style", {})
        text = props.get("text", "")

        params = []
        font_size = style.get("fontSize", "")
        if font_size:
            params.append(f"fontSize = {sp_val(font_size)}")
        font_weight = style.get("fontWeight", "")
        if font_weight in ("Bold", "SemiBold", "Medium"):
            params.append(f"fontWeight = FontWeight.{font_weight}")
        color = style.get("color", "")
        if color and color not in ("#000000", "#1C1B1F"):
            params.append(f"color = {hex_to_compose_color(color)}")

        params_str = ", ".join(params)
        if params_str:
            return f'{pad}Text("{text}", {params_str})'
        return f'{pad}Text("{text}")'

    def _render_list_item(self, comp: dict[str, Any], indent: int) -> str:
        pad = " " * indent
        props = {k: escape_kotlin_string(str(v)) if isinstance(v, str) else v for k, v in comp.get("props", {}).items()}
        headline = props.get("headlineText", props.get("title", props.get("text", "Item")))
        supporting = props.get("supportingText", props.get("subtitle", ""))

        supporting_code = f',\n{pad}    supportingContent = {{ Text("{supporting}") }}' if supporting else ""

        return f'''{pad}ListItem(
{pad}    headlineContent = {{ Text("{headline}") }}{supporting_code}
{pad})'''
