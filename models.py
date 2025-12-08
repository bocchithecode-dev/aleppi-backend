# models.py
from typing import Optional
from datetime import datetime
from sqlmodel import SQLModel, Field, Relationship


class UserBase(SQLModel):
    email: str = Field(index=True)
    role: int = Field(default=2)  # 1=admin, 2=profesional
    is_active: bool = Field(default=True)


class User(UserBase, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    hashed_password: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    professional: Optional["Professional"] = Relationship(back_populates="user")


class ProfessionalBase(SQLModel):
    first_name: str
    last_name: str
    specialty: str
    years_experience: int = 0
    degree: Optional[str] = None
    license_number: Optional[str] = None          
    license_file_path: Optional[str] = None       
    state: str
    city: str
    mobile_phone: str
    active: bool


class Professional(ProfessionalBase, table=True):
    __tablename__ = "professionals"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    user: User = Relationship(back_populates="professional")
