"""LLM-based Kotlin Compose screen code generator.

Replaces ComposeSpecMapper: takes design JSON per screen and generates
buildable .kt files via Claude Sonnet.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import structlog
from strands import Agent
from strands.models.bedrock import BedrockModel

from src.admin.config_service import get_model_id
from src.prompts.loader import get_prompt_loader
from src.prompts.slots import SCREEN_CODEGEN

logger = structlog.get_logger()

SCREEN_CODEGEN_PROMPT = """You are a Kotlin Compose code generator for modern Android apps.

Given a screen's component tree (JSON) and design tokens, produce a SINGLE compilable Kotlin file.

## GOLDEN RULE: Design JSON + Tokens = Single Source of Truth

ALL visual properties MUST come from the JSON `style` object or the design tokens. NEVER hardcode color hex values unless they come directly from the provided JSON/tokens. NEVER rely on Material3 theme defaults.

Color resolution priority:
1. Component's `style` object (e.g., `style.backgroundColor`, `style.color`)
2. Design tokens (e.g., `tokens.colors.primary`, `tokens.colors.surface`)
3. If truly nothing is specified → use `tokens.colors.surface` for backgrounds, `tokens.colors.text` for text

## CRITICAL: Explicit Colors on Every Component

Material3 defaults produce wrong colors. Every composable MUST have explicit color parameters derived from the JSON:

- **Scaffold**: `containerColor` = from `tokens.colors.background`
- **TopAppBar / LargeTopAppBar**: `containerColor` and `scrolledContainerColor` = from `tokens.colors.surface` or component style
- **NavigationBar**: `containerColor` = from BottomNavigation style.backgroundColor, or `tokens.colors.surface`
- **NavigationBarItem**: `selectedIconColor` / `selectedTextColor` = from BottomNavigation style.selectedColor or `tokens.colors.primary`; `unselectedIconColor` / `unselectedTextColor` = from style.unselectedColor or `tokens.colors.textSecondary`; `indicatorColor = Color.Transparent`
- **Card**: `containerColor` = from style.backgroundColor or `tokens.colors.surface`
- **Button / FAB**: `containerColor` = from style.backgroundColor or `tokens.colors.primary`
- **TextButton**: `contentColor` = from style.color or `tokens.colors.primary`
- **Surface**: `color` = from style.backgroundColor
- **TabRow**: `containerColor` = from style or `tokens.colors.surface`
- **HorizontalDivider**: `color` = from `tokens.colors.divider`
- **FilterChip / AssistChip**: colors from style or tokens

## NavigationBar Rules

1. `selected` state: Read EACH BottomNavigationItem's `props.selected` from JSON. Do NOT assume the first tab is selected.
2. Container color: Read from BottomNavigation `style.backgroundColor`. If absent, use `tokens.colors.surface`.
3. Selected/unselected colors: Read from style. If absent, selected = `tokens.colors.primary`, unselected = `tokens.colors.textSecondary`.

## NAMING RULE (ABSOLUTE)

ALL identifiers (file names, class names, function names, variable names, parameters) MUST be English-only ASCII.
NEVER use Korean or any non-ASCII characters in identifiers. Korean is ONLY allowed inside string literals (Text("한글"), contentDescription = "한글").

## Structure Rules

1. Output ONLY valid Kotlin code. No markdown fences, no explanation.
2. Package: `com.mdesigner.app.ui.screens`
3. Import `com.mdesigner.app.ui.theme.AppColors` for color references.
4. Composable takes `onNavigate: (String) -> Unit = {}` parameter.
5. Use `@OptIn(ExperimentalMaterial3Api::class)`.
6. Map ALL components from JSON. Do not skip any.
7. Apply style values EXACTLY: fontSize → .sp, fontWeight, color, backgroundColor, cornerRadius → RoundedCornerShape, padding → .dp.
8. Hex colors: #RRGGBB → `Color(0xFFRRGGBB)`, #RRGGBBAA → `Color(0xAARRGGBB)`.
9. Use `Icons.Outlined.*` for icons. The project includes `material-icons-extended` dependency, so ALL Outlined icons are available (Image, Photo, People, Group, AccountCircle, Widgets, etc.). Do NOT fall back to MoreVert — every icon in the mapping WILL compile.
10. NAVIGATION IS MANDATORY: Any component with `props.navigate_to` MUST be wrapped in a clickable element (Button, IconButton, TextButton, or Modifier.clickable) that calls `onNavigate("route_name")`. Any button/action that logically leads to another screen (e.g., "시작하기", "다음", "설정", tab items) MUST call `onNavigate` with the appropriate route even if `navigate_to` is not explicitly set in JSON. Bottom navigation items MUST call `onNavigate` in their onClick. Route names should be the target screen's composable route (lowercase, no spaces).
11. LazyColumn as main scroll container, horizontal padding 24.dp, vertical spacing 16.dp.
12. LargeTopAppBar title: fontWeight = FontWeight.Light, fontSize = 28.sp.
13. Function name: `{screen_name}Screen` — MUST be English-only (PascalCase). NEVER use Korean/non-ASCII characters in file names, class names, function names, variable names, or identifiers. Korean text is ONLY allowed inside string literals (e.g., `Text("홈")`, `contentDescription = "검색"`).

## Icon Name Mapping (JSON → Kotlin)
home→Home, menu→Menu, arrow_back→ArrowBack, arrow_forward→ArrowForward, chevron_left→ChevronLeft, chevron_right→ChevronRight, expand_more→ExpandMore, expand_less→ExpandLess, more_vert→MoreVert, more_horiz→MoreHoriz, close→Close, dashboard→Dashboard, search→Search, settings→Settings, delete→Delete, done→Done, add→Add, add_circle→AddCircle, remove→Remove, remove_circle→RemoveCircle, clear→Clear, create→Create, edit→Edit, share→Share, open_in_new→OpenInNew, download→Download, upload→Upload, save→Save, undo→Undo, redo→Redo, content_copy→ContentCopy, info→Info, help→Help, help_outline→HelpOutline, login→Login, logout→Logout, visibility→Visibility, visibility_off→VisibilityOff, lock→Lock, filter_list→FilterList, sort→Sort, refresh→Refresh, check_circle→CheckCircle, cancel→Cancel, navigate_before→NavigateBefore, navigate_next→NavigateNext, first_page→FirstPage, last_page→LastPage, notifications→Notifications, notifications_none→NotificationsNone, notifications_active→NotificationsActive, person→Person, person_add→PersonAdd, people→People, group→Group, account_circle→AccountCircle, face→Face, public→Public, manage_accounts→ManageAccounts, star→Star, star_border→StarBorder, favorite→Favorite, favorite_border→FavoriteBorder, bookmark→Bookmark, bookmark_border→BookmarkBorder, thumb_up→ThumbUp, thumb_down→ThumbDown, flag→Flag, report→Report, chat→Chat, chat_bubble→ChatBubble, forum→Forum, message→Message, email→Email, phone→Phone, call→Call, videocam→Videocam, send→Send, comment→Comment, contacts→Contacts, contact_mail→ContactMail, mail_outline→MailOutline, mark_email_unread→MarkEmailUnread, note_add→NoteAdd, post_add→PostAdd, photo→Photo, photo_camera→PhotoCamera, image→Image, mic→Mic, play_arrow→PlayArrow, pause→Pause, stop→Stop, volume_up→VolumeUp, volume_off→VolumeOff, music_note→MusicNote, place→Place, location_on→LocationOn, my_location→MyLocation, near_me→NearMe, directions→Directions, map→Map, local_shipping→LocalShipping, store→Store, restaurant→Restaurant, flight→Flight, smartphone→Smartphone, tablet→Tablet, laptop→Laptop, battery_full→BatteryFull, bluetooth→Bluetooth, wifi→Wifi, signal_cellular_alt→SignalCellularAlt, brightness_high→BrightnessHigh, flash_on→FlashOn, gps_fixed→GpsFixed, folder→Folder, folder_open→FolderOpen, create_new_folder→CreateNewFolder, file_copy→FileCopy, insert_drive_file→InsertDriveFile, attachment→Attachment, cloud→Cloud, cloud_upload→CloudUpload, cloud_download→CloudDownload, description→Description, article→Article, check_box→CheckBox, check_box_outline_blank→CheckBoxOutlineBlank, radio_button_checked→RadioButtonChecked, radio_button_unchecked→RadioButtonUnchecked, toggle_on→ToggleOn, toggle_off→ToggleOff, warning→Warning, error→Error, error_outline→ErrorOutline, verified→Verified, verified_user→VerifiedUser, pending→Pending, priority_high→PriorityHigh, shopping_cart→ShoppingCart, shopping_bag→ShoppingBag, payment→Payment, credit_card→CreditCard, account_balance_wallet→AccountBalanceWallet, receipt→Receipt, storefront→Storefront, view_list→ViewList, view_module→ViewModule, grid_view→GridView, list→List, widgets→Widgets, fingerprint→Fingerprint, key→Key, vpn_key→VpnKey, security→Security, shield→Shield, qr_code→QrCode, dark_mode→DarkMode, light_mode→LightMode, language→Language, translate→Translate, calendar_today→CalendarToday, event→Event, alarm→Alarm, task_alt→TaskAlt, link→Link, sync→Sync, history→History, tune→Tune, smart_toy→SmartToy, business→Business, assessment→Assessment, analytics→Analytics, trending_up→TrendingUp, trending_down→TrendingDown, bar_chart→BarChart, pie_chart→PieChart

IMPORTANT: Not all icon names above exist in `Icons.Outlined.*` in Jetpack Compose. If an icon does NOT compile, use the closest available alternative:
- photo → use `Icons.Outlined.Image` (Photo doesn't exist in Outlined)
- people → use `Icons.Outlined.Group` (People doesn't exist in Outlined)
- widgets → use `Icons.Outlined.GridView` (Widgets may not exist)
- contact_mail → use `Icons.Outlined.Email`
- manage_accounts → use `Icons.Outlined.Person`
- signal_cellular_alt → use `Icons.Outlined.SignalCellularAlt` or `Wifi`
- storefront → use `Icons.Outlined.Store`
- local_shipping → use `Icons.Outlined.LocalShipping` or `ShoppingCart`

General rule: If unsure whether an icon exists in `Icons.Outlined`, pick the most semantically similar icon that you are CERTAIN exists. Common safe icons: Home, Search, Person, Settings, Notifications, Favorite, Star, Email, Phone, Chat, Image, PlayArrow, Add, Close, Menu, MoreVert, Share, Delete, Edit, Check, Info, Warning, Lock, AccountCircle, Group, ShoppingCart, Place, CalendarToday, Cloud, Folder, Description.

NEVER output MoreVert as a substitute for a meaningful icon. If you cannot find the exact icon, use the closest semantic match from the safe list above.

## FINAL REMINDER — NavigationBar Template (MUST follow this pattern exactly)
```
NavigationBar(containerColor = <tokens.colors.surface as Color hex>) {{
    NavigationBarItem(
        selected = <from JSON props.selected for EACH item>,
        onClick = {{ onNavigate("route") }},
        icon = {{ Icon(Icons.Outlined.Xxx, contentDescription = "label") }},
        label = {{ Text("label") }},
        colors = NavigationBarItemDefaults.colors(
            selectedIconColor = <tokens.colors.primary as Color hex>,
            selectedTextColor = <tokens.colors.primary as Color hex>,
            unselectedIconColor = <tokens.colors.textSecondary as Color hex>,
            unselectedTextColor = <tokens.colors.textSecondary as Color hex>,
            indicatorColor = Color.Transparent
        )
    )
}}
```
Replace angle-bracket placeholders with actual hex values from the provided Design Tokens.

## NAVIGATION CHECKLIST (verify before outputting code)
1. Every Button/TextButton that says "시작하기", "다음", "확인", "설정", "이동" etc. → MUST call onNavigate
2. Every BottomNavigation item → MUST have onClick = { onNavigate("route") }
3. Every list item / card that represents a navigable destination → MUST have Modifier.clickable { onNavigate("route") }
4. Back arrow icon button → MUST call onNavigate("back") or navController equivalent
5. If the screen JSON has ANY `navigate_to` prop → the corresponding UI element MUST call onNavigate with that exact value

If you generate a screen where onNavigate is declared but NEVER called, your output is WRONG. Every screen with navigation targets must use onNavigate at least once.

## COMPILE SAFETY RULES (avoid known Material3/Compose API pitfalls)
1. DO NOT set the `border` parameter on FilterChip/AssistChip/ElevatedFilterChip. The `FilterChipDefaults.*Border(...)` return type differs across versions (ChipBorder vs BorderStroke?) and breaks compilation. Omit `border` entirely; use `colors` for styling.
2. Prefer stable, widely-available Material3 APIs. If unsure an overload exists, use the simplest form.

## IMPORTS (CRITICAL — incomplete imports are the #1 cause of build failure)
Write a COMPLETE import block. Every symbol you reference MUST be imported. The project depends ONLY on: compose foundation, material3, material-icons-extended, compose.ui, navigation-compose. Use ONLY symbols from these.

Start the file with EXACTLY this import block (it is verified to resolve every symbol these screens need — use wildcards, do not trim it):

```
package com.mdesigner.app.ui.screens

import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.*
import androidx.compose.foundation.lazy.grid.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.*
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.nestedscroll.nestedScroll
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.*
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.navigation.NavController
import androidx.navigation.compose.*
import com.mdesigner.app.ui.theme.AppColors
```

Then the `@OptIn(ExperimentalMaterial3Api::class)` annotation and the composable. For collapsing top bars use `TopAppBarDefaults.enterAlwaysScrollBehavior()` / `exitUntilCollapsedScrollBehavior()` with `Modifier.nestedScroll(scrollBehavior.nestedScrollConnection)`."""


def _to_pascal_case(name: str, index: int = 0) -> str:
    import re
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", name)
    if not cleaned:
        return f"Screen{index + 1}" if index > 0 else "Main"
    if cleaned[0].isdigit():
        cleaned = "Screen" + cleaned
    return cleaned[0].upper() + cleaned[1:]


class LLMCodeGenerator:
    def __init__(self, region: str = "us-west-2") -> None:
        self._region = region

    def _get_model(self) -> BedrockModel:
        model_id = get_model_id("codegen")
        return BedrockModel(model_id=model_id, region_name=self._region)

    async def generate_screen(self, screen: dict[str, Any], tokens: dict[str, Any], route_map: dict[str, str] | None = None, index: int = 0) -> tuple[str, str]:
        """Generate a single screen .kt file.

        Returns (file_path, kotlin_code).
        """
        screen_name = _to_pascal_case(screen.get("name", "Main"), index)
        file_path = f"app/src/main/java/com/mdesigner/app/ui/screens/{screen_name}Screen.kt"

        tokens_json = json.dumps(tokens, indent=2, ensure_ascii=False)
        screen_json = json.dumps(screen, indent=2, ensure_ascii=False)

        loader = get_prompt_loader()
        base_prompt = (await loader.get(SCREEN_CODEGEN) if loader else None) or SCREEN_CODEGEN_PROMPT
        system = base_prompt.replace("{screen_name}", screen_name)

        # Extract all icons used in this screen for explicit mapping hint
        icon_hints = self._extract_icon_hints(screen)

        # Build route map section
        route_section = ""
        if route_map:
            route_lines = [f"- \"{name}\" → route: \"{route}\"" for name, route in route_map.items()]
            route_section = f"""

## Navigation Route Map (use these EXACT route strings in onNavigate calls)
{chr(10).join(route_lines)}

When a component has navigate_to = "screen name", call onNavigate with the corresponding route from above."""

        user_message = f"""## Design Tokens
{tokens_json}

## Screen JSON
{screen_json}

## Icon Mapping for this screen (USE THESE EXACTLY)
{icon_hints}{route_section}

Generate the complete .kt file now. Use Icons.Outlined.* for ALL icons above — they are ALL available via material-icons-extended dependency."""

        agent = Agent(
            model=self._get_model(),
            system_prompt=system,
            callback_handler=None,
        )

        result = await asyncio.to_thread(agent, user_message)
        code = str(result)

        code = self._clean_code(code)

        logger.info("llm_screen_generated", screen=screen_name, code_len=len(code))
        return file_path, code

    def _extract_icon_hints(self, screen: dict[str, Any]) -> str:
        """Extract all icon names from the screen and provide explicit Kotlin mapping."""
        ICON_MAP = {
            "home": "Home", "search": "Search", "settings": "Settings", "person": "Person",
            "account_circle": "AccountCircle", "notifications": "Notifications", "chat": "Chat",
            "email": "Email", "phone": "Phone", "favorite": "Favorite", "star": "Star",
            "share": "Share", "image": "Image", "photo": "Photo", "photo_camera": "PhotoCamera",
            "group": "Group", "people": "People", "public": "Public", "widgets": "Widgets",
            "dashboard": "Dashboard", "menu": "Menu", "close": "Close", "add": "Add",
            "edit": "Edit", "delete": "Delete", "done": "Done", "more_vert": "MoreVert",
            "arrow_back": "ArrowBack", "send": "Send", "place": "Place", "map": "Map",
            "calendar_today": "CalendarToday", "smart_toy": "SmartToy", "business": "Business",
            "analytics": "Analytics", "trending_up": "TrendingUp", "bar_chart": "BarChart",
            "shopping_cart": "ShoppingCart", "cloud": "Cloud", "folder": "Folder",
            "download": "Download", "upload": "Upload", "bookmark": "Bookmark",
            "lock": "Lock", "visibility": "Visibility", "info": "Info", "warning": "Warning",
            "play_arrow": "PlayArrow", "mic": "Mic", "videocam": "Videocam",
            "grid_view": "GridView", "view_list": "ViewList", "store": "Store",
            "location_on": "LocationOn", "filter_list": "FilterList", "sort": "Sort",
            "refresh": "Refresh", "history": "History", "tune": "Tune", "link": "Link",
        }

        icons_found: set[str] = set()
        def walk(comp: dict[str, Any]) -> None:
            icon = comp.get("props", {}).get("icon", "")
            if icon:
                icons_found.add(icon)
            actions = comp.get("props", {}).get("actions", [])
            if isinstance(actions, list):
                for a in actions:
                    if isinstance(a, str):
                        icons_found.add(a)
            nav_icon = comp.get("props", {}).get("navigationIcon", "")
            if nav_icon:
                icons_found.add(nav_icon)
            for child in comp.get("children", []):
                walk(child)

        for comp in screen.get("components", []):
            walk(comp)

        if not icons_found:
            return "No icons in this screen."

        lines = []
        for icon in sorted(icons_found):
            kotlin = ICON_MAP.get(icon, icon.replace("_", " ").title().replace(" ", ""))
            lines.append(f"- \"{icon}\" → Icons.Outlined.{kotlin}")
        return "\n".join(lines)

    async def generate_all_screens(
        self,
        screens: list[dict[str, Any]],
        tokens: dict[str, Any],
        progress_callback: Callable[..., None] | None = None,
    ) -> dict[str, str]:
        """Generate all screens sequentially. Returns {file_path: code}.

        Sequential to avoid rate limits and provide clear progress per screen.
        Raises on first failure so the task is marked as failed.
        """
        files: dict[str, str] = {}

        # Build route map: screen name → navigation route (with index for uniqueness)
        route_map: dict[str, str] = {}
        for i, s in enumerate(screens):
            name = s.get("name", f"Screen{i + 1}")
            route = _to_pascal_case(name, i).lower()
            route_map[name] = route

        for i, screen in enumerate(screens):
            screen_name = screen.get("name", f"Screen {i + 1}")
            if progress_callback:
                progress_callback(screen_name=screen_name, index=i, total=len(screens), status="generating")

            path, code = await self.generate_screen(screen, tokens, route_map, i)
            files[path] = code

            if progress_callback:
                progress_callback(screen_name=screen_name, index=i, total=len(screens), status="done")

        return files

    def _clean_code(self, code: str) -> str:
        """Strip markdown fences and non-Kotlin preamble/postamble."""
        code = code.strip()

        if code.startswith("```"):
            first_newline = code.index("\n") if "\n" in code else len(code)
            code = code[first_newline + 1:]
        if code.endswith("```"):
            code = code[: code.rfind("```")]

        code = code.strip()

        if not code.startswith("package"):
            pkg_idx = code.find("package")
            if pkg_idx > 0:
                code = code[pkg_idx:]

        return code

