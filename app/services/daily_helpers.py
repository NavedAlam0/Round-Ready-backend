import time

import httpx
from loguru import logger

from app.config import settings

DAILY_API_URL = "https://api.daily.co/v1"


async def create_daily_room() -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(
            f"{DAILY_API_URL}/rooms",
            headers={"Authorization": f"Bearer {settings.daily_api_key}"},
            json={
                "properties": {
                    "exp": int(time.time()) + 3600,  # 1 hour
                    "enable_chat": False,
                }
            },
        )
        if not res.is_success:
            logger.error(f"Daily.co create room failed: HTTP {res.status_code} — {res.text}")
            raise ValueError(f"Daily.co create room failed: HTTP {res.status_code} — {res.text}")
        data = res.json()
        logger.info(f"Daily room created: {data['name']}")
        return {"url": data["url"], "name": data["name"]}


async def get_daily_token(room_name: str, is_owner: bool = False) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(
            f"{DAILY_API_URL}/meeting-tokens",
            headers={"Authorization": f"Bearer {settings.daily_api_key}"},
            json={
                "properties": {
                    "room_name": room_name,
                    "is_owner": is_owner,
                    "exp": int(time.time()) + 3600,
                }
            },
        )
        if not res.is_success:
            logger.error(f"Daily.co get token failed: HTTP {res.status_code} — {res.text}")
            raise ValueError(f"Daily.co get token failed: HTTP {res.status_code} — {res.text}")
        return res.json()["token"]
