from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Collection, CollectionMembership, Work
from app.services.collection_prompts import summary_prompt_for_work


async def test_latest_collection_controls_summary_prompt_with_global_fallback():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        work = Work(platform_work_id="prompt-work", title="提示词作品")
        manual = Collection(key="manual-import", title="手动导入")
        learning = Collection(
            key="local-learning",
            title="学习",
            summary_prompt="提炼学习步骤",
        )
        session.add_all([work, manual, learning])
        await session.flush()
        session.add(
            CollectionMembership(collection_id=manual.id, work_id=work.id)
        )
        await session.flush()
        session.add(
            CollectionMembership(collection_id=learning.id, work_id=work.id)
        )
        await session.commit()

        assert (
            await summary_prompt_for_work(session, work.id, "全局规则")
            == "提炼学习步骤"
        )

        learning.summary_prompt = None
        await session.commit()
        assert (
            await summary_prompt_for_work(session, work.id, "全局规则")
            == "全局规则"
        )
    await engine.dispose()
