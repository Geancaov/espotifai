from fastapi import FastAPI, Form, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from .auth import create_user as auth_create_user, authenticate, current_user
from .auth_google import router as google_router

app = FastAPI(title="Auth con Firestore + JWT")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

app.include_router(google_router)  # expone /auth/google

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
