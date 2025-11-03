# backend/api/auth.py
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from passlib.hash import pbkdf2_sha256
import jwt, os, datetime
from .firebase_db import get_user_by_username, create_user as fb_create_user


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")
SECRET = os.getenv("SECRET_KEY", "secret")
EXPIRE_MIN = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

def _hash_pwd(pwd: str) -> str:
    # pbkdf2_sha256 no tiene el límite de 72 bytes de bcrypt
    return pbkdf2_sha256.hash(pwd)

def create_user(username: str, password: str):
    if not username or len(username) < 3:
        raise HTTPException(status_code=422, detail="username inválido (min 3)")
    if not password or len(password) < 8:
        raise HTTPException(status_code=422, detail="password inválido (min 8)")
    try:
        u = fb_create_user(username=username, hashed_password=_hash_pwd(password))
        return {"id": u["id"], "username": u["username"]}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

def authenticate(username: str, password: str):
    u = get_user_by_username(username)
    if not u or not pbkdf2_sha256.verify(password, u.get("hashed_password", "")):
        return None
    payload = {
        "sub": u["username"],
        "uid": u["id"],
        "exp": datetime.datetime.utcnow() + datetime.timedelta(minutes=EXPIRE_MIN),
    }
    token = jwt.encode(payload, SECRET, algorithm="HS256")
    return token

def current_user(token: str = Depends(oauth2_scheme)):
    try:
        data = jwt.decode(token, SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Token inválido")
    return {"id": data["uid"], "username": data["sub"]}
