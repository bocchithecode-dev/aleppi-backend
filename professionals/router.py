# routers/professionals.py
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from sqlmodel import Session, select
from uuid import uuid4
from pathlib import Path
import shutil
from passlib.context import CryptContext

from database import get_session
from models import User, Professional
from professionals.schemas import ProfessionalCreate, ProfessionalRead,ProfessionalStatusUpdate 

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

router = APIRouter(prefix="/professionals", tags=["professionals"])


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


@router.post(
    "/",
    response_model=ProfessionalRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_professional(
    # Datos de usuario
    email: str = Form(...),
    password: str = Form(...),

    # Datos de profesional
    first_name: str = Form(...),
    last_name: str = Form(...),
    specialty: str = Form(...),
    years_experience: int = Form(0),
    degree: str = Form(None),
    license_number: str = Form(None),  # opcional: número de cédula
    state: str = Form(...),
    city: str = Form(...),
    mobile_phone: str = Form(...),

    # Archivo de cédula
    license_file: UploadFile = File(...),

    session: Session = Depends(get_session),
):
    # 1) validar que el email no exista
    existing_user = session.exec(
        select(User).where(User.email == email)
    ).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El email ya está registrado",
        )

    # 2) crear user con role=2 (profesional)
    user = User(
        email=email,
        hashed_password=get_password_hash(password),
        role=2,
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    # 3) guardar archivo de cédula en disco
    uploads_dir = Path("uploads/licenses")
    uploads_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(license_file.filename).suffix  # .pdf, .jpg, etc.
    file_name = f"{uuid4().hex}{ext}"
    file_path = uploads_dir / file_name

    with file_path.open("wb") as f:
        shutil.copyfileobj(license_file.file, f)

    # 4) crear professional ligado al user
    professional = Professional(
        user_id=user.id,
        first_name=first_name,
        last_name=last_name,
        specialty=specialty,
        years_experience=years_experience,
        degree=degree,
        license_number=license_number,              # número texto, si lo mandas
        license_file_path=str(file_path),           # 👈 ruta al archivo guardado
        state=state,
        city=city,
        mobile_phone=mobile_phone,
    )
    session.add(professional)
    session.commit()
    session.refresh(professional)

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
async def update_professional(
    professional_id: int,
    # ----- datos de usuario -----
    email: str = Form(...),
    password: str = Form(None),  # opcional: si viene, se actualiza

    # ----- datos de profesional -----
    first_name: str = Form(...),
    last_name: str = Form(...),
    specialty: str = Form(...),
    years_experience: int = Form(0),
    degree: str = Form(None),
    license_number: str = Form(None),
    state: str = Form(...),
    city: str = Form(...),
    mobile_phone: str = Form(...),

    # ----- archivo de cédula (opcional en update) -----
    license_file: UploadFile | None = File(None),

    session: Session = Depends(get_session),
):
    professional = session.get(Professional, professional_id)
    if not professional:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    user = session.get(User, professional.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    # 1) actualizar user
    user.email = email

    if password:  # solo si mandan password nuevo
        user.hashed_password = get_password_hash(password)

    session.add(user)

    # 2) si viene archivo nuevo, lo guardamos y actualizamos license_file_path
    if license_file is not None:
        uploads_dir = Path("uploads/licenses")
        uploads_dir.mkdir(parents=True, exist_ok=True)

        ext = Path(license_file.filename).suffix  # .pdf, .jpg, etc.
        file_name = f"{uuid4().hex}{ext}"
        file_path = uploads_dir / file_name

        with file_path.open("wb") as f:
            shutil.copyfileobj(license_file.file, f)

        professional.license_file_path = str(file_path)

    # 3) actualizar datos del profesional
    professional.first_name = first_name
    professional.last_name = last_name
    professional.specialty = specialty
    professional.years_experience = years_experience
    professional.degree = degree
    professional.license_number = license_number
    professional.state = state
    professional.city = city
    professional.mobile_phone = mobile_phone

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

@router.patch("/{professional_id}/status", response_model=ProfessionalRead)
def update_professional_status(
    professional_id: int,
    payload: ProfessionalStatusUpdate,
    session: Session = Depends(get_session),
):
    professional = session.get(Professional, professional_id)
    if not professional:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    professional.active = payload.active
    session.add(professional)
    session.commit()
    session.refresh(professional)

    # Cargar relación user si la usas en el response
    _ = professional.user

    return professional