# backend/api/jobs.py
import os
import json
import uuid
import redis
import datetime
from typing import Optional, Dict, Any, BinaryIO
from minio import Minio
from minio.error import S3Error
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

# Importar la DB de Firestore
from .firebase_db import db

# --- Configuración de Redis (SIN CAMBIOS) ---
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_QUEUE = os.getenv("REDIS_QUEUE", "convert")

_redis_client = None

def get_redis_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB
        )
        _redis_client.ping()
    return _redis_client

# --- Configuración de MinIO ---
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"

_minio_client = None

def get_minio_client() -> Minio:
    global _minio_client
    if _minio_client is None:
        _minio_client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE,
        )
    return _minio_client

# --- Lógica de Almacenamiento (MinIO) ---

def upload_file_to_minio(
    bucket: str, 
    object_name: str, 
    file_stream: BinaryIO, 
    file_length: int,
    content_type: str
) -> None:
    # ... (Esta función está implementada en el paso anterior - SIN CAMBIOS)
    client = get_minio_client()
    found = client.bucket_exists(bucket)
    if not found:
        client.make_bucket(bucket)
    client.put_object(
        bucket_name=bucket,
        object_name=object_name,
        data=file_stream,
        length=file_length,
        content_type=content_type
    )

def get_presigned_url_for_download(bucket: str, object_name: str, expires_in_hours: int = 1) -> str:
    """
    Genera una URL firmada temporal para descargar un objeto en MinIO.
    """
    client = get_minio_client()
    try:
        url = client.presigned_get_object(
            bucket_name=bucket,
            object_name=object_name,
            expires=datetime.timedelta(hours=expires_in_hours),
        )
        return url
    except S3Error as e:
        print(f"Error al generar URL presignada: {e}")
        raise


# --- Lógica de Media (Firestore) ---

def create_media_entry(
    user_id: str, 
    original_filename: str, 
    source_bucket: str, 
    source_object: str,
    content_type: str
) -> Dict[str, Any]:
    """
    (MODIFICADO) Crea un nuevo documento 'media', añadiendo el array 'job_ids'.
    """
    media_ref = db().collection("media").document()
    media_data = {
        "user_id": user_id,
        "original_filename": original_filename,
        "source_bucket": source_bucket,
        "source_object": source_object,
        "content_type": content_type,
        "created_at": datetime.datetime.utcnow(),
        "status": "uploaded",
        "jobs": {}, # Objeto para guardar detalles
        "job_ids": [] # (NUEVO) Array para búsquedas
    }
    media_ref.set(media_data)
    
    media_data["id"] = media_ref.id
    return media_data

def get_media_entry(media_id: str) -> Optional[Dict[str, Any]]:
    # ... (Esta función está implementada en el paso anterior - SIN CAMBIOS)
    doc = db().collection("media").document(media_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    data["id"] = doc.id
    return data

def get_media_by_job_id(job_id: str) -> Optional[Dict[str, Any]]:
    try:
        docs = (db()
            .collection("media")
            .where(filter=FieldFilter("job_ids", "array_contains", job_id))
            .limit(1)
            .stream())
        for d in docs:
            data = d.to_dict() or {}
            data["id"] = d.id
            return data
        return None
    except Exception as e:
        # Loguea y devuelve None para que el endpoint responda 404 y no 500
        print(f"[get_media_by_job_id] error: {e}")
        return None


def update_job_status(media_id: str, job_id: str, status: str, details: Dict[str, Any] = None):

    media_ref = db().collection("media").document(media_id)
    
    update_data = {
        f"jobs.{job_id}.status": status,
        f"jobs.{job_id}.details": details or {},
        f"jobs.{job_id}.updated_at": firestore.SERVER_TIMESTAMP,
    }
    
    # Marcar el estado general del 'media'
    if status == "done":
        update_data["status"] = "ready"
        # (El worker también debería añadir la ruta de salida aquí)
        # update_data[f"jobs.{job_id}.output_path"] = "..."
    elif status == "failed":
        update_data["status"] = "error"
        
    media_ref.update(update_data)
    print(f"Estado de job {job_id} actualizado a {status}")
    pass

# --- Lógica de Jobs (Redis) ---

def enqueue_conversion_job(
    *,
    media_id: str,
    job_id: str,
    target: str,                 # "mp3" | "mp4" | "hls"
    source_bucket: str,
    source_object: str,
    output_bucket: str,
    output_prefix: str
) -> None:
    """
    Publica en la cola (Redis) el JSON que el worker necesita.
    NO generar job_id aquí: viene del API para que coincida en todo lado.
    """
    payload = {
        "media_id": media_id,
        "job_id": job_id,
        "target": target,
        "source_bucket": source_bucket,
        "source_object": source_object,
        "output_bucket": output_bucket,
        "output_prefix": output_prefix,
    }

    # 1) Encolar en Redis
    r = get_redis_client()
    r.rpush(REDIS_QUEUE, json.dumps(payload))

    # 2) Guardar/mergear estado inicial del job en Firestore
    media_ref = db().collection("media").document(media_id)
    media_ref.set({
        "status": "processing",  
        "job_ids": firestore.ArrayUnion([job_id]),
        f"jobs.{job_id}": {
            "target": target,
            "status": "enqueued",
            "output_prefix": output_prefix,
            "enqueued_at": firestore.SERVER_TIMESTAMP,   
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    }, merge=True)
