from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# 连接池参数（实测教训：画布一轮长任务 + 通知 SSE 各占连接，10+20=30 会被打满，
# 触发 "QueuePool limit ... connection timed out, timeout 30" → 查询卡 30s 拖垮整轮）。
#   · pool_size/overflow 提高：长任务（请求级 session 整个流期间占用）+ 多标签页订阅；
#   · pool_timeout 收紧到 10s：宁可快速失败让上层降级，也不要每个操作白等 30s；
#   · pool_recycle：避免云 PG / 中间设备把空闲连接静默掐断后拿到半死连接。
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_size=20,
    max_overflow=40,
    pool_timeout=10,
    pool_recycle=1800,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
