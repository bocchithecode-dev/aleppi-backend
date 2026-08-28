# routers/professionals.py
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form,
                     HTTPException, UploadFile, status)
from passlib.context import CryptContext
from sqlalchemy import case, func, update
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from auth.deps import get_current_admin, get_current_user, get_current_user_optional
from database import get_session
from email_service.sender import send_rejection_email
from models import (Professional, ProfessionalAddress, ProfessionalRejection,
                    ProfessionalSchedule, ProfessionalSocials, Subscription,
                    StripeSubscription, User, UserSettings)
from professionals.access import can_manage_profile, sync_professional_status
from professionals.schemas import (ProfessionalActiveUpdate,
                                   ProfessionalAddressCreate,
                                   ProfessionalAddressRead,
                                   ProfessionalAddressUpdate, ProfessionalRead,
                                   ProfessionalRejectRequest,
                                   ProfessionalScheduleCreate,
                                   ProfessionalScheduleRead,
                                   ProfessionalScheduleUpdate,
                                   ProfessionalSocialsRead,
                                   ProfessionalSocialsUpsert,
                                   ProfessionalStatusUpdate)
from storage import storage_service
from utils.logging import get_logger

logger = get_logger(__name__)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

router = APIRouter(prefix="/professionals", tags=["professionals"])

# Nombre del plan (en el catálogo `subscriptions`) cuyos suscriptores activos se
# posicionan primero en el listado de profesionales.
PREMIUM_PLAN_NAME = "Suscripcion Premium"


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def _price_to_plan_name(session: Session) -> Dict[str, str]:
    """Mapa {price_id -> nombre de plan} desde el catálogo `subscriptions`."""
    rows = session.exec(
        select(Subscription.price_id, Subscription.name).where(
            Subscription.price_id.is_not(None)
        )
    ).all()
    return {price_id: name for price_id, name in rows}


def _to_professional_read(
    prof: Professional, price_to_name: Dict[str, str]
) -> ProfessionalRead:
    """Serializa un profesional agregando el nombre de su suscripción activa."""
    read = ProfessionalRead.model_validate(prof)
    subs = getattr(prof.user, "stripe_subscriptions", None) or []
    for sub in subs:
        if sub.status == "active" and sub.price_id in price_to_name:
            read.subscription = price_to_name[sub.price_id]
            break
    return read


@router.post("/", response_model=ProfessionalRead, status_code=status.HTTP_201_CREATED)
async def create_professional(
    email: str = Form(...),
    password: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    specialty: str = Form(...),
    short_description: str = Form(None),
    description: str = Form(None),
    skills: str = Form(None),
    years_experience: int = Form(0),
    degree: str = Form(None),
    license_number: str = Form(None),
    state: str = Form(...),
    city: str = Form(None),
    mobile_phone: str = Form(...),
    professional_status: str = Form("Inactivo"),
    schedules: str = Form(None),
    photo_file: UploadFile | None = File(None),
    license_file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    logger.info(
        "POST /professionals payload: %s",
        {
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "specialty": specialty,
            "short_description": short_description,
            "description": description,
            "skills": skills,
            "years_experience": years_experience,
            "degree": degree,
            "license_number": license_number,
            "state": state,
            "city": city,
            "mobile_phone": mobile_phone,
            "status": professional_status,
            "schedules": schedules,
            "photo_file": photo_file.filename if photo_file else None,
            "license_file": license_file.filename if license_file else None,
        },
    )

    existing_user = session.exec(select(User).where(User.email == email)).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="El email ya está registrado")

    user = User(email=email, hashed_password=get_password_hash(password), role=2)
    session.add(user)
    session.commit()
    session.refresh(user)

    # Upload photo to Azure Blob Storage
    url_photo = None
    if photo_file is not None:
        url_photo = await storage_service.upload_file(photo_file, "uploads/photos")

    # Upload license file to Azure Blob Storage
    license_file_path = await storage_service.upload_file(license_file, "uploads/licenses")

    professional = Professional(
        user_id=user.id,
        first_name=first_name,
        last_name=last_name,
        specialty=specialty,
        short_description=short_description,
        description=description,
        skills=skills,
        url_photo=url_photo,
        years_experience=years_experience,
        degree=degree,
        license_number=license_number,
        license_file_path=license_file_path,
        state=state,
        city=city,
        mobile_phone=mobile_phone,
        status=professional_status,
    )
    session.add(professional)
    session.commit()
    session.refresh(professional)

    # Create schedules if provided
    if schedules is not None:
        try:
            schedules_data = json.loads(schedules)
            for schedule_item in schedules_data:
                new_schedule = ProfessionalSchedule(
                    professional_id=professional.id,
                    day=schedule_item.get("day"),
                    open=schedule_item.get("open"),
                    close=schedule_item.get("close"),
                    is_closed=schedule_item.get("is_closed", False)
                )
                session.add(new_schedule)
            session.commit()
        except json.JSONDecodeError:
            logger.error("Error parsing schedules JSON")

    return professional


@router.get("/", response_model=List[ProfessionalRead])
def list_professionals(session: Session = Depends(get_session)):
    stmt = (
        select(Professional)
        .where(Professional.deleted_at.is_(None))
        .options(selectinload(Professional.user))
    )

    # Los suscriptores con plan Premium se posicionan primero. Cuando hay varios
    # Premium, su orden es aleatorio en cada request; el resto queda por id.
    # Premium = suscripción Stripe activa cuyo price_id corresponde, en el
    # catálogo `subscriptions`, al plan "Suscripcion Premium".
    is_premium = (
        select(StripeSubscription.id)
        .join(Subscription, Subscription.price_id == StripeSubscription.price_id)
        .where(
            StripeSubscription.user_id == Professional.user_id,
            StripeSubscription.status == "active",
            Subscription.name == PREMIUM_PLAN_NAME,
        )
        .exists()
    )
    stmt = stmt.order_by(
        is_premium.desc(),
        case((is_premium, func.random()), else_=0.0),
        Professional.id.asc(),
    )

    professionals = session.exec(stmt).all()
    logger.info("professionals=%s", len(professionals))
    price_to_name = _price_to_plan_name(session)
    for p in professionals:
        sync_professional_status(session, p)
    return [_to_professional_read(p, price_to_name) for p in professionals]


@router.get("/{professional_id}", response_model=ProfessionalRead)
def get_professional(
    professional_id: int,
    session: Session = Depends(get_session),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    stmt = (
        select(Professional)
        .where(Professional.id == professional_id)
        .options(selectinload(Professional.user))
    )
    professional = session.exec(stmt).first()
    if not professional:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    if professional.deleted_at is not None:
        is_owner_or_admin = current_user is not None and (
            current_user.role == 1 or current_user.id == professional.user_id
        )
        if not is_owner_or_admin or not can_manage_profile(session, professional):
            raise HTTPException(status_code=404, detail="Profesional no encontrado")

    sync_professional_status(session, professional)
    return _to_professional_read(professional, _price_to_plan_name(session))


@router.put("/{professional_id}", response_model=ProfessionalRead)
async def update_professional(
    professional_id: int,
    email: str = Form(...),
    password: str = Form(None),
    first_name: str = Form(...),
    last_name: str = Form(...),
    specialty: str = Form(...),
    short_description: str = Form(None),
    description: str = Form(None),
    skills: str = Form(None),
    years_experience: int = Form(0),
    degree: str = Form(None),
    license_number: str = Form(None),
    state: str = Form(...),
    city: str = Form(None),
    mobile_phone: str = Form(...),
    professional_status: str = Form(None),
    web: str = Form(None),
    facebook: str = Form(None),
    instagram: str = Form(None),
    youtube: str = Form(None),
    schedule: str = Form(None),
    url_photo: UploadFile | None = File(None),
    license_file: UploadFile | None = File(None),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    logger.info(
        "PUT /professionals/%s payload: %s",
        professional_id,
        {
            "email": email,
            "password_provided": bool(password),
            "first_name": first_name,
            "last_name": last_name,
            "specialty": specialty,
            "short_description": short_description,
            "description": description,
            "skills": skills,
            "years_experience": years_experience,
            "degree": degree,
            "license_number": license_number,
            "state": state,
            "city": city,
            "mobile_phone": mobile_phone,
            "status": professional_status,
            "web": web,
            "facebook": facebook,
            "instagram": instagram,
            "youtube": youtube,
            "schedule": schedule,
            "url_photo": url_photo.filename if url_photo and url_photo.filename else None,
            "license_file": license_file.filename if license_file and license_file.filename else None,
        },
    )

    professional = session.get(Professional, professional_id)
    if not professional:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    if current_user.role != 1 and professional.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="No autorizado")

    if professional.deleted_at is not None and not can_manage_profile(session, professional):
        raise HTTPException(status_code=403, detail="Perfil eliminado sin suscripción vigente")

    user = session.get(User, professional.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    user.email = email
    if password:
        user.hashed_password = get_password_hash(password)
    session.add(user)

    if url_photo is not None and url_photo.filename:
        print(url_photo)
        professional.url_photo = await storage_service.upload_file(
            url_photo, "uploads/photos"
        )

    if license_file is not None and license_file.filename:
        professional.license_file_path = await storage_service.upload_file(
            license_file, "uploads/licenses"
        )

    professional.first_name = first_name
    professional.last_name = last_name
    professional.specialty = specialty
    professional.short_description = short_description
    professional.description = description
    professional.skills = skills
    professional.years_experience = years_experience
    professional.degree = degree
    professional.license_number = license_number
    professional.state = state
    professional.city = city
    professional.mobile_phone = mobile_phone
    if professional_status is not None:
        professional.status = professional_status
    session.add(professional)

    # Upsert socials
    socials = session.exec(
        select(ProfessionalSocials).where(
            ProfessionalSocials.professional_id == professional_id
        )
    ).first()

    if socials is None:
        socials = ProfessionalSocials(professional_id=professional_id)

    socials.web = web
    socials.facebook = facebook
    socials.instagram = instagram
    socials.youtube = youtube
    session.add(socials)

    # Upsert schedules
    if schedule is not None:
        try:
            schedules_data = json.loads(schedule)

            # Eliminar schedules existentes
            existing_schedules = session.exec(
                select(ProfessionalSchedule).where(
                    ProfessionalSchedule.professional_id == professional_id
                )
            ).all()
            for sch in existing_schedules:
                session.delete(sch)

            # Crear nuevos schedules
            for schedule_item in schedules_data:
                new_schedule = ProfessionalSchedule(
                    professional_id=professional_id,
                    day=schedule_item.get("day"),
                    open=schedule_item.get("start") or schedule_item.get("open"),
                    close=schedule_item.get("end") or schedule_item.get("close"),
                    is_closed=schedule_item.get("is_closed", False)
                )
                session.add(new_schedule)
        except json.JSONDecodeError:
            logger.error("Error parsing schedules JSON")

    session.commit()
    session.refresh(professional)
    return professional


@router.delete("/{professional_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_professional(
    professional_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    professional = session.get(Professional, professional_id)
    if not professional:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    if current_user.role != 1 and professional.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="No autorizado")

    if professional.deleted_at is None:
        professional.deleted_at = datetime.utcnow()
        professional.status = "Inactivo"
        session.add(professional)
        session.commit()
    return


@router.patch("/", response_model=ProfessionalRead)
def update_professional_active(
    payload: ProfessionalActiveUpdate,
    session: Session = Depends(get_session),
):
    professional = session.get(Professional, payload.id)
    if not professional:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    professional.active = payload.active
    if payload.status is not None:
        professional.status = payload.status
    session.add(professional)
    session.commit()
    session.refresh(professional)
    return professional


@router.patch("/{professional_id}/status", response_model=ProfessionalRead)
def update_professional_status(
    professional_id: int,
    payload: ProfessionalStatusUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(get_current_admin),
):
    professional = session.get(Professional, professional_id)
    if not professional:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    if payload.active is not None:
        professional.active = payload.active
    if payload.status is not None:
        professional.status = payload.status
    session.add(professional)
    session.commit()
    session.refresh(professional)
    return professional


@router.patch("/{professional_id}/reject", response_model=ProfessionalRead)
def reject_professional(
    professional_id: int,
    payload: ProfessionalRejectRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    admin: User = Depends(get_current_admin),
):
    professional = session.get(Professional, professional_id)
    if not professional:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    professional.status = "Rechazado"
    professional.active = False

    rejection = ProfessionalRejection(
        professional_id=professional.id,
        reason=payload.reason.strip(),
        reviewed_by=admin.id,
    )

    session.add(professional)
    session.add(rejection)
    session.commit()
    session.refresh(professional)

    settings = session.exec(
        select(UserSettings).where(UserSettings.user_id == professional.user_id)
    ).first()
    notify_email = settings.notify_email if settings else True

    if notify_email:
        user = session.get(User, professional.user_id)
        if user and user.email:
            background_tasks.add_task(
                send_rejection_email,
                to_email=user.email,
                professional_name=f"{professional.first_name} {professional.last_name}",
                reason=rejection.reason,
            )

    return professional


# ---------- Socials (1:1) ----------
@router.put("/{professional_id}/socials", response_model=ProfessionalSocialsRead)
def upsert_socials(
    professional_id: int,
    payload: ProfessionalSocialsUpsert,
    session: Session = Depends(get_session),
):
    if not session.get(Professional, professional_id):
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    socials = session.exec(
        select(ProfessionalSocials).where(
            ProfessionalSocials.professional_id == professional_id
        )
    ).first()

    if socials is None:
        socials = ProfessionalSocials(professional_id=professional_id)

    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(socials, k, v)

    session.add(socials)
    session.commit()
    session.refresh(socials)
    return socials


@router.get("/{professional_id}/socials", response_model=ProfessionalSocialsRead)
def get_socials(professional_id: int, session: Session = Depends(get_session)):
    socials = session.exec(
        select(ProfessionalSocials).where(
            ProfessionalSocials.professional_id == professional_id
        )
    ).first()
    if not socials:
        raise HTTPException(status_code=404, detail="Socials no encontrados")
    return socials


# ---------- Schedules (1:N) ----------
@router.get(
    "/{professional_id}/schedules", response_model=List[ProfessionalScheduleRead]
)
def list_schedules(professional_id: int, session: Session = Depends(get_session)):
    return session.exec(
        select(ProfessionalSchedule)
        .where(ProfessionalSchedule.professional_id == professional_id)
        .order_by(ProfessionalSchedule.day.asc())
    ).all()


@router.post(
    "/{professional_id}/schedules",
    response_model=ProfessionalScheduleRead,
    status_code=201,
)
def create_schedule(
    professional_id: int,
    payload: ProfessionalScheduleCreate,
    session: Session = Depends(get_session),
):
    if not session.get(Professional, professional_id):
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    row = ProfessionalSchedule(professional_id=professional_id, **payload.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.patch(
    "/{professional_id}/schedules/{schedule_id}",
    response_model=ProfessionalScheduleRead,
)
def update_schedule(
    professional_id: int,
    schedule_id: int,
    payload: ProfessionalScheduleUpdate,
    session: Session = Depends(get_session),
):
    row = session.get(ProfessionalSchedule, schedule_id)
    if not row or row.professional_id != professional_id:
        raise HTTPException(status_code=404, detail="Schedule no encontrado")

    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(row, k, v)

    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.delete("/{professional_id}/schedules/{schedule_id}", status_code=204)
def delete_schedule(
    professional_id: int, schedule_id: int, session: Session = Depends(get_session)
):
    row = session.get(ProfessionalSchedule, schedule_id)
    if not row or row.professional_id != professional_id:
        raise HTTPException(status_code=404, detail="Schedule no encontrado")

    session.delete(row)
    session.commit()
    return


# ---------- Addresses (1:N) ----------
@router.get(
    "/{professional_id}/addresses", response_model=List[ProfessionalAddressRead]
)
def list_addresses(professional_id: int, session: Session = Depends(get_session)):
    return session.exec(
        select(ProfessionalAddress)
        .where(ProfessionalAddress.professional_id == professional_id)
        .order_by(ProfessionalAddress.is_primary.desc(), ProfessionalAddress.id.desc())
    ).all()


@router.post(
    "/{professional_id}/addresses",
    response_model=ProfessionalAddressRead,
    status_code=201,
)
def create_address(
    professional_id: int,
    payload: ProfessionalAddressCreate,
    session: Session = Depends(get_session),
):
    if not session.get(Professional, professional_id):
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    if payload.is_primary:
        session.exec(
            update(ProfessionalAddress)
            .where(ProfessionalAddress.professional_id == professional_id)
            .values(is_primary=False)
        )

    row = ProfessionalAddress(professional_id=professional_id, **payload.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.patch(
    "/{professional_id}/addresses/{address_id}", response_model=ProfessionalAddressRead
)
def update_address(
    professional_id: int,
    address_id: int,
    payload: ProfessionalAddressUpdate,
    session: Session = Depends(get_session),
):
    row = session.get(ProfessionalAddress, address_id)
    if not row or row.professional_id != professional_id:
        raise HTTPException(status_code=404, detail="Address no encontrado")

    data = payload.model_dump(exclude_unset=True)

    if data.get("is_primary") is True:
        session.exec(
            update(ProfessionalAddress)
            .where(
                (ProfessionalAddress.professional_id == professional_id)
                & (ProfessionalAddress.id != address_id)
            )
            .values(is_primary=False)
        )

    for k, v in data.items():
        setattr(row, k, v)

    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.post(
    "/{professional_id}/addresses/{address_id}/make-primary",
    response_model=ProfessionalAddressRead,
)
def make_primary_address(
    professional_id: int, address_id: int, session: Session = Depends(get_session)
):
    row = session.get(ProfessionalAddress, address_id)
    if not row or row.professional_id != professional_id:
        raise HTTPException(status_code=404, detail="Address no encontrado")

    session.exec(
        update(ProfessionalAddress)
        .where(ProfessionalAddress.professional_id == professional_id)
        .values(is_primary=False)
    )

    row.is_primary = True
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.delete("/{professional_id}/addresses/{address_id}", status_code=204)
def delete_address(
    professional_id: int, address_id: int, session: Session = Depends(get_session)
):
    row = session.get(ProfessionalAddress, address_id)
    if not row or row.professional_id != professional_id:
        raise HTTPException(status_code=404, detail="Address no encontrado")

    session.delete(row)
    session.commit()
    return
