"""S3 report upload + CloudFront Signed URL. Spec Section 7.3.

The reports CloudFront distribution is NOT public: its default cache behavior is
gated by a trusted key group (see iac/modules/cdn/main.tf, REPORT_URL contract).
The only read path is therefore a CloudFront signed URL with a short expiry.
This module signs every returned URL with the RSA private key stored in Secrets
Manager (the matching public key is uploaded to CloudFront by terraform). It
never returns an unsigned URL.
"""

import functools
import json
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.signers import CloudFrontSigner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

REGION = os.environ.get("AWS_REGION", "us-east-1")
# Read lazily via .get() so importing this module never crashes when the env
# is not yet populated; validate at call time with a clear error instead.
REPORT_BUCKET = os.environ.get("REPORT_BUCKET", "")
REPORT_CF_DOMAIN = os.environ.get("REPORT_CF_DOMAIN", "")
# CloudFront signed-URL provisioning (REPORT_URL contract). The public key is
# uploaded to CloudFront / added to the trusted key group by terraform; the
# private key PEM is stored in Secrets Manager. Both are injected as env on the
# report agent runtime from the cdn module outputs.
REPORT_CF_KEY_PAIR_ID = os.environ.get("REPORT_CF_KEY_PAIR_ID", "")
REPORT_CF_PRIVATE_KEY_SECRET = os.environ.get("REPORT_CF_PRIVATE_KEY_SECRET", "")
# Signed-URL lifetime; short by default so shared links expire.
REPORT_URL_EXPIRY_SECONDS = int(os.environ.get("REPORT_URL_EXPIRY_SECONDS", "3600"))

_s3_cache: dict = {}


def _require_report_env():
    missing = [
        n
        for n, v in (
            ("REPORT_BUCKET", REPORT_BUCKET),
            ("REPORT_CF_DOMAIN", REPORT_CF_DOMAIN),
            ("REPORT_CF_KEY_PAIR_ID", REPORT_CF_KEY_PAIR_ID),
            ("REPORT_CF_PRIVATE_KEY_SECRET", REPORT_CF_PRIVATE_KEY_SECRET),
        )
        if not v
    ]
    if missing:
        raise RuntimeError(
            f"Report generation requires env vars: {', '.join(missing)}. "
            "Set them on the report agent runtime (from the S3 reports bucket, "
            "CloudFront domain, trusted CloudFront public-key id, and the "
            "Secrets Manager secret holding the CloudFront private key — all "
            "from the cdn module terraform outputs)."
        )


def _get_s3_client():
    if "client" not in _s3_cache:
        _s3_cache["client"] = boto3.client("s3", region_name=REGION)
    return _s3_cache["client"]


@functools.lru_cache(maxsize=1)
def _load_private_key():
    """Fetch the CloudFront signing private key (PEM) from Secrets Manager."""
    sm = boto3.client("secretsmanager", region_name=REGION)
    resp = sm.get_secret_value(SecretId=REPORT_CF_PRIVATE_KEY_SECRET)
    secret = resp.get("SecretString")
    if secret is None:
        secret = resp["SecretBinary"].decode("utf-8")
    # Allow the secret to be either a raw PEM or a JSON object {"privateKey": ...}.
    pem = secret
    stripped = secret.lstrip()
    if stripped.startswith("{"):
        obj = json.loads(secret)
        pem = obj.get("privateKey") or obj.get("private_key") or obj.get("pem")
        if not pem:
            raise RuntimeError(
                "CloudFront private-key secret JSON has no 'privateKey' field."
            )
    return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)


def _rsa_signer(message: bytes) -> bytes:
    return _load_private_key().sign(message, padding.PKCS1v15(), hashes.SHA1())


def _get_cf_signer() -> CloudFrontSigner:
    if "cf_signer" not in _s3_cache:
        _s3_cache["cf_signer"] = CloudFrontSigner(REPORT_CF_KEY_PAIR_ID, _rsa_signer)
    return _s3_cache["cf_signer"]


def upload_to_s3(
    session_id: str,
    report_type: str,
    html_content: str,
) -> str:
    """HTML 보고서를 S3에 업로드하고 만료 있는 CloudFront signed URL을 반환."""
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

    url = f"https://{REPORT_CF_DOMAIN}/{key}"
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=REPORT_URL_EXPIRY_SECONDS)
    return _get_cf_signer().generate_presigned_url(url, date_less_than=expires_at)


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
