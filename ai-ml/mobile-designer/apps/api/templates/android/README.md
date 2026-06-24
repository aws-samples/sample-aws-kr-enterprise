# Android Project Templates

This directory contains Jinja2 templates for Android project generation.

Currently, the code generator (`src/handoff/code_generator/generator.py`) generates
Gradle, Manifest, and Kotlin files inline for simplicity. Jinja2 templates will be
used when the project structure becomes more complex or requires user customization.

## Planned Templates
- `build.gradle.kts.j2` — App-level build configuration
- `settings.gradle.kts.j2` — Multi-module settings
- `AndroidManifest.xml.j2` — Manifest with dynamic package name
- `Screen.kt.j2` — Composable screen template
- `Theme.kt.j2` — Theme configuration template
