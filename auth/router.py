# auth/router.py
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlmodel import Session, select

from database import get_session
from models import User
from .schemas import (
    Token,
    LoginRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
)

SECRET_KEY = "CAMBIA_ESTA_CLAVE_SUPER_SECRETA"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

router = APIRouter(prefix="/auth", tags=["auth"])


# ----------------- helpers ----------------- #

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(*, user_id: int, role: int, email: str, active: bool) -> str:
    now = datetime.now()
    payload = {
        "sub": str(user_id),
        "email": str(email),
        "role": int(role),
        "is_active":bool(active),
        "jti": str(uuid4()),
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)).timestamp()),
        "iss": 'aleppi-backend',
        "aud": 'aleppi-frontend',
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_user_by_email(session: Session, email: str) -> Optional[User]:
    statement = select(User).where(User.email == email)
    return session.exec(statement).first()


# ----------------- endpoints ----------------- #

@router.post("/login", response_model=Token)
def login(payload: LoginRequest, session: Session = Depends(get_session)):
    user = get_user_by_email(session, payload.email)
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")

    # IMPORTANTÍSIMO: checar aquí también
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Usuario inactivo")

    access_token = create_access_token(user_id=user.id, role=user.role, email=user.email)
    return Token(access_token=access_token, token_type="bearer")


@router.post("/logout")
def logout():
    # Con JWT puro, el logout normalmente es del lado del cliente.
    # Si quieres, luego vemos lista negra en Redis.
    return {"detail": "Logout exitoso. Borra el token en el cliente."}


@router.post("/password/forgot")
def forgot_password(
    payload: ForgotPasswordRequest,
    session: Session = Depends(get_session),
):
    user = get_user_by_email(session, payload.email)
    if not user:
        # Podrías responder 200 igual para no filtrar usuarios
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado",
        )

    reset_token = create_access_token(
        {"sub": str(user.id), "scope": "password_reset"},
        expires_delta=timedelta(hours=1),
    )

    # Aquí en producción mandarías email con el link que incluye reset_token
    return {
        "detail": "Se ha generado un token de recuperación.",
        "reset_token": reset_token,
    }


@router.post("/password/reset")
def reset_password(
    payload: ResetPasswordRequest,
    session: Session = Depends(get_session),
):
    try:
        data = jwt.decode(payload.token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token inválido o expirado",
        )

    if data.get("scope") != "password_reset":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token no es de recuperación de contraseña",
        )

    user_id = data.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token inválido",
        )

    user = session.get(User, int(user_id))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado",
        )

    user.hashed_password = get_password_hash(payload.new_password)
    session.add(user)
    session.commit()

    return {"detail": "Contraseña actualizada correctamente"}
