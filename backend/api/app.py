from fastapi import FastAPI, Form, HTTPException, Depends, Body, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response
import prometheus_client
import uuid
from pathlib import Path
from minio.error import S3Error
from typing import Literal, Optional, Tuple, List
from google.cloud import firestore
from fastapi.responses import StreamingResponse
import psutil
from datetime import datetime
from pydantic import BaseModel  # <--- Asegúrate de tener esto

from .auth import create_user as auth_create_user, authenticate, current_user
from .auth_google import router as google_router
from .metrics import (
    PrometheusMiddleware,
    api_queue_size,
    api_jobs_enqueued_total,
    api_media_uploads_total,
)
from . import jobs
from .jobs import REDIS_QUEUE
from .firebase_db import get_db  # <--- Asegúrate de tener esto





app = FastAPI(title="Auth con Firestore + JWT")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

app.add_middleware(PrometheusMiddleware)

@app.get("/metrics")
def metrics():
    try:
        r = jobs.get_redis_client()
        q_len = r.llen(REDIS_QUEUE)
        api_queue_size.labels(queue_name=REDIS_QUEUE).set(q_len)
    except Exception:
        api_queue_size.labels(queue_name=REDIS_QUEUE).set(0)
        
    return Response(
        media_type="text/plain",
        content=prometheus_client.generate_latest(),
    )


app.include_router(google_router)

@app.get("/monitor/summary")
def monitor_summary(user = Depends(current_user)):
    """
    Resumen ligero para el dashboard de monitoreo (JSON).
    No devuelve todas las métricas Prometheus, solo lo que nos interesa mostrar.
    """
    # CPU y RAM de la API (este proceso)
    cpu = psutil.cpu_percent(interval=0)
    mem = psutil.virtual_memory().percent

    # Tamaño de la cola de trabajos en Redis
    try:
        r = jobs.get_redis_client()
        q_len = r.llen(REDIS_QUEUE)
    except Exception:
        q_len = None

    # Total de usuarios registrados en Firestore (si falla, lo dejamos en None)
    db = get_db()
    try:
        users_ref = db.collection("users")
        total_users = len(list(users_ref.stream()))
    except Exception:
        total_users = None

    username = user.get("username") or user.get("email") or "desconocido"

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "api": {
            "status": "online",
            "cpu_percent": cpu,
            "memory_percent": mem,
        },
        "queue": {
            "name": REDIS_QUEUE,
            "length": q_len,
        },
        "sessions": {
            # Para el proyecto, basta con reportar la sesión actual y un estimado
            "current_user": {
                "id": user["id"],
                "username": username,
            },
            "active_sessions_estimate": 1,
            "total_users": total_users,
        },
        "nodes": [
            {"id": "api",       "role": "API / gateway",          "status": "online"},
            {"id": "worker_a",  "role": "worker de conversión",   "status": "online"},
            {"id": "worker_b",  "role": "worker de conversión",   "status": "online"},
        ],
    }


@app.post("/auth/register")
def register(username: str = Form(...), password: str = Form(...)):
    u = auth_create_user(username, password)
    return {"id": u["id"], "username": u["username"]}

@app.post("/auth/token")
def token(username: str = Form(...), password: str = Form(...)):
    tok = authenticate(username, password)
    if not tok:
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    return {"access_token": tok, "token_type": "bearer"}

@app.get("/me")
def me(user = Depends(current_user)):
    return {"id": user["id"], "username": user["username"]}


SUPPORTED_TARGETS = Literal["mp3", "mp4", "hls"]
SUPPORTED_AUDIO_INPUT_EXT = {".mp3", ".wav", ".flac", ".ogg"}
SUPPORTED_VIDEO_INPUT_EXT = {".mp4", ".mkv", ".mov"}
SUPPORTED_INPUT_EXT = SUPPORTED_AUDIO_INPUT_EXT | SUPPORTED_VIDEO_INPUT_EXT

# Constantes de MinIO
MINIO_MEDIA_BUCKET = "espotifai-media"


class ShareWithUserRequest(BaseModel):
    username_to_share: str  
    job_id: str     

class UserPublic(BaseModel):
    id: str
    username: str
        


@app.post("/auth/logout")
def logout(user = Depends(current_user)):
    return {"detail": "Sesión cerrada correctamente", "user": user["username"]}



@app.get("/users", response_model=List[UserPublic])
def list_users(user=Depends(current_user)):
    """
    Devuelve la lista de usuarios registrados (se excluye al usuario actual).
    """
    db = get_db()
    users_ref = db.collection("users")
    docs = users_ref.stream()

    result: List[UserPublic] = []
    for doc in docs:
        data = doc.to_dict() or {}
        username = data.get("username")
        if not username:
            continue
        # Excluir al usuario que está logueado
        if doc.id == user["id"]:
            continue
        result.append(UserPublic(id=doc.id, username=username))

    return result


@app.post("/media/upload")
def upload_media(
    file: UploadFile = File(...),
    user = Depends(current_user)):

    try:
        # 1. Generar un media_id único
        media_id = str(uuid.uuid4())
        user_id = user["id"]
        
        # Extensión en minúscula
        file_ext = Path(file.filename).suffix.lower()

        # 1.1 Validar que el formato sea soportado
        if file_ext not in SUPPORTED_INPUT_EXT:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Formato no soportado: {file_ext}. "
                    f"Audio permitidos: {sorted(SUPPORTED_AUDIO_INPUT_EXT)}. "
                    f"Video permitidos: {sorted(SUPPORTED_VIDEO_INPUT_EXT)}."
                ),
            )

        # 1.2 Calcular tamaño original del archivo
        file.file.seek(0, 2)              # ir al final
        original_size = file.file.tell()  # bytes
        file.file.seek(0)                 # volver al inicio

        # 2. Definir dónde se guardará en MinIO
        # Ej: uploads/user_123/media_abc/original.mp4
        object_name = f"uploads/{user_id}/{media_id}/original{file_ext}"
        
        # 3. Subir a MinIO
        jobs.upload_file_to_minio(
            bucket=MINIO_MEDIA_BUCKET,
            object_name=object_name,
            file_stream=file.file,
            file_length=original_size,           # usamos el tamaño calculado
            content_type=file.content_type
        )
        
        # 4. Incrementar métrica
        api_media_uploads_total.inc()
        
        # 5. Crear entrada en Firestore (ahora con extensión y tamaño)
        media_entry = jobs.create_media_entry(
            user_id=user_id,
            original_filename=file.filename,
            source_bucket=MINIO_MEDIA_BUCKET,
            source_object=object_name,
            content_type=file.content_type,
            original_extension=file_ext,
            original_size_bytes=original_size,
        )
        
        print(f"Usuario {user['username']} subió {file.filename} como {media_id}")
        
        # 6. Retornar el nuevo media_id
        return media_entry
        
    except S3Error as e:
        print(f"Error de MinIO: {e}")
        raise HTTPException(status_code=500, detail="Error al subir archivo a MinIO")
    except Exception as e:
        print(f"Error en upload: {e}")
        raise HTTPException(status_code=500, detail=f"Error interno: {e}")


@app.post("/media/{media_id}/convert")
def convert_media(
    media_id: str,
    target: SUPPORTED_TARGETS = Body(..., embed=True, description="Formato: mp3, mp4, o hls"),
    user = Depends(current_user)
):
    """
    Encola un trabajo de conversión y deja todo lo necesario en Firestore.
    """
    # 1) Validar media y propiedad
    media_entry = jobs.get_media_entry(media_id)
    if not media_entry:
        raise HTTPException(status_code=404, detail="Media no encontrado")
    if media_entry.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="No autorizado para este media")

    # 2) Metadatos de origen y bucket de salida
    source_bucket = media_entry.get("source_bucket")
    source_object = media_entry.get("source_object")
    if not source_bucket or not source_object:
        raise HTTPException(500, "Metadatos de media incompletos (sin bucket/object)")
    output_bucket = source_bucket  # usamos el mismo bucket para la salida

    # 3) Identidad del job y prefijo de salida
    job_id = str(uuid.uuid4())
    output_prefix = f"converted/{media_id}/{job_id}"  # mp3/mp4 => archivo; hls => carpeta con index.m3u8


    # 5) Encolar en Redis con toda la info que el worker necesita
    try:
        jobs.enqueue_conversion_job(
            media_id=media_id,
            job_id=job_id,              # <- usar el MISMO job_id
            target=target,
            source_bucket=source_bucket,
            source_object=source_object,
            output_bucket=output_bucket,
            output_prefix=output_prefix  # <- importantísimo
        )

        # 6) Métricas (si ya definiste el collector)
        api_jobs_enqueued_total.labels(target_format=target).inc()

        return {
            "media_id": media_id,
            "job_id": job_id,
            "status": "enqueued",
            "target": target
        }

    except Exception as e:
        print(f"[convert_media] Error encolando {job_id}: {e}")
        raise HTTPException(status_code=500, detail="Error al encolar trabajo")

@app.get("/jobs/{job_id}/status")
def job_status(
    job_id: str,
    media_id: Optional[str] = None,
    user = Depends(current_user)
):
    # Permite pasar media_id para evitar la búsqueda por array_contains
    media = jobs.get_media_entry(media_id) if media_id else jobs.get_media_by_job_id(job_id)
    if not media:
        raise HTTPException(404, "No se encontró media para este job")

    if media.get("user_id") != user["id"]:
        raise HTTPException(403, "No autorizado")

    job = (media.get("jobs") or {}).get(job_id)
    if not job:
        raise HTTPException(404, "Job no existe en este media")

    # Respuesta consistente
    return {
        "job_id": job_id,
        "media_id": media["id"],
        **job
    }


def _resolve_media_output_for_job(media_entry: dict, job_id: Optional[str]) -> Tuple[str, str, str, str]:
    """
    A partir del media_entry de Firestore y (opcionalmente) un job_id,
    devuelve (bucket, object_name, target, job_id_resuelto) del resultado listo.
    """
    all_jobs = media_entry.get("jobs", {}) or {}
    job_details = None
    target_job_id = job_id

    if target_job_id:
        job_details = all_jobs.get(target_job_id)
        if not job_details:
            raise HTTPException(
                status_code=404,
                detail=f"Job {target_job_id} no encontrado para este media"
            )
    else:
        # Buscar el primer job con status "done"
        for j_id, j_details in all_jobs.items():
            if j_details.get("status") == "done":
                job_details = j_details
                target_job_id = j_id
                break

    if not job_details:
        raise HTTPException(
            status_code=404,
            detail="No hay conversiones completadas para compartir"
        )

    if job_details.get("status") != "done":
        raise HTTPException(
            status_code=400,
            detail=f"El Job {target_job_id} no está completado (estado: {job_details.get('status')})"
        )

    # En Firestore no se guarda output_bucket, así que usamos el source_bucket del media
    bucket = media_entry.get("source_bucket")
    target = job_details.get("target")
    output_prefix = job_details.get("output_prefix")

    if not bucket or not target or not output_prefix:
        raise HTTPException(
            status_code=500,
            detail="No hay información suficiente de salida para este job"
        )

    # Armar el nombre del objeto según el target
    if target in ("mp3", "mp4"):
        object_name = f"{output_prefix}/output.{target}"
    elif target == "hls":
        object_name = f"{output_prefix}/index.m3u8"
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Target desconocido: {target}"
        )

    return bucket, object_name, target, target_job_id

@app.get("/media/{media_id}/share")
def share_media(
    media_id: str,
    job_id: Optional[str] = None,
    user = Depends(current_user)
):
    """
    Genera una URL presignada para descargar un resultado de conversión.
    Si 'job_id' no se provee, busca el primer job completado.
    """
    # 1. Obtener media_entry de Firestore
    media_entry = jobs.get_media_entry(media_id)
    if not media_entry:
        raise HTTPException(status_code=404, detail="Media no encontrado")

    # 2. Validar que pertenece al usuario
    if media_entry.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="No autorizado")

    # 3–4. Resolver bucket, objeto y target usando el helper
    bucket, object_name, target, target_job_id = _resolve_media_output_for_job(
        media_entry,
        job_id
    )

    # 5. Generar URL presignada (para compartir/copiar)
    try:
        url = jobs.get_presigned_url_for_download(
            bucket=bucket,
            object_name=object_name,
        )
        return {"url": url, "target": target, "job_id": target_job_id}
    except Exception as e:
        print(f"Error generando URL presignada: {e}")
        raise HTTPException(
            status_code=500,
            detail="No se pudo generar la URL de descarga"
        )


@app.post("/media/{media_id}/share-with-user")
def share_with_user(
    media_id: str,
    payload: ShareWithUserRequest,
    user = Depends(current_user),
):
    """
    Comparte un media convertido con otro usuario del sistema, usando su username.
    - Verifica que el media exista.
    - Verifica que el media pertenezca al usuario que está compartiendo.
    - Verifica que el job_id indicado pertenezca a ese media.
    - Agrega al destinatario en el array 'shared_with' y en un array 'shares'.
    """
    db = get_db()

    # 1. Obtener el documento de media
    media_ref = db.collection("media").document(media_id)
    media_doc = media_ref.get()
    if not media_doc.exists:
        raise HTTPException(status_code=404, detail="Media no existe")

    media = media_doc.to_dict()

    # 2. Validar propietario
    owner_id = media.get("user_id")
    if owner_id != user["id"]:
        raise HTTPException(
            status_code=403,
            detail="No puedes compartir un archivo que no es tuyo",
        )

    # 3. Buscar usuario destino por username
    username_to_share = payload.username_to_share.strip()
    users_ref = db.collection("users")
    target_stream = users_ref.where("username", "==", username_to_share).limit(1).stream()
    target_doc = next(target_stream, None)

    if not target_doc:
        raise HTTPException(status_code=404, detail="Usuario destino no existe")

    target_user_id = target_doc.id

    # 4. Validar que el job exista dentro del media
    job_id = payload.job_id

        # Usamos la estructura real que guarda la API:
        # - job_ids: lista de IDs de jobs (strings)
        # - jobs: mapa job_id -> detalles

    job_ids = media.get("job_ids") or []
    jobs_map = media.get("jobs") or {}

    # Comprobar que el job_id está registrado en el media
    if job_id not in job_ids and (not isinstance(jobs_map, dict) or job_id not in jobs_map):
        raise HTTPException(
            status_code=400,
            detail="El job_id indicado no pertenece a este media",
        )

    # 5. Actualizar campos de sharing en el documento
    shared_with = media.get("shared_with") or []
    if target_user_id not in shared_with:
        shared_with.append(target_user_id)

    # Lista de shares detallados (opcional, pero útil)
    shares = media.get("shares") or []
    share_entry = {"user_id": target_user_id, "job_id": job_id}
    if share_entry not in shares:
        shares.append(share_entry)

    media_ref.update(
        {
            "shared_with": shared_with,
            "shares": shares,
        }
    )

    return {
        "detail": "Archivo compartido correctamente",
        "media_id": media_id,
        "job_id": job_id,
        "shared_with_user_id": target_user_id,
        "username_to_share": username_to_share,
    }



@app.get("/media/shared-with-me")
def media_shared_with_me(user = Depends(current_user)):
    """
    Devuelve la lista de medias que han sido compartidos con el usuario actual.
    Busca en Firestore documentos 'media' donde 'shared_with' contenga el id del usuario.
    """
    db = get_db()

    media_ref = db.collection("media")
    query = media_ref.where("shared_with", "array_contains", user["id"])
    docs = list(query.stream())

    items = []
    for d in docs:
        data = d.to_dict() or {}
        items.append(
            {
                "media_id": d.id,
                "owner_id": data.get("user_id"),
                "original_filename": data.get("original_filename"),
                "jobs": data.get("jobs", []),
                "shared_with": data.get("shared_with", []),
                "shares": data.get("shares", []),
            }
        )

    return {"items": items}



@app.get("/media/{media_id}/stream")
def stream_media(
    media_id: str,
    job_id: Optional[str] = None,
):
    """
    Devuelve un streaming del archivo convertido (mp3/mp4)
    para que el frontend lo use en el <audio> sin necesitar Authorization.
    Es equivalente a una URL presignada: quien tenga media_id + job_id puede acceder.
    """
    # 1. Obtener media_entry
    media_entry = jobs.get_media_entry(media_id)
    if not media_entry:
        raise HTTPException(status_code=404, detail="Media no encontrado")

    # 2. Resolver bucket/objeto usando el helper
    bucket, object_name, target, target_job_id = _resolve_media_output_for_job(
        media_entry,
        job_id
    )

    # 3. Obtener objeto desde MinIO
    client = jobs.get_minio_client()
    try:
        obj = client.get_object(bucket, object_name)
    except S3Error as e:
        print(f"Error al obtener objeto de MinIO: {e}")
        raise HTTPException(
            status_code=500,
            detail="No se pudo obtener el archivo desde MinIO"
        )

    # 4. Tipo de contenido según target
    if target == "mp3":
        media_type = "audio/mpeg"
    elif target == "mp4":
        media_type = "video/mp4"
    else:
        media_type = "application/octet-stream"

    # 5. Devolver stream en chunks de 32KB
    return StreamingResponse(obj.stream(32 * 1024), media_type=media_type)

