import os
from pathlib import Path
from minio import Minio
from minio.error import S3Error

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"


def get_minio_client() -> Minio:
    return Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE,
    )


def download_object(bucket: str, object_name: str, dest_path: str) -> str:
    client = get_minio_client()
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    client.fget_object(bucket, object_name, dest_path)
    return dest_path


def upload_object(bucket: str, object_name: str, file_path: str, content_type: str = "application/octet-stream") -> None:
    client = get_minio_client()
    client.fput_object(bucket, object_name, file_path, content_type=content_type)