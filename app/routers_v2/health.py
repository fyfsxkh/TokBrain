from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import SystemHealth, SystemProbe
from app.services.health import SYSTEM_PROBES, run_system_checks, run_system_probe


router = APIRouter(prefix="/api/system-health", tags=["health"])


@router.get("", response_model=SystemHealth)
async def get_health(session: AsyncSession = Depends(get_db)):
    return await run_system_checks(session)


@router.get("/probes/{probe}", response_model=SystemProbe)
async def get_health_probe(probe: str, session: AsyncSession = Depends(get_db)):
    if probe not in SYSTEM_PROBES:
        raise HTTPException(status_code=404, detail="未知的本地检测项目")
    return await run_system_probe(session, probe)
