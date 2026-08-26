from fastapi import APIRouter, HTTPException, Response, Cookie, Depends
from pydantic import BaseModel

from ..auth.ldap_auth import authenticate, LdapAuthError
from ..security import create_session_token, decode_session_token
from ..schemas import UserProfile
from ..config import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()

COOKIE_NAME = "session"


class LoginRequest(BaseModel):
    login: str
    password: str


@router.post("/login", response_model=UserProfile)
def login(body: LoginRequest, response: Response):
    try:
        user = authenticate(body.login, body.password)
    except LdapAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    is_admin = user.login.lower() in settings.admin_logins()

    token = create_session_token(
        user.login,
        {
            "display_name": user.display_name,
            "email": user.email,
            "department": user.department,
            "position": user.position,
            "is_admin": is_admin,
        },
    )
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.app_env != "dev",
        samesite="lax",
        max_age=settings.jwt_expire_minutes * 60,
    )
    return UserProfile(
        login=user.login,
        display_name=user.display_name,
        email=user.email,
        department=user.department,
        position=user.position,
        is_admin=is_admin,
    )


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


def get_current_user(session: str | None = Cookie(default=None)) -> UserProfile:
    if not session:
        raise HTTPException(status_code=401, detail="Не авторизован")
    payload = decode_session_token(session)
    if not payload:
        raise HTTPException(status_code=401, detail="Сессия истекла, войдите заново")
    return UserProfile(
        login=payload["sub"],
        display_name=payload.get("display_name", payload["sub"]),
        email=payload.get("email", ""),
        department=payload.get("department"),
        position=payload.get("position"),
        is_admin=payload.get("is_admin", False),
    )


def require_admin(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """Зависимость для эндпоинтов, доступных только роли «Администратор формы»
    (routers/admin.py, и POST /api/dictionaries/reload). Роль вычисляется один раз
    при логине (см. login() выше) из settings.form_admin_logins и живёт в самом
    JWT — не требует похода в БД/AD на каждый запрос."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Требуется роль «Администратор формы»")
    return user


@router.get("/me", response_model=UserProfile)
def me(user: UserProfile = Depends(get_current_user)):
    return user
