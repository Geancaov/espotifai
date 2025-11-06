import os
from typing import Optional, Dict, Any
from google.cloud import firestore
# (Opcional) para filtros sin warning:
# from google.cloud.firestore_v1.base_query import FieldFilter

PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID")
_db = None

def db() -> firestore.Client:
    """Cliente cacheado de Firestore (compatible con tu código actual)."""
    global _db
    if _db is None:
        _db = firestore.Client(project=PROJECT_ID) if PROJECT_ID else firestore.Client()
    return _db

# Alias conveniente (así puedes from firebase_db import get_db)
get_db = db

# ---------------------------
# Usuarios (lo tuyo, intacto)
# ---------------------------

def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    # Versión actual (muestra warning pero funciona):
    docs = db().collection("users").where("username", "==", username).limit(1).stream()

    # (Opcional) versión sin warning:
    # docs = (db().collection("users")
    #         .where(filter=FieldFilter("username", "==", username))
    #         .limit(1)
    #         .stream())

    for d in docs:
        data = d.to_dict()
        data["id"] = d.id
        return data
    return None

def create_user(username: str, hashed_password: str) -> Dict[str, Any]:
    existing = get_user_by_username(username)
    if existing:
        raise ValueError("Usuario ya existe")
    ref = db().collection("users").document()
    ref.set({"username": username, "hashed_password": hashed_password})
    data = ref.get().to_dict() or {}
    data["id"] = ref.id
    return data

# --------------------------------------
# Media + Jobs (helpers para API/worker)
# --------------------------------------

def get_media_entry(media_id: str) -> Optional[Dict[str, Any]]:
    """Obtiene el documento media/{media_id} con su id embebido."""
    snap = db().collection("media").document(media_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    data["id"] = snap.id
    return data

def set_media_job_enqueued(
    media_id: str,
    job_id: str,
    *,
    target: str,
    output_prefix: str,
) -> None:
    """
    Deja el job en 'enqueued' y registra output_prefix.
    Úsalo al encolar (API).
    """
    ref = db().collection("media").document(media_id)
    ref.set({
        "job_ids": firestore.ArrayUnion([job_id]),
        f"jobs.{job_id}": {
            "target": target,
            "status": "enqueued",
            "output_prefix": output_prefix,
            "details": None,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    }, merge=True)

def update_media_job_fields(media_id: str, job_id: str, **fields) -> None:
    """
    Actualiza campos arbitrarios en jobs.{job_id}.*
    Útil para status='processing' | 'done' | 'failed', details, etc.
    """
    if not fields:
        return
    ref = db().collection("media").document(media_id)
    ref.update({f"jobs.{job_id}.{k}": v for k, v in fields.items()})

def mark_media_job_processing(media_id: str, job_id: str) -> None:
    update_media_job_fields(
        media_id, job_id,
        status="processing",
        updated_at=firestore.SERVER_TIMESTAMP,
    )

def mark_media_job_done(media_id: str, job_id: str, *, output_prefix: Optional[str] = None) -> None:
    payload = {
        "status": "done",
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    if output_prefix:
        payload["output_prefix"] = output_prefix
    update_media_job_fields(media_id, job_id, **payload)

def mark_media_job_failed(media_id: str, job_id: str, *, details: Optional[str] = None) -> None:
    payload = {
        "status": "failed",
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    if details:
        payload["details"] = details
    update_media_job_fields(media_id, job_id, **payload)
