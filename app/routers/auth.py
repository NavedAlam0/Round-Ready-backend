import asyncio
import json
import socket

import firebase_admin
import urllib3.util.connection as urllib3_connection
from firebase_admin import credentials, auth as firebase_auth
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from loguru import logger

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.schemas.user import UserResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])

security = HTTPBearer()

if settings.force_ipv4_http:
    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET

# Initialize Firebase Admin SDK
if not firebase_admin._apps:
    cred_source = (
        json.loads(settings.firebase_credentials_json)
        if settings.firebase_credentials_json
        else settings.firebase_credentials_path
    )
    cred = credentials.Certificate(cred_source)
    firebase_admin.initialize_app(cred, {"httpTimeout": settings.firebase_http_timeout})


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Verify Firebase ID token and return the user.

    If the user doesn't exist in the database yet (first login), auto-create them.
    """
    token = credentials.credentials
    logger.info("Starting Firebase token verification...")
    try:
        decoded = await asyncio.wait_for(
            asyncio.to_thread(firebase_auth.verify_id_token, token, check_revoked=False),
            timeout=settings.firebase_http_timeout + 5.0,
        )
    except asyncio.TimeoutError:
        logger.error(
            f"Firebase token verification timed out after "
            f"{settings.firebase_http_timeout + 5.0:.0f}s"
        )
        raise HTTPException(status_code=401, detail="Token verification timed out")
    except Exception as e:
        logger.warning(f"Firebase token verification failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    logger.info("Firebase token verified OK")

    firebase_uid = decoded["uid"]
    email = decoded.get("email", "")
    name = decoded.get("name", "") or email.split("@")[0]
    auth_provider = decoded.get("firebase", {}).get("sign_in_provider", "password")

    # Find or create user in database
    logger.info("Querying database for user...")
    result = await db.execute(select(User).where(User.id == firebase_uid))
    user = result.scalar_one_or_none()
    logger.info(f"Database query done, user found: {user is not None}")

    if not user:
        # First login — create user in our database
        user = User(id=firebase_uid, email=email, full_name=name, auth_provider=auth_provider)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        logger.info(f"Created new user from Firebase: {email} ({firebase_uid})")

    return user


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user
