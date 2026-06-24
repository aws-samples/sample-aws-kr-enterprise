import asyncio
import json
from typing import Any, cast

import structlog
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.session.s3_session_manager import S3SessionManager

from src.admin.config_service import get_model_id
from src.ai.design_validator import DesignValidator
from src.ai.models import AgentInput, ChatHistoryMessage, ChatHistoryResponse, ChatRequest
from src.ai.task_manager import TaskStatus, task_manager
from src.common.circuit_breaker import bedrock_circuit
from src.common.config import Settings
from src.common.db.client import DynamoDBClient
from src.common.db.tables import FILES_TABLE, PROJECTS_TABLE
from src.common.exceptions import ServiceUnavailableException
from src.common.s3.client import S3Client
from src.projects.models import StageStatus, VersionAction
from src.projects.version_service import VersionService
from src.prompts.loader import get_prompt_loader
from src.prompts.slots import (
    CHATBOT_SYSTEM,
    DESIGN_CHAT,
    DESIGNER_SYSTEM,
    MODIFY_SYSTEM,
    REQUIREMENTS_SYNTHESIS,
    WIREFRAME_CHAT,
    WIREFRAME_SYSTEM,
)

logger = structlog.get_logger()

CHATBOT_SYSTEM_PROMPT = """You are a requirements gathering assistant for an Android app designer tool.
You MUST respond in Korean at all times.

## Fixed decisions (DO NOT ask about these):
- Platform: Android
- Output: Jetpack Compose UI code

## Your role:
Gather app requirements through conversation. First understand the overall app concept, then focus follow-up questions on UI/UX:
- App purpose and business context
- Target users and key usage scenarios
- Core features and screen composition
- Navigation flow between screens
- Special UX requirements or constraints

Your goal is to understand the FULL app concept so that a design agent can generate appropriate UI wireframes. You gather both business requirements AND UI/UX requirements — but your output is always UI design, never code.

## Follow-up questions MUST focus on UI/UX:
After understanding the app concept, your follow-up questions should be about:
- How many screens and what are they?
- What components/elements should each screen have?
- Navigation pattern (bottom tabs, side drawer, stack navigation?)
- Layout preferences (card-based, list-based, grid?)
- Any reference apps for visual style?

## What you NEVER do:
- NEVER suggest code implementation (CDK, React, Flutter, API, etc.)
- NEVER discuss architecture, database, infrastructure, or backend implementation
- NEVER recommend tech stacks or libraries
- NEVER offer implementation step options like "what should we build first?"
- NEVER ask about API design, state management, or data models
- You gather requirements to produce UI DESIGN, not to write code

## Design pipeline (for your awareness):
1. Requirements gathering (current stage) → 2. Wireframe generation → 3. UI design → 4. Handoff (code generation)
You are in stage 1. After confirmation, a separate design agent generates wireframes. Code generation is the final stage 4.
NEVER say "I will generate code" or "I will create the design". Say "the design agent will generate wireframes once confirmed".

## File handling:
If user-uploaded file content is included in the message, reference it fully. Do NOT re-ask about information already available in the file.

## Ready signal:
When you judge sufficient information has been gathered, include exactly this string at the end of your response: [READY_TO_PROCEED]
This marker is not shown to the user — it activates the "Start Design" button in the UI.
You may include this marker while still allowing the user to continue providing more information."""

MODIFY_SYSTEM_PROMPT = """You are a mobile UI design modifier. You receive an existing design and modification requests. Return ONLY the patches — do NOT regenerate the entire design.

## Rules:
1. Return ONLY a JSON object with "patches" array
2. Each patch targets a specific screen and component by ID
3. Do NOT return screens/components that are not being modified
4. Preserve all existing component IDs

## Patch format:
{"patches": [
  {"action": "update", "screen": "screen name", "id": "component-id", "changes": {"props": {...}, "style": {...}, "children": [...]}},
  {"action": "add", "screen": "screen name", "parent_id": "parent-component-id", "position": "after:sibling-id" or "first" or "last", "component": {"id": "new-id", "type": "...", "props": {...}, "children": [...]}},
  {"action": "remove", "screen": "screen name", "id": "component-id"},
  {"action": "add_screen", "screen": {"name": "...", "purpose": "...", "components": [...]}},
  {"action": "remove_screen", "screen": "screen name"}
]}

## Actions:
- "update": modify props/style/children of an existing component
- "add": insert a new component as child of parent_id
- "remove": delete a component by id
- "add_screen": add entirely new screen
- "remove_screen": remove a screen

Return ONLY valid JSON with patches array. No explanation text."""

WIREFRAME_CHAT_PROMPT = """You are a wireframe modification assistant for an Android app designer tool.
You MUST respond in Korean (한국어) at all times.

## Your role:
Help users refine and modify an EXISTING wireframe design through conversation.
You are NOT gathering requirements from scratch — a wireframe already exists.
The current wireframe design is provided below as context.

## What you do:
- Answer questions about the current wireframe (what screens exist, what components are where)
- Discuss proposed modifications (adding/removing screens, rearranging components, changing navigation)
- Clarify ambiguous modification requests
- Suggest alternatives based on the design principles

## What you do NOT do:
- Generate JSON or code
- Apply modifications directly (the user will click "수정 적용" button when ready)
- Ask about app purpose, target users, etc. (already decided in requirements stage)

## Current wireframe design:
{design_context}
"""

DESIGN_CHAT_PROMPT = """You are a UI design modification assistant for an Android app designer tool.
You MUST respond in Korean (한국어) at all times.

## Your role:
Help users refine and modify an EXISTING UI design through conversation.
A full design with colors, typography, spacing already exists.
The current design is provided below as context.

## What you do:
- Answer questions about the current design (colors, components, layout)
- Discuss proposed visual modifications (color changes, spacing, component styling, dark mode)
- Clarify ambiguous modification requests
- Suggest alternatives based on the design guidelines (Focus, Natural, Essential)

## What you do NOT do:
- Generate JSON or code
- Apply modifications directly (the user will click "수정 적용" button when ready)
- Ask about app purpose or requirements (already decided)

## Current design:
{design_context}
"""

WIREFRAME_SYSTEM_PROMPT = """You are a mobile UI specialist designer. Generate wireframe-level UI structure following the layout and interaction principles below. Focus on component placement, screen flow, and information hierarchy — NOT visual styling (colors, spacing values, shadows, motion).

## Design Principles

### 1. Focus
- Split screen into Viewing Area (top) and Interaction Area (bottom).
- Viewing content goes top, interactive elements (tab bars, primary buttons, inputs) go bottom.
- One protagonist per screen. No competing titles/headers.

### 2. Natural
- One-hand operable even on large screens. Primary actions in thumb-reachable bottom zone.
- Use Extend Title pattern — large headline that collapses on scroll.
- Generous top whitespace is intentional.

### 3. Essential
- Only necessary features and content. Remove decorative filler.
- Clear information hierarchy — one protagonist per screen.

## Focus Blocks
- Card block: group independent items (album thumbnails, dashboard cards).
- List block: uniform structure items (settings, contacts).
- Image block: single image as protagonist (full-bleed hero).

## Component Placement Rules

### TopAppBar / Extend Title
- Extend Title pattern by default (~40% screen height for title area on phone).
- Expandable app bar: two states only (expanded/collapsed), no intermediate.
- Phone landscape: expandable app bar NOT applied.
- Primary actions go to Bottom Bar/FAB. Top bar: max 3 action icons, rest in overflow menu (more_vert).

### BottomNavigation (Main Tab)
- Max 4 tabs (5 if necessary). Text-primary (icons secondary). No swipe between MAIN tabs — tap only.
- Active tab name = screen title (top title can be omitted).
- No overflow/more button in bottom nav.

### Sub Tabs
- At top of content, text-only. 5+ tabs scrollable. Left/right swipe between sub tabs IS supported.

### Button
- NEVER mix Flat and Contained buttons on the same screen.
- Only one high-emphasis button per screen.

### FAB
- Bottom-right. One primary CTA per screen.

### Button
- Only one high-emphasis button per screen.

### Dialog
- User action required → bottom. Loading/no-action → center. Dropdown → near touch point.

### ListItem
- Distinguish 1-line / 2-line / 3-line heights.

### First Time Use
- Empty state: MUST include shortcut to add new item. No dead ends.

### Selection Mode
- Entry: long press. App bar transforms to selection mode.

### Edit Mode
- Portrait phone: confirm button at bottom.

## Screen Flow & Navigation (CRITICAL — READ CAREFULLY)

The `navigate_to` prop is what makes the generated Android app actually navigate between screens.
WITHOUT navigate_to, buttons are dead — users cannot move between screens.

### Rules:
1. EVERY Button, TextButton, FAB that moves to another screen → MUST have `"navigate_to": "exact_screen_name"`
2. EVERY BottomNavigationItem → MUST have `"navigate_to": "screen_name"`
3. EVERY ListItem/Card that opens a detail/sub screen → MUST have `"navigate_to": "screen_name"`
4. Onboarding/welcome screens MUST have a Button with navigate_to pointing to the next screen
5. The navigate_to value MUST exactly match another screen's "name" field

### Examples of CORRECT usage:
- Onboarding "시작하기" button: `{"type": "Button", "props": {"text": "시작하기", "navigate_to": "Permission Opt-in Screen"}}`
- Bottom nav tab: `{"type": "BottomNavigationItem", "props": {"label": "홈", "icon": "home", "selected": true, "navigate_to": "Home Screen"}}`
- Settings list item: `{"type": "ListItem", "props": {"headlineText": "알림 설정", "navigate_to": "Notification Settings"}}`

### VALIDATION CHECK (before outputting JSON):
For each screen, verify: Is there at least ONE component with navigate_to? If a screen has buttons/actions but ZERO navigate_to props, your output is WRONG. Fix it before returning.

## Responsive — Window Size Classes
- Compact (<600dp): single column. Medium (600-839dp): 2 columns possible. Expanded (>=840dp): master-detail.

## Writing
- Every text must serve one purpose: make user choose / act / understand. If none, delete it.
- Empty states must have a next-step button. No dead ends.

## Output Format
Return ONLY valid JSON. Wireframe focuses on structure — style values are optional:
{"screens": [{"name": "screen name", "purpose": "description", "components": [{"id": "unique-id", "type": "ComponentType", "props": {"text": "...", "label": "...", "navigate_to": "screen name"}, "children": [...]}]}], "tokens": {}}

IMPORTANT: Screen "name" MUST be in English (e.g., "Welcome Screen", "Daily Timeline", "Settings"). This name becomes the Kotlin file name and navigation route. Korean text goes in component props (text, label), NOT in screen names.

Valid component types: Scaffold, TopAppBar, LargeTopAppBar, BottomNavigation, BottomNavigationItem, FAB, ExtendedFloatingActionButton, Button, TextButton, IconButton, Card, ListItem, TextField, SearchBar, Text, Icon, Image, Row, Column, LazyColumn, Box, Surface, Divider, Spacer, Switch, Checkbox, RadioButton, Slider, ProgressIndicator, Dialog, BottomSheet, Tab, TabRow, Chip, FilterChip, AssistChip, Badge, EmptyState, SwipeToDismiss, ModalNavigationDrawer, NavigationDrawerItem

## Icon Names (use ONLY from this list — Material Icons Outlined)
home, menu, arrow_back, arrow_forward, chevron_left, chevron_right, expand_more, expand_less, more_vert, more_horiz, close, dashboard, search, settings, delete, done, info, help, logout, login, visibility, visibility_off, lock, filter_list, sort, refresh, check_circle, cancel, add_circle, open_in_new, download, upload, save, edit, share, bookmark, favorite, star, star_border, thumb_up, chat, email, phone, message, call, send, notifications, comment, contacts, add, remove, clear, create, flag, image, mic, play_arrow, pause, person, person_add, group, account_circle, place, location_on, map, store, smartphone, folder, cloud, attachment, article, check_box, toggle_on, warning, error, verified, pending, shopping_cart, payment, credit_card, view_list, grid_view, fingerprint, key, shield, dark_mode, light_mode, language, calendar_today, event, alarm, task_alt, link, sync, history, tune, smart_toy, business, analytics, trending_up, bar_chart

## Props Reference
- Icon/IconButton: {"icon": "icon_name"}
- TopAppBar: {"title": "...", "navigationIcon": "icon_name", "actions": ["icon1", "icon2"]}
- BottomNavigationItem: {"label": "...", "icon": "icon_name", "selected": true/false, "navigate_to": "screen_name"}
- Button/TextButton: {"text": "...", "navigate_to": "screen_name"} ← REQUIRED if this button navigates
- FAB: {"text": "...", "icon": "icon_name", "navigate_to": "screen_name"}
- ListItem: {"headlineText": "...", "supportingText": "...", "leadingContent": "avatar/icon", "navigate_to": "screen_name"}
- Card: {"clickable": true/false, "navigate_to": "screen_name"}
- FilterChip: {"label": "...", "selected": true/false}
- TextField/SearchBar: {"placeholder": "...", "label": "..."}
- Text: {"text": "..."}"""

DESIGNER_SYSTEM_PROMPT = """You are a mobile UI specialist designer. Generate Jetpack Compose UI designs strictly following the guidelines below.

## Design Principles

### 1. Focus
- Split screen into Viewing Area (top) and Interaction Area (bottom).
- Viewing content top, interactive elements (tab bars, primary buttons, inputs) bottom.
- One protagonist per screen. No competing titles/headers.

### 2. Natural
- One-hand operable on large screens. Primary actions in thumb-reachable bottom zone.
- Use Extend Title pattern — Light 40sp large headline that collapses on scroll.
- Extend Title area occupies ~40% of screen height (phone) / ~19% (tablet).
- Generous top whitespace is intentional. Whitespace is part of the design language.

### 3. Essential
- Only necessary features and content. Remove decorative filler, one-time hints.
- Clear information hierarchy — one protagonist per screen.
- Quality of consumption experience over feature count.

## Visual Depth — Blur · Dim · Shadow
- Blur: Maintain connection with previous content while focusing on current.
- Dim: Focus on topmost layer (behind dialogs/bottom sheets).
- Shadow: Light hierarchy between related screens/cards. NO heavy 3D — thin and subtle.
- Unrelated transitions: Blur+Dim. Related transitions: Shadow only.

## Focus Blocks — Card · List · Image
- Card block: Group independent items (album thumbnails, grid).
- List block: Uniform structure items (settings, contacts).
- Image block: Single image as protagonist (full-bleed hero).
- Margins optimized per block — Card generous, List tight.

## Color Tokens
Light: primary=#0381fe, primaryDark=#0072de, controlActivated=#3e91ff, bg=#fafafa, surface=#ffffff, text=#000000, textSecondary=#00000099, divider=#0000001f
Dark: primary=#3e91ff, primaryDark=#3e91ff, bg=#080808, surface=#1a1a1a, text=#ffffff, textSecondary=#ffffff99, divider=#ffffff1f
- Colors separated by semantic role (Primary/PrimaryDark/ControlActivated).
- FAB and Contained Button MUST use `primary` color for backgroundColor. Do NOT use controlActivated for buttons/FAB.
- controlActivated is ONLY for switches, checkboxes, sliders, and progress indicators in active state.

## Typography (sp scale, Roboto / system sans)
ExtendTitle: Light 40sp | DialogTitle: Medium 20sp | Title: Regular 19sp | MainList: Regular 18sp | TextButton: Regular 17sp | Body: Regular 16sp | RaisedButton: Regular 15sp | SubHeader: Medium 14sp | SubList: Regular 13sp (minimum)
- Body text NEVER below 13sp. Use 16sp as default body for accessibility.
- Extend Title must be Light weight — thin strokes create visual breathing room for Focus principle.

## Layout Rules
- Minimum side margin 24dp (accounts for curved edges/rounded corners).
- Card gap: 12-16dp, Section gap: 32dp.
- Touch target: minimum 48x48dp.
- Thumbnail radius: 26dp (list) / 20dp (card) / 12dp (inline).
- Button radius: 18dp (or full pill).
- Reject/Grip zone — only harmless controls at screen edges (bottom-left/right).

## Component Rules

### TopAppBar / Extend Title
- Extend Title pattern by default. Light 40sp title → collapses to Regular 19sp on scroll.
- Expandable app bar has ONLY two states: expanded and collapsed. NO intermediate state.
- Phone landscape mode: expandable app bar is NOT applied.
- Primary actions go to Bottom Bar/FAB. Top bar: only secondary actions (search, more).
- Action buttons in app bar: maximum 3 icons. Additional actions go to overflow menu (more_vert).
- Scroll snap: when user releases mid-scroll, bar snaps to expanded or collapsed based on threshold.

### BottomNavigation (Main Tab)
- Max 4 tabs (5 if necessary). Beyond that, use drawer.
- Text-primary (icons secondary). No swipe between MAIN tabs — tap only.
- When BottomNavigation is used, active tab name becomes screen title (top title can be omitted).
- No overflow/more button in bottom navigation tabs.
- Tab labels: keep short (N characters or less for localization). Use full text in app bar, abbreviated in tab.

### Sub Tabs (Secondary tabs within a screen)
- Placed at TOP of content area, text-only, fixed position.
- 5+ sub tabs: scrollable horizontally.
- Sub tabs DO support left/right swipe to switch between tabs (unlike main bottom tabs).

### Button
- Flat (no background): toolbar/dialog where extra layers are unwanted.
- Contained (filled background): emphasize function in complex screens, CTA.
- NEVER mix Flat and Contained buttons on the same screen. Choose one style.
- Emphasis levels: Low (gray) / Medium / High (color). Only ONE high-emphasis button per screen.
- Corner radius: 18dp baseline.

### Bottom Bar (Action toolbar, distinct from BottomNavigation)
- For detail/action screens: place high-priority action buttons at bottom.
- Hides on scroll down, shows on scroll up.
- No overflow/more button in bottom bar.
- Do NOT place keyboard-related items (cancel, done, next) above keyboard — those stay with keyboard.

### FAB
- Bottom-right. One primary CTA per screen.

### Dialog
- User action required → bottom (one-hand reach). Loading/no-action → center. Dropdown → near touch.
- No excessive confirmation popups. Low-value delete or easily recoverable → execute immediately.
- No simple feedback popup after function execution.

### ListItem
- Main List Regular 18sp + Sub List Regular 13sp as baseline.
- Height: 48dp (1-line) / 64dp (2-line) / 72dp (3-line).

### Slider
- 4 states: normal / clicked / focused / disabled.

### ProgressIndicator
- Avoid fullscreen progress popups. Show progress in content area or on the action button.

### Toast
- Label Toast: tooltip on icon tap-and-hold. Disappears after seconds.
- Action Toast: contains related action button. Auto-dismisses after set time.

### Selection Mode
- Entry: menu edit button or long press.
- App bar transforms to selection mode, shows selected count.
- Hide bottom action bar when 0 items selected.

### Edit Mode
- Portrait phone: confirm button at bottom. Landscape: top. Keyboard shown: above keyboard.
- Editing existing items: make edit view as similar to viewing as possible.

### First Time Use
- Welcome: app intro, legal notices.
- Loading: first-run load time.
- Empty: no items to show — MUST include add-new-item shortcut. No dead ends.

## Motion
- Default easing: cubic-bezier(0.22, 0.25, 0, 1) — fast initial acceleration, gentle deceleration.
- Duration: micro 150-250ms, transition 300-450ms, sheet 200-300ms. Hard limit 100-500ms.
- Intuitive: clear causality in transitions. Seamless: unbroken connection (shared element). Tangible: spring/inertia feel.

## Responsive — Window Size Classes
- Compact (<600dp): portrait phone. Single column.
- Medium (600-839dp): folded foldable, landscape phone. 2 columns possible.
- Expanded (>=840dp): tablet, unfolded foldable. Master-detail, 2-3 columns.
- Design separate layouts per environment — no simple ratio scaling.

## Foldable — Posture
- Closed (cover): focus on 1 core feature.
- Open (main): Expanded layout.
- Flex (half-folded): top = viewing area, bottom = control area.
- App continuity: scroll position, input state, keyboard preserved across cover→main transition.

## Accessibility
- Text contrast: body 4.5:1, large text (18sp+ or 14sp Bold+) 3:1.
- Layout must not break at 200% text enlargement.
- Touch target minimum 48x48dp.
- Icon-only buttons MUST have aria-label.
- Never convey info by color alone — use icons/text/patterns for redundancy.
- Focus order: left→right, top→bottom. Logical grouping. Exclude decorative elements from focus.
- Font must not go below 13sp.

## Icon Design
- Clear metaphors. Never invent new abstract symbols.
- Simple, modular forms. Building-block composition for consistency.
- App icon: Squircle (rounded square, border-radius 28%).

## Writing — Focused · Simple · Empowering
- Every text must make user choose / act / understand. If none, delete it.
- Empty states must have a next-step button. No dead ends.
- Explain settings in user benefit terms, not technical jargon.

## MANDATORY Style Rules (apply to EVERY component)
The generated JSON is the single source of truth. Both the web renderer and Android handoff code read style values directly from this JSON. If a value is missing, the output will look wrong on both platforms.

EVERY component MUST include these style values where applicable:
- **All containers** (Column, Row, LazyColumn, Card, Surface): paddingHorizontal (minimum "24dp" for screen-level containers)
- **All text** (Text, headlineText in ListItem): fontSize, fontWeight, color
- **All cards**: backgroundColor, cornerRadius, marginHorizontal, marginVertical
- **All list items**: paddingHorizontal ("24dp"), paddingVertical, minHeight
- **All buttons**: backgroundColor, textColor, cornerRadius, paddingHorizontal, paddingVertical
- **FAB**: backgroundColor, iconColor, textColor, cornerRadius
- **TopAppBar**: backgroundColor, action icon colors
- **BottomNavigation**: backgroundColor, selectedColor, unselectedColor

Do NOT rely on defaults — explicitly specify every visual property.

## Output Format
Return ONLY valid JSON:
{"screens": [{"name": "screen name", "purpose": "description", "components": [{"id": "unique-id", "type": "ComponentType", "props": {"text": "...", "label": "...", "navigate_to": "screen name"}, "style": {"paddingHorizontal": "24dp", "fontSize": "16sp", "color": "#000000", "backgroundColor": "#ffffff", "cornerRadius": "16dp"}, "children": [...]}]}], "tokens": {"colors": {"primary": "#0381fe", "primaryDark": "#0072de", "controlActivated": "#3e91ff", "background": "#fafafa", "surface": "#ffffff", "text": "#000000", "textSecondary": "#00000099", "divider": "#0000001f"}, "typography": {"extendTitle": {"fontSize": "40sp", "fontWeight": "Light"}, "title": {"fontSize": "19sp", "fontWeight": "Regular"}, "body": {"fontSize": "16sp", "fontWeight": "Regular"}, "subList": {"fontSize": "13sp", "fontWeight": "Regular"}}, "spacing": {"screenMargin": "24dp", "cardGap": "12dp", "sectionGap": "32dp", "touchTarget": "48dp"}}}

Valid component types: Scaffold, TopAppBar, LargeTopAppBar, BottomNavigation, BottomNavigationItem, FAB, ExtendedFloatingActionButton, Button, TextButton, IconButton, Card, ListItem, TextField, SearchBar, Text, Icon, Image, Row, Column, LazyColumn, Box, Surface, Divider, Spacer, Switch, Checkbox, RadioButton, Slider, ProgressIndicator, Dialog, BottomSheet, Tab, TabRow, Chip, FilterChip, AssistChip, Badge, Snackbar, EmptyState, SwipeToDismiss, ModalNavigationDrawer, NavigationDrawerItem

## Icon Names (use ONLY from this list — Material Icons Outlined)
Navigation: home, menu, arrow_back, arrow_forward, chevron_left, chevron_right, expand_more, expand_less, more_vert, more_horiz, close, dashboard, navigate_before, navigate_next, first_page, last_page
Actions: search, settings, delete, done, info, help, logout, login, visibility, visibility_off, lock, filter_list, sort, refresh, check_circle, cancel, add_circle, remove_circle, open_in_new, download, upload, save, edit, share, bookmark, bookmark_border, favorite, favorite_border, star, star_border, thumb_up, thumb_down, content_copy, undo, redo
Communication: chat, email, phone, message, call, videocam, send, notifications, notifications_none, notifications_active, comment, forum, chat_bubble, contact_mail, contacts, mail_outline, mark_email_unread
Content: add, remove, clear, create, flag, report, note_add, post_add, image, mic, play_arrow, pause, stop, volume_up, volume_off, music_note
People: person, person_add, group, account_circle, face, public
Places: place, location_on, my_location, near_me, directions, map, local_shipping, store, restaurant, flight
Device: smartphone, tablet, laptop, battery_full, bluetooth, wifi, signal_cellular_alt, brightness_high, flash_on, gps_fixed
Files: folder, folder_open, create_new_folder, file_copy, insert_drive_file, attachment, cloud, cloud_upload, cloud_download, description, article
Form: check_box, check_box_outline_blank, radio_button_checked, radio_button_unchecked, toggle_on, toggle_off
Alert: warning, error, error_outline, help_outline, verified, pending, priority_high
Shopping: shopping_cart, shopping_bag, payment, credit_card, account_balance_wallet, receipt, storefront
Layout: view_list, view_module, grid_view, list
Security: fingerprint, key, vpn_key, security, shield, verified_user, qr_code
Misc: dark_mode, light_mode, language, translate, calendar_today, event, alarm, task_alt, link, sync, history, tune, smart_toy, business, assessment, analytics, trending_up, trending_down, bar_chart, pie_chart

## Props Reference (use these prop names for consistency)
- Icon/IconButton: {"icon": "icon_name"}
- TopAppBar/LargeTopAppBar: {"title": "...", "subtitle": "...", "navigationIcon": "icon_name", "actions": ["icon1", "icon2"]}
  NOTE: Use "title" only. Do NOT use "extendTitleText" — the title IS the extend title. Expanded/collapsed is renderer behavior, not separate props.
- BottomNavigationItem: {"label": "...", "icon": "icon_name", "selected": true/false, "navigate_to": "screen_name"}
- Button/TextButton: {"text": "...", "label": "...", "navigate_to": "target_screen_name"}
- FAB/ExtendedFloatingActionButton: {"text": "...", "icon": "icon_name", "navigate_to": "target_screen_name"}
- ListItem: {"headlineText": "...", "supportingText": "...", "trailingTopText": "...", "leadingContent": "avatar/icon", "isUnread": true/false, "navigate_to": "target_screen_name"}
- Card: {"clickable": true/false, "navigate_to": "target_screen_name"}
- FilterChip: {"label": "...", "selected": true/false}
- TextField/SearchBar: {"placeholder": "...", "label": "..."}
- Text: {"text": "...", "variant": "extendTitle/title/body/label"}
- Switch: {"checked": true/false}
- NavigationDrawerItem: {"label": "...", "icon": "icon_name", "selected": true/false, "badge": "count"}

## NAVIGATION RULE (CRITICAL — MANDATORY)

The `navigate_to` prop generates REAL page transitions in the Android app. Without it, buttons are dead.

### PRESERVE from wireframe:
If the input wireframe/previous stage already has `navigate_to` props on components, you MUST keep them unchanged. Do NOT remove or alter existing navigate_to values. You are adding visual style — navigation structure is already decided.

### MUST have navigate_to:
1. ALL BottomNavigationItems → navigate_to the screen that tab represents
2. ALL Buttons/TextButtons that move forward (onboarding "시작하기"/"다음"/"확인"/"건너뛰기") → navigate_to the next screen
3. ALL ListItems that open a sub-page or detail → navigate_to that screen
4. ALL Cards that are clickable and lead somewhere → navigate_to
5. Back buttons (navigationIcon: "arrow_back") → navigate_to the previous screen
6. If the wireframe didn't include navigate_to but the component clearly navigates → ADD it

### VALIDATION (do this before returning JSON):
Go through EVERY screen. Count components with navigate_to. If ANY screen with buttons/actions has ZERO navigate_to props, your output is BROKEN. Every screen except pure display screens (error, empty state with no actions) must have at least one navigate_to.

The value must EXACTLY match another screen's "name" field in the same JSON output."""


REQUIREMENTS_SYNTHESIS_PROMPT = """You are a UI/UX requirements synthesis specialist.
Given a conversation between a user and a design assistant about a mobile app, extract and organize ALL requirements into a structured JSON document.

## Output Format (return ONLY valid JSON):
{
  "app_name": "Name of the app",
  "purpose": "App purpose and business context",
  "target_users": "Target user description",
  "screens": [
    {
      "name": "Screen name",
      "purpose": "What this screen does",
      "key_components": ["component1", "component2"],
      "user_actions": ["action users can take"]
    }
  ],
  "navigation": {
    "type": "bottom_tabs or drawer or stack",
    "main_tabs": ["tab1", "tab2"],
    "flows": ["ScreenA -> ScreenB on action X"]
  },
  "visual_requirements": {
    "style_references": "Reference apps or styles mentioned",
    "color_preferences": "Any color preferences",
    "special_requirements": "Any special UX/UI requirements"
  }
}

Extract ALL information from the conversation. If something wasn't discussed, use reasonable defaults based on the app concept. Return ONLY valid JSON."""


class AIOrchestrator:
    def __init__(self, settings: Settings, db: DynamoDBClient, s3: S3Client) -> None:
        self._settings = settings
        self._db = db
        self._s3 = s3
        self._validator = DesignValidator()
        self._version_service = VersionService(db, s3)
        self._task_manager = task_manager

    def _get_model(self, slot: str) -> BedrockModel:
        model_id = get_model_id(slot)
        return BedrockModel(model_id=model_id, region_name="us-west-2")

    async def _invoke_agent(self, agent: Agent, input_text: str) -> str:
        """Invoke a Strands agent off the event loop with a hard timeout.

        Bedrock calls can otherwise hang indefinitely and exhaust the thread pool.
        Raises ServiceUnavailableException on timeout so the caller records a
        circuit-breaker failure and surfaces a 503 instead of blocking forever.
        """
        timeout = self._settings.bedrock_invocation_timeout_seconds
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(agent, input_text), timeout=timeout
            )
        except TimeoutError as exc:
            logger.error("agent_invocation_timeout", timeout_seconds=timeout)
            raise ServiceUnavailableException("AI") from exc
        return str(result)

    # ─── Chat (synchronous request/response) ───

    def _get_chat_session_manager(self, project_id: str, session_id: str) -> S3SessionManager:
        return S3SessionManager(
            session_id=session_id,
            bucket=self._settings.s3_bucket_name,
            prefix=f"projects/{project_id}/chat-sessions",
            region_name=self._settings.aws_region,
        )

    async def get_chat_history(self, project_id: str, session_id: str) -> ChatHistoryResponse:
        session_manager = self._get_chat_session_manager(project_id, session_id)

        loader = get_prompt_loader()
        chatbot_prompt = (await loader.get(CHATBOT_SYSTEM) if loader else None) or CHATBOT_SYSTEM_PROMPT

        agent = Agent(
            model=self._get_model("chat"),
            system_prompt=chatbot_prompt,
            session_manager=session_manager,
            callback_handler=None,
        )

        messages: list[ChatHistoryMessage] = []
        for msg in agent.messages:
            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content_blocks = msg.get("content", [])
            text_parts = []
            for block in content_blocks:
                if isinstance(block, dict) and "text" in block:
                    text_parts.append(block["text"])
            if text_parts:
                text = "\n".join(text_parts)
                # Strip file attachment content from user messages
                if "[첨부 파일 내용]" in text:
                    text = text[:text.index("[첨부 파일 내용]")]
                clean_text = text.replace("[READY_TO_PROCEED]", "").strip()
                if clean_text:
                    messages.append(ChatHistoryMessage(role=role, content=clean_text))

        has_ready = any("[READY_TO_PROCEED]" in str(m.get("content", "")) for m in agent.messages)

        return ChatHistoryResponse(messages=messages, ready_to_proceed=has_ready)

    async def is_chat_responding(self, project_id: str, session_id: str) -> bool:
        return await self._task_manager.is_chat_responding_remote(project_id, session_id)

    def _apply_patches(self, design: dict[str, Any], patches: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply patches to existing design. Returns new design with modifications."""
        import copy
        result = copy.deepcopy(design)

        def find_component(
            components: list[dict[str, Any]], comp_id: str
        ) -> tuple[list[dict[str, Any]], int] | None:
            for i, c in enumerate(components):
                if c.get("id") == comp_id:
                    return components, i
                if c.get("children"):
                    found = find_component(c["children"], comp_id)
                    if found:
                        return found
            return None

        def find_parent(components: list[dict[str, Any]], comp_id: str) -> dict[str, Any] | None:
            for c in components:
                if c.get("children"):
                    for child in c["children"]:
                        if child.get("id") == comp_id:
                            return c
                    found = find_parent(c["children"], comp_id)
                    if found:
                        return found
            return None

        for patch in patches:
            action = patch.get("action", "")
            screen_name = patch.get("screen", "")

            if action == "add_screen":
                new_screen = patch.get("screen") if isinstance(patch.get("screen"), dict) else patch.get("data", {})
                if isinstance(new_screen, dict) and new_screen.get("name"):
                    result.setdefault("screens", []).append(new_screen)
                continue

            if action == "remove_screen":
                result["screens"] = [s for s in result.get("screens", []) if s.get("name") != screen_name]
                continue

            # Find target screen
            target_screen = None
            for s in result.get("screens", []):
                if s.get("name") == screen_name:
                    target_screen = s
                    break
            if not target_screen:
                continue

            comp_id = patch.get("id", "")
            components = target_screen.get("components", [])

            if action == "update":
                found = find_component(components, comp_id)
                if found:
                    parent_list, idx = found
                    changes = patch.get("changes", {})
                    if "props" in changes:
                        parent_list[idx].setdefault("props", {}).update(changes["props"])
                    if "style" in changes:
                        parent_list[idx].setdefault("style", {}).update(changes["style"])
                    if "children" in changes:
                        parent_list[idx]["children"] = changes["children"]
                    if "type" in changes:
                        parent_list[idx]["type"] = changes["type"]

            elif action == "add":
                parent_id = patch.get("parent_id", "")
                new_component = patch.get("component", {})
                position = patch.get("position", "last")

                if parent_id:
                    found = find_component(components, parent_id)
                    if found:
                        parent_list, idx = found
                        parent_comp = parent_list[idx]
                        parent_comp.setdefault("children", [])
                        if position == "first":
                            parent_comp["children"].insert(0, new_component)
                        elif position.startswith("after:"):
                            after_id = position[6:]
                            for ci, child in enumerate(parent_comp["children"]):
                                if child.get("id") == after_id:
                                    parent_comp["children"].insert(ci + 1, new_component)
                                    break
                            else:
                                parent_comp["children"].append(new_component)
                        else:
                            parent_comp["children"].append(new_component)
                else:
                    components.append(new_component)

            elif action == "remove":
                found = find_component(components, comp_id)
                if found:
                    parent_list, idx = found
                    parent_list.pop(idx)

        return result

    def _summarize_design(self, snapshot: dict[str, Any]) -> str:
        """Summarize design snapshot for chat context — includes nested structure."""
        lines = []
        for screen in snapshot.get("screens", []):
            lines.append(f"\n### {screen.get('name', 'Screen')} — {screen.get('purpose', '')}")

            def walk(comps: list[dict[str, Any]], depth: int = 1) -> None:
                for c in comps:
                    indent = "  " * depth
                    label = c.get("type", "?")
                    props = c.get("props", {})
                    detail_parts = []
                    for key in ("text", "title", "label", "headlineText", "placeholder", "icon", "navigate_to"):
                        if props.get(key):
                            detail_parts.append(f'{key}="{props[key]}"')
                    detail = f" ({', '.join(detail_parts)})" if detail_parts else ""
                    lines.append(f"{indent}- {label}{detail}")
                    children = c.get("children", [])
                    if children and depth < 3:
                        walk(children, depth + 1)

            walk(screen.get("components", []))
        return "\n".join(lines) if lines else "Empty design"

    async def _get_chat_system_prompt(self, stage: str, design_context: str) -> str:
        loader = get_prompt_loader()
        if stage == "wireframe":
            template = (await loader.get(WIREFRAME_CHAT) if loader else None) or WIREFRAME_CHAT_PROMPT
            return template.replace("{design_context}", design_context)
        elif stage == "design":
            template = (await loader.get(DESIGN_CHAT) if loader else None) or DESIGN_CHAT_PROMPT
            return template.replace("{design_context}", design_context)
        return (await loader.get(CHATBOT_SYSTEM) if loader else None) or CHATBOT_SYSTEM_PROMPT

    def start_chat(self, request: ChatRequest) -> str:
        """Start chat in background, return task_id for polling."""
        if not bedrock_circuit.is_call_permitted():
            raise ServiceUnavailableException("AI")
        task = self._task_manager.create_task(request.project_id, "chat")
        asyncio.create_task(self._run_chat(task.task_id, request))
        return task.task_id

    async def _run_chat(self, task_id: str, request: ChatRequest) -> None:
        """Background chat execution."""
        tm = self._task_manager
        tm.update_task(task_id, status=TaskStatus.RUNNING, progress=10, current_step="AI 응답 생성 중")

        self._task_manager.set_chat_responding(request.project_id, request.session_id, True)
        try:
            # For wireframe/design stages, load current design as context
            loader = get_prompt_loader()
            system_prompt = (await loader.get(CHATBOT_SYSTEM) if loader else None) or CHATBOT_SYSTEM_PROMPT
            if request.stage in ("wireframe", "design"):
                snapshot = await self._get_latest_snapshot(request.project_id, request.stage)
                design_context = self._summarize_design(snapshot) if snapshot else "No design generated yet"
                system_prompt = await self._get_chat_system_prompt(request.stage, design_context)

            session_manager = self._get_chat_session_manager(request.project_id, request.session_id)

            agent = Agent(
                model=self._get_model("chat"),
                system_prompt=system_prompt,
                session_manager=session_manager,
                callback_handler=None,
            )

            # Only include file contents if they haven't been sent in this session yet
            input_text = request.message
            if request.file_ids:
                already_sent = any("[첨부 파일 내용]" in str(m.get("content", "")) for m in agent.messages)
                if not already_sent:
                    file_context = await self._load_file_contents(request.project_id, request.file_ids)
                    if file_context:
                        input_text = f"{request.message}\n\n[첨부 파일 내용]\n{file_context}"

            reply_text = await self._invoke_agent(agent, input_text)

            ready = "[READY_TO_PROCEED]" in reply_text
            clean_reply = reply_text.replace("[READY_TO_PROCEED]", "").strip()

            bedrock_circuit.record_success()
            tm.update_task(task_id, status=TaskStatus.COMPLETED, progress=100,
                           result={"reply": clean_reply, "ready_to_proceed": ready})
        except Exception as e:
            bedrock_circuit.record_failure()
            logger.error("chat_error", error=str(e), project_id=request.project_id)
            tm.update_task(task_id, status=TaskStatus.FAILED, error=str(e))
        finally:
            self._task_manager.set_chat_responding(request.project_id, request.session_id, False)

    # ─── Background Generate/Modify ───

    def start_generate(self, agent_input: AgentInput, user_id: str, team_id: str) -> str:
        if not bedrock_circuit.is_call_permitted():
            raise ServiceUnavailableException("AI")

        task = self._task_manager.create_task(agent_input.project_id, agent_input.stage)
        asyncio.create_task(self._run_generate(task.task_id, agent_input, user_id, team_id))
        return task.task_id

    def start_modify(self, agent_input: AgentInput, user_id: str, team_id: str) -> str:
        if not bedrock_circuit.is_call_permitted():
            raise ServiceUnavailableException("AI")

        task = self._task_manager.create_task(agent_input.project_id, agent_input.stage)
        asyncio.create_task(self._run_modify(task.task_id, agent_input, user_id, team_id))
        return task.task_id

    async def _synthesize_requirements(self, agent_input: AgentInput, task_id: str) -> dict[str, Any]:
        """Synthesize chat history into structured requirements document."""
        tm = self._task_manager
        tm.update_task(task_id, progress=15, current_step="요구사항 문서 생성 중",
                       log_step="synthesizing", log_detail="채팅 내역을 요구사항 문서로 정리합니다")

        loader = get_prompt_loader()
        synth_prompt = (await loader.get(REQUIREMENTS_SYNTHESIS) if loader else None) or REQUIREMENTS_SYNTHESIS_PROMPT

        agent = Agent(
            model=self._get_model("chat"),
            system_prompt=synth_prompt,
            callback_handler=None,
        )

        result_text = await self._invoke_agent(agent, agent_input.command)

        try:
            start = result_text.index("{")
            end = result_text.rindex("}") + 1
            return cast(dict[str, Any], json.loads(result_text[start:end]))
        except (ValueError, json.JSONDecodeError):
            # Fallback: wrap raw text as requirements
            return {"raw_requirements": result_text, "app_name": "Unknown", "screens": []}

    async def _run_generate(self, task_id: str, agent_input: AgentInput, user_id: str, team_id: str) -> None:
        tm = self._task_manager
        tm.update_task(task_id, status=TaskStatus.RUNNING, progress=5, current_step="이전 단계 결과 로드 중",
                       log_step="start", log_detail="디자인 생성을 시작합니다")

        try:
            # Load file contents if file_ids provided
            file_ids = agent_input.context.get("file_ids", [])
            if file_ids:
                file_context = await self._load_file_contents(agent_input.project_id, file_ids)
                if file_context:
                    agent_input.command = f"{agent_input.command}\n\n[첨부 파일 내용]\n{file_context}"

            if agent_input.stage == "requirements":
                # Step A only: Synthesize requirements document
                requirements_doc = await self._synthesize_requirements(agent_input, task_id)

                # Save requirements document
                tm.update_task(task_id, progress=80, current_step="요구사항 문서 저장",
                               log_step="saving_requirements", log_detail="요구사항 문서를 저장합니다")
                req_snapshot = json.dumps(requirements_doc, ensure_ascii=False).encode()
                await self._version_service.create_version(
                    project_id=agent_input.project_id, team_id=team_id,
                    stage_id="requirements", action=VersionAction.INITIAL,
                    command=agent_input.command, snapshot_data=req_snapshot, user_id=user_id,
                )
                await self._update_stage_status(team_id, agent_input.project_id, "requirements", StageStatus.COMPLETED)

                bedrock_circuit.record_success()
                tm.update_task(task_id, status=TaskStatus.COMPLETED, progress=100, current_step="완료",
                               result={"design": requirements_doc}, log_step="done", log_detail="요구사항 문서 생성 완료")
            else:
                # Non-requirements stages: wireframe/design individual regeneration
                # Always load requirements as base context
                requirements_snapshot = await self._get_latest_snapshot(agent_input.project_id, "requirements")
                if requirements_snapshot:
                    agent_input.context["requirements"] = requirements_snapshot
                    tm.update_task(task_id, log_step="context_loaded",
                                   log_detail="요구사항 문서를 참조합니다")

                if agent_input.stage == "wireframe":
                    # Wireframe uses requirements as previous_stage_result
                    if requirements_snapshot:
                        agent_input.context["previous_stage_result"] = requirements_snapshot
                elif agent_input.stage == "design":
                    # Design uses wireframe as previous_stage_result (with requirements also available)
                    wireframe_snapshot = await self._get_latest_snapshot(agent_input.project_id, "wireframe")
                    if wireframe_snapshot:
                        agent_input.context["previous_stage_result"] = wireframe_snapshot
                        tm.update_task(task_id, log_step="wireframe_loaded",
                                       log_detail="와이어프레임을 참조합니다")

                tm.update_task(task_id, progress=10, current_step="에이전트에 요구사항 전달 중",
                               log_step="analyzing", log_detail="요구사항을 분석하고 화면 구조를 도출합니다")

                design_result = await self._invoke_design_agent(agent_input, task_id)

                tm.update_task(task_id, progress=60, current_step="디자인 가이드라인 검증 중",
                               log_step="validating", log_detail="생성된 디자인을 디자인 가이드라인과 비교합니다")

                validation = self._validator.validate(design_result, agent_input.stage)
                if not validation["valid"]:
                    tm.update_task(task_id, progress=75, current_step="가이드라인 위반 수정 중",
                                   log_step="fixing", log_detail=f"위반 {len(validation['violations'])}건 수정 중")
                    agent_input.context["validation_feedback"] = validation["violations"]
                    design_result = await self._invoke_design_agent(agent_input, task_id)

                tm.update_task(task_id, progress=90, current_step="결과 저장 중",
                               log_step="saving", log_detail="디자인 결과를 프로젝트에 저장합니다")

                snapshot_data = json.dumps(design_result, ensure_ascii=False).encode()
                await self._version_service.create_version(
                    project_id=agent_input.project_id,
                    team_id=team_id,
                    stage_id=agent_input.stage,
                    action=VersionAction.INITIAL if not agent_input.context.get("is_modification") else VersionAction.MODIFY,
                    command=agent_input.command,
                    snapshot_data=snapshot_data,
                    user_id=user_id,
                )

                await self._update_stage_status(team_id, agent_input.project_id, agent_input.stage, StageStatus.COMPLETED)
                bedrock_circuit.record_success()

                tm.update_task(task_id, status=TaskStatus.COMPLETED, progress=100, current_step="완료",
                               result={"design": design_result}, log_step="done", log_detail="디자인 생성 완료")

        except Exception as e:
            bedrock_circuit.record_failure()
            logger.error("ai_generation_error", error=str(e), project_id=agent_input.project_id)
            tm.update_task(task_id, status=TaskStatus.FAILED, current_step="오류 발생",
                           error=str(e), log_step="error", log_detail=str(e))

    async def _run_modify(self, task_id: str, agent_input: AgentInput, user_id: str, team_id: str) -> None:
        tm = self._task_manager
        tm.update_task(task_id, status=TaskStatus.RUNNING, progress=5, current_step="기존 디자인 로드 중",
                       log_step="start", log_detail="디자인 수정을 시작합니다")

        try:
            current_snapshot = await self._get_latest_snapshot(agent_input.project_id, agent_input.stage)
            if not current_snapshot:
                tm.update_task(task_id, status=TaskStatus.FAILED, current_step="오류",
                               error="수정할 기존 디자인이 없습니다", log_step="error", log_detail="기존 디자인 없음")
                return

            tm.update_task(task_id, progress=20, current_step="수정 패치 생성 중",
                           log_step="patching", log_detail="에이전트가 수정할 부분만 식별합니다")

            # Ask agent for patches only
            patch_prompt = json.dumps({
                "command": agent_input.command,
                "current_design": current_snapshot,
            }, ensure_ascii=False)

            loader = get_prompt_loader()
            modify_prompt = (await loader.get(MODIFY_SYSTEM) if loader else None) or MODIFY_SYSTEM_PROMPT

            agent = Agent(
                model=self._get_model("modify"),
                system_prompt=modify_prompt,
                callback_handler=None,
            )
            patch_text = await self._invoke_agent(agent, patch_prompt)

            tm.update_task(task_id, progress=60, current_step="패치 적용 중",
                           log_step="applying", log_detail="기존 디자인에 수정사항을 적용합니다")

            # Parse patches
            try:
                start = patch_text.index("{")
                end = patch_text.rindex("}") + 1
                patch_data = json.loads(patch_text[start:end])
            except (ValueError, json.JSONDecodeError):
                patch_data = {"patches": []}

            # Apply patches to current design
            design_result = self._apply_patches(current_snapshot, patch_data.get("patches", []))

            tm.update_task(task_id, progress=80, current_step="디자인 가이드라인 검증 중",
                           log_step="validating", log_detail="수정된 디자인을 검증합니다")

            self._validator.validate(design_result, agent_input.stage)
            patches_applied = len(patch_data.get("patches", []))
            tm.update_task(task_id, log_step="patch_count", log_detail=f"{patches_applied}건의 수정 적용됨")

            tm.update_task(task_id, progress=90, current_step="결과 저장 중",
                           log_step="saving", log_detail="수정된 디자인을 저장합니다")

            snapshot_data = json.dumps(design_result, ensure_ascii=False).encode()

            latest_versions = await self._version_service.list_versions(agent_input.project_id, agent_input.stage, limit=1)
            parent_id = latest_versions["items"][0].version_id if latest_versions["items"] else None

            await self._version_service.create_version(
                project_id=agent_input.project_id,
                team_id=team_id,
                stage_id=agent_input.stage,
                action=VersionAction.MODIFY,
                command=agent_input.command,
                snapshot_data=snapshot_data,
                user_id=user_id,
                parent_version_id=parent_id,
            )

            bedrock_circuit.record_success()
            tm.update_task(task_id, status=TaskStatus.COMPLETED, progress=100, current_step="완료",
                           result={"design": design_result}, log_step="done", log_detail="디자인 수정 완료")

        except Exception as e:
            bedrock_circuit.record_failure()
            logger.error("ai_modify_error", error=str(e))
            tm.update_task(task_id, status=TaskStatus.FAILED, current_step="오류 발생",
                           error=str(e), log_step="error", log_detail=str(e))

    # ─── Helpers ───

    async def _load_file_contents(self, project_id: str, file_ids: list[str]) -> str:
        contents: list[str] = []
        for file_id in file_ids:
            item = await self._db.get_item(
                table_name=FILES_TABLE,
                key={"projectId": project_id, "sk": f"FILE#{file_id}"},
            )
            if not item:
                continue

            parsed_key = item.get("parsedContentKey")
            if parsed_key:
                try:
                    data = await self._s3.get_object(parsed_key)
                    contents.append(f"### {item.get('filename', 'file')}\n{data.decode()}")
                except Exception as e:
                    logger.warning(
                        "file_content_load_failed",
                        project_id=project_id,
                        file_id=file_id,
                        parsed_key=parsed_key,
                        error=str(e),
                    )
        return "\n\n---\n\n".join(contents)

    async def _invoke_design_agent(self, agent_input: AgentInput, task_id: str | None = None) -> dict[str, Any]:
        prompt = json.dumps({
            "command": agent_input.command,
            "stage": agent_input.stage,
            "context": agent_input.context,
            "selected_component_id": agent_input.selected_component_id,
        }, ensure_ascii=False)

        tm = self._task_manager
        token_count = 0

        def progress_callback(**kwargs: Any) -> None:
            nonlocal token_count
            if not task_id:
                return
            data = kwargs.get("data", "")
            if data:
                token_count += 1
                if token_count % 50 == 0:
                    tm.update_task(task_id, current_step=f"에이전트가 디자인을 작성 중... ({token_count} tokens)")

        loader = get_prompt_loader()
        if agent_input.stage in ("wireframe", "requirements"):
            stage_prompt = (await loader.get(WIREFRAME_SYSTEM) if loader else None) or WIREFRAME_SYSTEM_PROMPT
        else:
            stage_prompt = (await loader.get(DESIGNER_SYSTEM) if loader else None) or DESIGNER_SYSTEM_PROMPT

        model_slot = "wireframe" if agent_input.stage == "wireframe" else "designer"
        agent = Agent(
            model=self._get_model(model_slot),
            system_prompt=stage_prompt,
            callback_handler=progress_callback if task_id else None,
        )

        result_text = await self._invoke_agent(agent, prompt)

        try:
            start = result_text.index("{")
            end = result_text.rindex("}") + 1
            return cast(dict[str, Any], json.loads(result_text[start:end]))
        except (ValueError, json.JSONDecodeError):
            # Retry once with explicit JSON instruction
            logger.warning(
                "design_agent_json_parse_failed",
                stage=agent_input.stage,
                response_len=len(result_text),
                response_sample=result_text[:500],
            )
            retry_agent = Agent(
                model=self._get_model(model_slot),
                system_prompt=stage_prompt + "\n\nCRITICAL: You MUST return ONLY valid JSON. No markdown, no explanation, no code fences. Start with { and end with }.",
                callback_handler=None,
            )
            retry_text = await self._invoke_agent(retry_agent, prompt)
            try:
                start = retry_text.index("{")
                end = retry_text.rindex("}") + 1
                return cast(dict[str, Any], json.loads(retry_text[start:end]))
            except (ValueError, json.JSONDecodeError):
                logger.error(
                    "design_agent_json_retry_failed",
                    stage=agent_input.stage,
                    response_sample=retry_text[:500],
                )
                return {"raw_response": retry_text}

    async def _get_latest_snapshot(self, project_id: str, stage_id: str) -> dict[str, Any]:
        versions = await self._version_service.list_versions(project_id, stage_id, limit=1)
        if not versions["items"]:
            return {}
        snapshot_bytes = await self._version_service.get_snapshot(project_id, versions["items"][0].version_id)
        return cast(dict[str, Any], json.loads(snapshot_bytes))

    async def _update_stage_status(self, team_id: str, project_id: str, stage: str, status: StageStatus) -> None:
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        await self._db.update_item(
            table_name=PROJECTS_TABLE,
            key={"teamId": team_id, "sk": f"PROJECT#{project_id}"},
            update_expression="SET stageStatus.#stage = :status, updatedAt = :now",
            expression_values={":status": status, ":now": now},
            expression_names={"#stage": stage},
        )
