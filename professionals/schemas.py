from typing import Optional, List
from pydantic import BaseModel, ConfigDict, EmailStr
from auth.schemas import UserRead
from datetime import time, datetime

class ProfessionalSocialsRead(BaseModel):
    web: Optional[str] = None
    facebook: Optional[str] = None
    instagram: Optional[str] = None
    youtube: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class ProfessionalScheduleRead(BaseModel):
    day: str
    open: Optional[time] = None
    close: Optional[time] = None
    model_config = ConfigDict(from_attributes=True)

class ProfessionalReviewRead(BaseModel):
    id: int
    user: Optional[str] = None   # si luego quieres nombre, aquí lo llenas
    rating: int
    comment: Optional[str] = None
    date: datetime

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
    license_file_path: Optional[str]
    state: str
    city: str
    mobile_phone: str
    user: UserRead

    rating: Optional[float] = None
    reviews: List[ProfessionalReviewRead] = []
    address: Optional[str] = None
    socials: Optional[ProfessionalSocialsRead] = None
    schedule: List[ProfessionalScheduleRead] = []
    
    model_config = ConfigDict(from_attributes=True)


class ProfessionalStatusUpdate(BaseModel):
    active: bool