from functools import lru_cache

from src.common.config import Settings, get_settings
from src.common.db.client import DynamoDBClient
from src.common.s3.client import S3Client


@lru_cache
def get_db() -> DynamoDBClient:
    return DynamoDBClient(get_settings())


@lru_cache
def get_s3() -> S3Client:
    return S3Client(get_settings())


def get_settings_dep() -> Settings:
    return get_settings()
