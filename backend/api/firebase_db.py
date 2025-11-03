import os
from typing import Optional, Dict, Any
from google.cloud import firestore

PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID")
_db = None

def db():
    global _db
    if _db is None:
        _db = firestore.Client(project=PROJECT_ID) if PROJECT_ID else firestore.Client()
    return _db

def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    docs = db().collection("users").where("username", "==", username).limit(1).stream()
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
    data = ref.get().to_dict()
    data["id"] = ref.id
    return data
