"""Jinja2 HTML report renderer. Spec Section 7.2."""

import os

from jinja2 import ChainableUndefined, Environment, FileSystemLoader

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

DEFAULTS = {
    "title": "RCA Report",
    "severity": "Medium",
    "summary": "",
    "generated_at": "",
    "findings": [],
    "recommendations": [],
    "alarm_history": {},
    "charts": {},
    "metrics": {},
    "timeline": [],
}


def render_report(report_type: str, data: dict) -> str:
    """report_type에 맞는 Jinja2 템플릿을 렌더링하여 HTML 문자열 반환."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=ChainableUndefined,
    )
    merged = {**DEFAULTS, **data}
    template_name = f"{report_type}-template.html"
    template = env.get_template(template_name)
    return template.render(**merged)
