"""
JWT-сессия (выдаётся после успешной LDAP-аутентификации, хранится в httpOnly-cookie)
и подписанные одноразовые токены для ссылок "Согласовать/Отклонить" в письме
(itsdangerous — токен содержит request_id + approver_id и живёт ограниченное время,
поэтому согласующему НЕ нужно логиниться, чтобы кликнуть по ссылке из письма).
"""
from datetime import datetime, timedelta
from typing import Optional

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from jose import jwt, JWTError

from .config import get_settings

settings = get_settings()
ALGORITHM = "HS256"

_approval_serializer = URLSafeTimedSerializer(settings.app_secret_key, salt="approval-link")


def create_session_token(user_login: str, extra: dict) -> str:
    payload = {
        "sub": user_login,
        "exp": datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes),
        **extra,
    }
    return jwt.encode(payload, settings.app_secret_key, algorithm=ALGORITHM)


def decode_session_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.app_secret_key, algorithms=[ALGORITHM])
    except JWTError:
        return None


def make_approval_token(request_id: str, approver_id: str) -> str:
    return _approval_serializer.dumps({"request_id": request_id, "approver_id": approver_id})


def read_approval_token(token: str, max_age_seconds: int = 60 * 60 * 24 * 14) -> Optional[dict]:
    try:
        return _approval_serializer.loads(token, max_age=max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None
