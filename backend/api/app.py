from fastapi import FastAPI, Form, HTTPException, Depends, Body, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response
import prometheus_client
from typing import Literal
from .auth import create_user as auth_create_user, authenticate, current_user
from .auth_google import router as google_router
from .metrics import (PrometheusMiddleware, api_queue_size, api_jobs_enqueued_total, api_media_uploads_total)
from . import jobs
from .jobs import REDIS_QUEUE

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

# Constantes de MinIO
MINIO_MEDIA_BUCKET = "espotifai-media"


@app.post("/media/upload")
def upload_media(
    file: UploadFile = File(...),
    user = Depends(current_user)):

    try:
        # 1. Generar un media_id único
        media_id = str(uuid.uuid4())
        user_id = user["id"]
        
        # Obtener extensión (ej: .mp4)
        file_ext = Path(file.filename).suffix
        
        # 2. Definir dónde se guardará en MinIO
        # Ej: uploads/user_123/media_abc/original.mp4
        object_name = f"uploads/{user_id}/{media_id}/original{file_ext}"
        
        # 3. Subir a MinIO
        # Necesitamos 'seek(0)' para resetear el puntero del stream
        file.file.seek(0) 
        jobs.upload_file_to_minio(
            bucket=MINIO_MEDIA_BUCKET,
            object_name=object_name,
            file_stream=file.file,
            file_length=file.size,
            content_type=file.content_type
        )
        
        # 4. Incrementar métrica
        api_media_uploads_total.inc()
        
        # 5. Crear entrada en Firestore
        media_entry = jobs.create_media_entry(
            user_id=user_id,
            original_filename=file.filename,
            source_bucket=MINIO_MEDIA_BUCKET,
            source_object=object_name,
            content_type=file.content_type
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
    (MODIFICADO) Encola un trabajo de conversión.
    """
    # 1. Validar que el 'media_id' existe (usando la función real)
    media_entry = jobs.get_media_entry(media_id)
    if not media_entry:
        raise HTTPException(status_code=404, detail="Media no encontrado")

    # 2. Validar propiedad
    if media_entry.get("user_id") != user['id']:
       raise HTTPException(status_code=403, detail="No autorizado para este media")

    # 3. Obtener detalles para el job
    source_bucket = media_entry.get("source_bucket")
    source_object = media_entry.get("source_object")
    output_bucket = source_bucket # Usamos el mismo bucket para salida

    if not source_bucket or not source_object:
        raise HTTPException(500, "Metadatos de media incompletos (sin bucket/object)")

    # 4. Encolar el trabajo
    try:
        job_id = jobs.enqueue_conversion_job(
            media_id=media_id,
            source_bucket=source_bucket,
            source_object=source_object,
            target=target,
            output_bucket=output_bucket
        )
        
        # 5. Actualizar métricas
        api_jobs_enqueued_total.labels(target_format=target).inc()
        
        return {
            "media_id": media_id,
            "job_id": job_id, 
            "status": "enqueued", 
            "target": target
        }
        
    except Exception as e:
        print(f"Error encolando: {e}")
        raise HTTPException(status_code=500, detail=f"Error al encolar trabajo")


@app.get("/jobs/{job_id}/status")
def get_job_status(job_id: str, user = Depends(current_user)):
    media_entry = jobs.get_media_by_job_id(job_id)
    
    if not media_entry:
        raise HTTPException(status_code=404, detail="Job ID no encontrado")

    # 2. Validar que el job pertenece al usuario
    if media_entry.get("user_id") != user['id']:
       raise HTTPException(status_code=403, detail="No autorizado para este Job ID")

    # 3. Retornar el estado (leyendo del mapa 'jobs')
    job_details = media_entry.get("jobs", {}).get(job_id)
    
    if not job_details:
        # Esto no debería pasar si get_media_by_job_id funcionó
        raise HTTPException(status_code=500, detail="Inconsistencia de datos")
        
    return {
        "job_id": job_id,
        "media_id": media_entry.get("id"),
        "status": job_details.get("status"),
        "target": job_details.get("target"),
        "details": job_details.get("details"), # Detalles (ej. error)
        "updated_at": job_details.get("updated_at")
    }

@app.get("/media/{media_id}/share")
def share_media(
    media_id: str, 
    job_id: Optional[str] = None, # (NUEVO) Param opcional
    user = Depends(current_user)
):
    """
    (IMPLEMENTADO) Genera una URL presignada para descargar un 
    resultado de conversión (ej. el .mp3 o el .m3u8).
    
    Si 'job_id' no se provee, busca el primer job completado.
    """
    
    # 1. Obtener media_entry de Firestore
    media_entry = jobs.get_media_entry(media_id)
    if not media_entry:
        raise HTTPException(status_code=404, detail="Media no encontrado")

    # 2. Validar que pertenece al usuario
    if media_entry.get("user_id") != user['id']:
       raise HTTPException(status_code=403, detail="No autorizado")

    # 3. Encontrar el job_details que queremos compartir
    all_jobs = media_entry.get("jobs", {})
    job_details = None
    target_job_id = job_id # El job que buscamos

    if target_job_id:
        # El usuario especificó un job_id
        if target_job_id not in all_jobs:
            raise HTTPException(status_code=404, detail="Job ID no encontrado en este media")
        job_details = all_jobs[target_job_id]
    else:
        # El usuario no especificó, buscar el primero que esté "done"
        for j_id, j_details in all_jobs.items():
            if j_details.get("status") == "done":
                job_details = j_details
                target_job_id = j_id # Encontramos el ID
                break

    if not job_details:
        raise HTTPException(status_code=404, detail="No hay conversiones completadas para compartir")

    if job_details.get("status") != "done":
        raise HTTPException(status_code=400, detail=f"El Job {target_job_id} no está completado (estado: {job_details.get('status')})")

    # 4. Determinar el 'object_name' en MinIO
    
    # El worker (Integrante B) debe guardar la ruta de salida.
    # Asumiremos la estructura basada en la lógica del worker
    
    bucket = media_entry.get("source_bucket")
    prefix = job_details.get("output_prefix") # Ej: "media/media_id/conversions/job_id/"
    target = job_details.get("target")

    if not bucket or not prefix or not target:
         raise HTTPException(status_code=500, detail="Metadatos del job incompletos")

    object_name = ""
    if target == "mp3":
        object_name = f"{prefix}{target_job_id}.mp3"
    elif target == "mp4":
        object_name = f"{prefix}{target_job_id}.mp4"
    elif target == "hls":
        # El archivo principal de HLS es el playlist
        object_name = f"{prefix}index.m3u8"
    else:
        raise HTTPException(status_code=500, detail=f"Target desconocido: {target}")

    # 5. Llamar a jobs.get_presigned_url_for_download()
    try:
        url = jobs.get_presigned_url_for_download(
            bucket=bucket,
            object_name=object_name
        )
        # 6. Retornar la URL
        return {"url": url, "target": target, "job_id": target_job_id}
        
    except Exception as e:
        print(f"Error generando URL presignada: {e}")
        raise HTTPException(status_code=500, detail="No se pudo generar la URL de descarga")