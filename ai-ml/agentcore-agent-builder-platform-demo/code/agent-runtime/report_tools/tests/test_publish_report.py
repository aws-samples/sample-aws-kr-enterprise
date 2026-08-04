import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def test_publish_report_renders_uploads_and_returns_cf_url(monkeypatch):
    os.environ["REPORT_BUCKET"] = "test-bucket"
    os.environ["REPORT_CF_DOMAIN"] = "d123.cloudfront.net"

    from report_tools import s3_uploader

    # Force module-level env vars to reflect the test values (they are read at import).
    s3_uploader.REPORT_BUCKET = "test-bucket"
    s3_uploader.REPORT_CF_DOMAIN = "d123.cloudfront.net"

    captured = {}

    class FakeS3:
        def put_object(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(s3_uploader, "_get_s3_client", lambda: FakeS3())

    # session_id comes from internal_tools._request_context
    import internal_tools
    internal_tools._request_context["session_id"] = "sess-abc"

    url = s3_uploader.publish_report("rca", {"title": "T"})

    assert url.startswith("https://d123.cloudfront.net/")
    assert "sess-abc" in captured["Key"]
    assert captured["Bucket"] == "test-bucket"
    assert captured["ContentType"] == "text/html"
    body = captured["Body"].decode("utf-8") if isinstance(captured["Body"], bytes) else captured["Body"]
    assert "T" in body


def test_generate_signed_url_removed():
    from report_tools import s3_uploader
    assert not hasattr(s3_uploader, "generate_signed_url")
