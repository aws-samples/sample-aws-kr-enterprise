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
# render_report never raises TemplateNotFound for a documented type. The
# fallback template is report-type-neutral (its heading/footer come from
# reportTypeLabel and its RCA-specific sections render only when populated), so
# a non-RCA type is never mislabeled as an RCA report.
FALLBACK_TEMPLATE = "rca-template.html"

# Human-readable labels used for the report title/footer so a report is never
# mislabeled by the shared fallback template.
REPORT_TYPE_LABELS = {
    "rca": "Automated RCA Report",
    "incident": "Incident Report",
    "health-check": "Health Check Report",
    "daily-summary": "Daily Summary Report",
    "security-audit": "Security Audit Report",
}


def _report_type_label(report_type: str) -> str:
    if report_type in REPORT_TYPE_LABELS:
        return REPORT_TYPE_LABELS[report_type]
    pretty = report_type.replace("-", " ").replace("_", " ").strip().title()
    return f"{pretty} Report" if pretty else "Report"

DEFAULTS = {
    "title": "RCA Report",
    "generatedAt": "",
    "severity": "Medium",
    "executiveSummary": "",
    "metrics": {},        # name -> {value, trend, period}
    "incidents": [],      # [{id, title, severity}]
    "changes": [],        # [{time, type, user, detail}]
    "rootCause": "",
    "recommendation": "",
}


def render_report(report_type: str, data: dict, template_str: str | None = None) -> str:
    """report_type에 맞는 HTML을 렌더링하여 문자열 반환.

    template_str가 주어지면 파일 템플릿 대신 인라인 템플릿 문자열을 렌더링한다
    (파일 템플릿이 없는 novel report_type용). 두 경로 모두 동일한 autoescape /
    ChainableUndefined 환경을 사용한다.
    """
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=ChainableUndefined,
        autoescape=select_autoescape(["html", "xml"]),
    )
    label = _report_type_label(report_type)
    merged = {**DEFAULTS, **data}
    # Give the shared fallback template a correct, per-type label so a non-RCA
    # report is not rendered as an "Automated RCA Report". A caller-supplied
    # reportTypeLabel (or an inline template_str) always wins.
    merged.setdefault("reportTypeLabel", label)
    # DEFAULTS["title"] is RCA-specific; if the caller did not supply a title,
    # fall back to the per-type label rather than "RCA Report".
    if not data.get("title"):
        merged["title"] = label

    if template_str is not None:
        template = env.from_string(template_str)
        return template.render(**merged)

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
