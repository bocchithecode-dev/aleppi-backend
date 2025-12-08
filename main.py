# main.py
from fastapi import FastAPI

from database import create_db_and_tables
from auth.router import router as auth_router
from professionals.router import router as professionals_router
from admin.router import router as admin_users_router
from stripe.router import router as stripe_router

app = FastAPI(
    title="ALEPPI BACKEND",
    version="0.1.0",
)

app.include_router(auth_router)
app.include_router(professionals_router)
app.include_router(admin_users_router)
app.include_router(stripe_router)

@app.on_event("startup")
def on_startup():
    create_db_and_tables()


@app.get("/", tags=["health"])
def root():
    return {"message": "OK, API corriendo"}
