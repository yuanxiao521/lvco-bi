"""Verify the password directly via the backend's verify_password helper."""
import asyncio
import bcrypt
from sqlalchemy import select
from app.core.database import get_db
from app.core.security import verify_password
from app.models.user import User


async def main():
    async for db in get_db():
        result = await db.execute(select(User).where(User.email == "test@lvcom"))
        user = result.scalar_one_or_none()
        if user is None:
            print("user not found")
            return
        stored = user.password_hash
        print("stored hash:", stored)
        print("hash length:", len(stored))
        # Test admin123
        print("verify admin123:", verify_password("admin123", stored))
        print("bcrypt checkpw:", bcrypt.checkpw(b"admin123", stored.encode("utf-8")))
        break


asyncio.run(main())