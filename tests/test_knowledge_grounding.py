import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Collection, CollectionMembership, KnowledgeChunk, Work
import app.services.knowledge as knowledge
from app.services.knowledge import search
from app.services.providers import ProviderUsage


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        group = Collection(key="manual-import", title="手动导入")
        value.add(group)
        works = [
            Work(
                platform_work_id="work-1",
                title="咖啡教程",
                source_url="https://www.douyin.com/video/1",
                library_state="in_library",
                processing_state="processed",
            ),
            Work(
                platform_work_id="work-2",
                title="不应检索",
                library_state="pending",
                processing_state="processed",
            ),
        ]
        value.add_all(works)
        await value.flush()
        value.add(CollectionMembership(collection_id=group.id, work_id=works[0].id))
        value.add_all(
            [
                KnowledgeChunk(
                    work_id=works[0].id,
                    chunk_index=0,
                    source_kind="transcript",
                    text="手冲咖啡建议使用九十二度热水",
                ),
                KnowledgeChunk(
                    work_id=works[0].id,
                    chunk_index=1,
                    source_kind="ocr",
                    text="研磨度应接近细砂糖",
                ),
                KnowledgeChunk(
                    work_id=works[1].id,
                    chunk_index=0,
                    source_kind="metadata",
                    text="手冲咖啡隐藏候选",
                ),
            ]
        )
        await value.commit()
        yield value
    await engine.dispose()


async def test_lexical_search_does_not_return_zero_score_evidence(session):
    assert await search(session, "量子力学") == []


async def test_lexical_search_returns_grounded_source_and_static_group(session):
    results = await search(session, "咖啡热水")
    assert len(results) == 1
    assert results[0]["title"] == "咖啡教程"
    assert results[0]["collection"] == "手动导入"
    assert results[0]["external_url"].endswith("/video/1")
    assert "九十二度" in results[0]["text"]


async def test_search_excludes_pending_works_even_when_text_matches(session):
    results = await search(session, "隐藏候选")
    assert results == []


async def test_search_groups_multiple_chunks_from_the_same_work(session):
    results = await search(session, "咖啡研磨")
    assert len(results) == 1
    assert "九十二度" in results[0]["text"]
    assert "细砂糖" in results[0]["text"]


async def test_semantic_search_keeps_exact_chinese_phrase_above_unrelated_vector_hit(
    monkeypatch,
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class FakeProvider:
        def __init__(self, _key):
            pass

        async def embed(self, _texts):
            return [[1.0, 0.0]], ProviderUsage("text-embedding-v4", 1, 0, 0)

    async def fake_secret(*_args):
        return "test-key"

    async def fake_reserve(*_args, **_kwargs):
        return object()

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(knowledge, "DashScopeProvider", FakeProvider)
    monkeypatch.setattr(knowledge, "get_secret", fake_secret)
    monkeypatch.setattr(knowledge, "reserve", fake_reserve)
    monkeypatch.setattr(knowledge, "record_usage", no_op)
    monkeypatch.setattr(knowledge, "consume", no_op)

    async with factory() as value:
        math = Work(
            platform_work_id="math",
            title="定积分区间减半",
            library_state="in_library",
            processing_state="processed",
        )
        psychology = Work(
            platform_work_id="psychology",
            title="心理学沟通",
            library_state="in_library",
            processing_state="processed",
        )
        value.add_all([math, psychology])
        await value.flush()
        value.add_all(
            [
                KnowledgeChunk(
                    work_id=math.id,
                    chunk_index=0,
                    source_kind="notes",
                    text="区间减半公式可以缩短定积分的计算区间。",
                    embedding=[0.30, 0.953939],
                ),
                KnowledgeChunk(
                    work_id=psychology.id,
                    chunk_index=0,
                    source_kind="notes",
                    text="沟通中需要关注情绪和关系。",
                    embedding=[1.0, 0.0],
                ),
            ]
        )
        await value.commit()
        results = await search(value, "区间减半")

    assert results
    assert results[0]["title"] == "定积分区间减半"
    await engine.dispose()
