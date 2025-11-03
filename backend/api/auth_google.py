# backend/api/auth_google.py
from fastapi import APIRouter, HTTPException, Body
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
import os, jwt, datetime
from .firebase_db import get_user_by_username, create_user as fb_create_user

router = APIRouter()  

SECRET = os.getenv("SECRET_KEY", "secret")
EXPIRE_MIN = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")

@router.post("/auth/google")
def auth_google(payload: dict = Body(...)):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(500, "Falta GOOGLE_CLIENT_ID en el backend")
    idt = payload.get("id_token")
    if not idt:
        raise HTTPException(400, "id_token requerido")

    try:
        info = google_id_token.verify_oauth2_token(
            idt, google_requests.Request(), GOOGLE_CLIENT_ID
        )
        email = (info.get("email") or "").lower()
        if not email:
            raise HTTPException(400, "Token sin email")

        username = email
        u = get_user_by_username(username)
        if not u:
            # cuenta federada; sin password
            u = fb_create_user(username=username, hashed_password="GOOGLE_ACCOUNT")

        token = jwt.encode(
            {
                "sub": username,
                "uid": u["id"],
                "exp": datetime.datetime.utcnow() + datetime.timedelta(minutes=EXPIRE_MIN),
                "provider": "google",
            },
            SECRET,
            algorithm="HS256",
        )
        return {"access_token": token, "token_type": "bearer"}

    except ValueError:
        raise HTTPException(401, "ID token inválido")
