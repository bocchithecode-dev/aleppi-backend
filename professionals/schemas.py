from typing import Optional
from pydantic import BaseModel, EmailStr
from auth.schemas import UserRead

class ProfessionalCreate(BaseModel):
    # Datos de usuario
    email: EmailStr
    password: str

    # Datos de perfil profesional (form de la imagen)
    first_name: str
    last_name: str
    specialty: str
    years_experience: int = 0
    degree: Optional[str] = None
    license_number: Optional[str] = None
    state: str
    city: str
    mobile_phone: str


class ProfessionalRead(BaseModel):
    id: int
    first_name: str
    last_name: str
    specialty: str
    years_experience: int
    degree: Optional[str]
    license_number: Optional[str]
    state: str
    city: str
    mobile_phone: str
    user: UserRead

    class Config:
        orm_mode = True


class ProfessionalStatusUpdate(BaseModel):
    active: bool