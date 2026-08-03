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


def generate_signed_url(key: str, expires_in: int = 3600) -> str:
    """S3 Presigned URL 생성."""
    s3 = _get_s3_client()
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": REPORT_BUCKET, "Key": key},
        ExpiresIn=expires_in,
    )
    return url
