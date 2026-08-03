"""Jinja2 HTML report renderer. Spec Section 7.2."""

import logging
import os

from jinja2 import (
    ChainableUndefined,
    Environment,
    FileSystemLoader,
    TemplateNotFound,
    select_autoescape,
)

logger = logging.getLogger(__name__)

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

# Only rca-template.html ships today; other report types fall back to it so
# render_report never raises TemplateNotFound for a documented type.
FALLBACK_TEMPLATE = "rca-template.html"

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
        autoescape=select_autoescape(["html", "xml"]),
    )
    merged = {**DEFAULTS, **data}
    template_name = f"{report_type}-template.html"
    try:
        template = env.get_template(template_name)
    except TemplateNotFound:
        logger.warning(
            "No template for report_type=%s; falling back to %s",
            report_type,
            FALLBACK_TEMPLATE,
        )
        template = env.get_template(FALLBACK_TEMPLATE)
    return template.render(**merged)
