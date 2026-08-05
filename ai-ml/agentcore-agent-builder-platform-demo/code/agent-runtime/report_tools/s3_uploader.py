"""S3 report upload + Signed URL. Spec Section 7.3."""

import os
from datetime import datetime, timezone

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
# Read lazily via .get() so importing this module never crashes when the env
# is not yet populated; validate at call time with a clear error instead.
REPORT_BUCKET = os.environ.get("REPORT_BUCKET", "")
REPORT_CF_DOMAIN = os.environ.get("REPORT_CF_DOMAIN", "")

_s3_cache: dict = {}


def _require_report_env():
    missing = [
        n
        for n, v in (("REPORT_BUCKET", REPORT_BUCKET), ("REPORT_CF_DOMAIN", REPORT_CF_DOMAIN))
        if not v
    ]
    if missing:
        raise RuntimeError(
            f"Report generation requires env vars: {', '.join(missing)}. "
            "Set them on the report agent runtime (from the S3 reports bucket "
            "and CloudFront domain terraform outputs)."
        )


def _get_s3_client():
    if "client" not in _s3_cache:
        _s3_cache["client"] = boto3.client("s3", region_name=REGION)
    return _s3_cache["client"]


def upload_to_s3(
    session_id: str,
    report_type: str,
    html_content: str,
) -> str:
    """HTML 보고서를 S3에 업로드하고 presigned URL을 반환."""
    _require_report_env()
    s3 = _get_s3_client()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"{date_str}/{session_id}/{report_type}-report.html"

    s3.put_object(
        Bucket=REPORT_BUCKET,
        Key=key,
        Body=html_content.encode("utf-8"),
        ContentType="text/html",
        CacheControl="max-age=86400",
    )

    return f"https://{REPORT_CF_DOMAIN}/{key}"


def publish_report(report_type: str, data: dict, template_str: str = None) -> str:
    """리포트를 렌더링→S3 업로드하고 CloudFront URL을 반환하는 단일 도구.

    render_report → upload_to_s3를 내부에서 순차 실행하여 LLM 왕복을 1회로 줄인다.
    session_id는 런타임 request context에서 읽는다(LLM 인자가 아님).
    template_str가 주어지면 파일 템플릿이 없는 novel report_type에 대해 인라인
    템플릿을 렌더링한다.
    """
    from report_tools.renderer import render_report
    from internal_tools import _request_context

    session_id = _request_context.get("session_id", "") or "no-session"
    html = render_report(report_type, data, template_str=template_str)
    return upload_to_s3(session_id, report_type, html)
