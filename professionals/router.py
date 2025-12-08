# routers/professionals.py
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select
from passlib.context import CryptContext

from database import get_session
from models import User, Professional
from professionals.schemas import ProfessionalCreate, ProfessionalRead

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

router = APIRouter(prefix="/professionals", tags=["professionals"])


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


@router.post("/", response_model=ProfessionalRead, status_code=status.HTTP_201_CREATED)
def create_professional(
    payload: ProfessionalCreate,
    session: Session = Depends(get_session),
):
    # 1) validar que el email no exista
    existing_user = session.exec(
        select(User).where(User.email == payload.email)
    ).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El email ya está registrado",
        )

    # 2) crear user con role=2 (profesional)
    user = User(
        email=payload.email,
        hashed_password=get_password_hash(payload.password),
        role=2,
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    # 3) crear professional ligado al user
    professional = Professional(
        user_id=user.id,
        first_name=payload.first_name,
        last_name=payload.last_name,
        specialty=payload.specialty,
        years_experience=payload.years_experience,
        degree=payload.degree,
        license_number=payload.license_number,
        state=payload.state,
        city=payload.city,
        mobile_phone=payload.mobile_phone,
    )
    session.add(professional)
    session.commit()
    session.refresh(professional)

    # cargamos relación user
    professional.user = user
    return professional


@router.get("/", response_model=List[ProfessionalRead])
def list_professionals(session: Session = Depends(get_session)):
    professionals = session.exec(select(Professional)).all()
    # aseguramos que la relación user se cargue
    for p in professionals:
        _ = p.user
    return professionals


@router.get("/{professional_id}", response_model=ProfessionalRead)
def get_professional(
    professional_id: int,
    session: Session = Depends(get_session),
):
    professional = session.get(Professional, professional_id)
    if not professional:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")
    _ = professional.user
    return professional


@router.put("/{professional_id}", response_model=ProfessionalRead)
def update_professional(
    professional_id: int,
    payload: ProfessionalCreate,  # podrías hacer otro schema solo para update
    session: Session = Depends(get_session),
):
    professional = session.get(Professional, professional_id)
    if not professional:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    user = session.get(User, professional.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    # actualizar user
    user.email = payload.email
    user.hashed_password = get_password_hash(payload.password)
    session.add(user)

    # actualizar professional
    professional.first_name = payload.first_name
    professional.last_name = payload.last_name
    professional.specialty = payload.specialty
    professional.years_experience = payload.years_experience
    professional.degree = payload.degree
    professional.license_number = payload.license_number
    professional.state = payload.state
    professional.city = payload.city
    professional.mobile_phone = payload.mobile_phone

    session.add(professional)
    session.commit()
    session.refresh(professional)
    professional.user = user
    return professional


@router.delete("/{professional_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_professional(
    professional_id: int,
    session: Session = Depends(get_session),
):
    professional = session.get(Professional, professional_id)
    if not professional:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    # si quieres borrar también el user:
    user = session.get(User, professional.user_id)
    if user:
        session.delete(user)
    session.delete(professional)
    session.commit()
    return
