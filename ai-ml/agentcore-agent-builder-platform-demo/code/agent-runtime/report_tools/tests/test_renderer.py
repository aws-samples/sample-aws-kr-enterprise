import os
import sys

# Make `report_tools` importable when running pytest from code/agent-runtime/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from report_tools.renderer import DEFAULTS, render_report


def test_defaults_cover_every_rca_template_variable():
    # The RCA template references exactly these top-level variables.
    required = {
        "title", "generatedAt", "severity", "executiveSummary",
        "metrics", "incidents", "changes", "rootCause", "recommendation",
    }
    assert required.issubset(set(DEFAULTS.keys()))


def test_render_report_fills_defaults_for_missing_data():
    # With empty data, the RCA file template still renders without raising,
    # and the title default appears.
    html = render_report("rca", {})
    assert "<html" in html.lower()
    assert DEFAULTS["title"] in html


def test_render_report_uses_provided_data():
    html = render_report("rca", {"title": "My RCA", "executiveSummary": "All good"})
    assert "My RCA" in html
    assert "All good" in html


def test_render_report_with_inline_template_string():
    tpl = "<html><body><h1>{{ title }}</h1><p>{{ rootCause }}</p></body></html>"
    html = render_report("novel-type", {"title": "T", "rootCause": "boom"}, template_str=tpl)
    assert "<h1>T</h1>" in html
    assert "boom" in html


def test_inline_template_autoescapes_data():
    # Data values are HTML-escaped (autoescape on) — XSS defense.
    tpl = "<div>{{ executiveSummary }}</div>"
    html = render_report("x", {"executiveSummary": "<script>alert(1)</script>"}, template_str=tpl)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
