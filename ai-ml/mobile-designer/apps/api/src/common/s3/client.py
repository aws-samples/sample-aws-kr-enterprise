from typing import Any, cast

import aioboto3
import structlog
from botocore.config import Config
from botocore.exceptions import ClientError

from src.common.config import Settings

logger = structlog.get_logger()


class S3Client:
    def __init__(self, settings: Settings) -> None:
        self._session = aioboto3.Session()
        self._settings = settings
        self._bucket = settings.s3_bucket_name
        self._endpoint_url = settings.s3_endpoint_url
        self._region = settings.aws_region

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "region_name": self._region,
            "config": Config(s3={"addressing_style": "virtual"}),
        }
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url
        return kwargs

    async def generate_presigned_upload_url(
        self, key: str, content_type: str, max_size_bytes: int
    ) -> dict[str, Any]:
        async with self._session.client("s3", **self._client_kwargs()) as client:
            url = await client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": key,
                    "ContentType": content_type,
                },
                ExpiresIn=self._settings.s3_presigned_url_expiry,
            )
            return {
                "url": url,
                "key": key,
                "fields": {
                    "Content-Type": content_type,
                },
                "max_size_bytes": max_size_bytes,
            }

    async def generate_presigned_download_url(self, key: str, filename: str | None = None) -> str:
        params: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        async with self._session.client("s3", **self._client_kwargs()) as client:
            return cast(
                str,
                await client.generate_presigned_url(
                    "get_object",
                    Params=params,
                    ExpiresIn=self._settings.s3_presigned_url_expiry,
                ),
            )

    async def put_object(self, key: str, body: bytes, content_type: str = "application/octet-stream") -> None:
        async with self._session.client("s3", **self._client_kwargs()) as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
            )

    async def get_object(self, key: str) -> bytes:
        async with self._session.client("s3", **self._client_kwargs()) as client:
            response = await client.get_object(Bucket=self._bucket, Key=key)
            return cast(bytes, await response["Body"].read())

    async def delete_object(self, key: str) -> None:
        async with self._session.client("s3", **self._client_kwargs()) as client:
            await client.delete_object(Bucket=self._bucket, Key=key)

    async def delete_objects(self, keys: list[str]) -> None:
        if not keys:
            return
        async with self._session.client("s3", **self._client_kwargs()) as client:
            objects = [{"Key": k} for k in keys]
            for i in range(0, len(objects), 1000):
                batch = objects[i : i + 1000]
                await client.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": batch},
                )

    async def list_objects(self, prefix: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        async with self._session.client("s3", **self._client_kwargs()) as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    results.append(obj)
        return results

    async def head_object(self, key: str) -> dict[str, Any] | None:
        """Return object metadata, or None if it does not exist.

        Only a genuine 404/NoSuchKey maps to None — other errors (permissions,
        throttling, outage) are logged and re-raised so callers don't mistake a
        transient failure for a missing object.
        """
        try:
            async with self._session.client("s3", **self._client_kwargs()) as client:
                return cast(dict[str, Any], await client.head_object(Bucket=self._bucket, Key=key))
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            logger.error("s3_head_object_error", key=key, error_code=code, error=str(e))
            raise
