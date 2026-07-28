from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Job
from app.schemas import JobView
from app.services.jobs import cancel_job, job_to_dict


router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=list[JobView])
async def list_jobs(session: AsyncSession = Depends(get_db)):
    rows = (
        await session.execute(select(Job).order_by(desc(Job.created_at)).limit(50))
    ).scalars().all()
    return [job_to_dict(row) for row in rows]


@router.get("/{job_id}", response_model=JobView)
async def get_job(job_id: str, session: AsyncSession = Depends(get_db)):
    job = await session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job_to_dict(job)


@router.post("/{job_id}/cancel", response_model=JobView)
async def stop_job(job_id: str, session: AsyncSession = Depends(get_db)):
    try:
        return job_to_dict(await cancel_job(session, job_id))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
