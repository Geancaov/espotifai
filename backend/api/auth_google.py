from fastapi import APIRouter, HTTPException, Body
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
import os, jwt, datetime
from .firebase_db import get_user_by_username, create_user as fb_create_user

router = APIRouter()

SECRET = os.getenv("SECRET_KEY", "secret")
EXPIRE_MIN = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
FIREBASE_PROJECT_ID = (os.getenv("FIREBASE_PROJECT_ID") or "").strip()

# tolerancia de desfase de reloj (en segundos)
CLOCK_SKEW = 60


@router.post("/auth/google")
def auth_google(payload: dict = Body(...)):
    idt = payload.get("id_token")
    if not idt:
        raise HTTPException(status_code=400, detail="id_token requerido")

    req = google_requests.Request()
    info = None
    errores = []

    # 1) Intentar como ID token OAuth2 del cliente web
    if GOOGLE_CLIENT_ID:
        try:
            info = google_id_token.verify_oauth2_token(
                idt,
                req,
                audience=GOOGLE_CLIENT_ID,
                clock_skew_in_seconds=CLOCK_SKEW,
            )
        except Exception as e:
            errores.append(f"oauth2: {e}")

    # 2) Si no funcionó, intentar como token de Firebase
    if info is None:
        try:
            info = google_id_token.verify_firebase_token(
                idt,
                req,
                audience=FIREBASE_PROJECT_ID or None,
                clock_skew_in_seconds=CLOCK_SKEW,
            )
        except Exception as e:
            errores.append(f"firebase: {e}")
            # si llega aquí, las dos validaciones fallaron
            detalle = "; ".join(errores) or "ID token inválido"
            raise HTTPException(status_code=401, detail=detalle)

    email = info.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Token válido pero sin email")

    username = email
    u = get_user_by_username(username)
    if not u:
        u = fb_create_user(username=username, hashed_password="GOOGLE_ACCOUNT")

    token = jwt.encode(
        {
            "sub": username,
            "uid": u["id"],
            "exp": datetime.datetime.utcnow()
            + datetime.timedelta(minutes=EXPIRE_MIN),
            "provider": "google",
        },
        SECRET,
        algorithm="HS256",
    )
    return {"access_token": token, "token_type": "bearer"}
