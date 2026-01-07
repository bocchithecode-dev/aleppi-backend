# auth/schemas.py
from typing import Optional
from pydantic import BaseModel, ConfigDict, EmailStr


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class UserRead(BaseModel):
    id: int
    email: EmailStr
    is_active: bool
    role: int
    model_config = ConfigDict(from_attributes=True)
