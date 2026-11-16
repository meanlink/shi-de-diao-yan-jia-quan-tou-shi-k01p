from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from src.config.database import get_db
from src.middlewares.auth import current_user
from src.models import Adjudication, Dataset, Job, LedgerBatch, Observation, Report, Audit
from src.services.business import metadata
from src.services import ledger
router=APIRouter()
@router.get('/meta')
def meta():return metadata()
@router.get('/overview')
def overview(db=Depends(get_db),user=Depends(current_user)):
    return {'datasets':db.scalar(select(func.count()).select_from(Dataset)), 'records':sum(len(d.records) for d in db.scalars(select(Dataset))), 'jobs':db.scalar(select(func.count()).select_from(Job)), 'approved':db.scalar(select(func.count()).select_from(Job).where(Job.approved==True)), 'reports':db.scalar(select(func.count()).select_from(Report)), 'observations':db.scalar(select(func.count()).select_from(Observation)), 'pending_adjudications':db.scalar(select(func.count()).select_from(Adjudication).where(Adjudication.status=='pending')), 'ledger_batches':db.scalar(select(func.count()).select_from(LedgerBatch)), 'published_batches':db.scalar(select(func.count()).select_from(LedgerBatch).where(LedgerBatch.status=='published')), 'ledger_version':ledger.current_version(db)}
@router.get('/audits')
def audits(db=Depends(get_db),user=Depends(current_user)):
    return [{'id':a.id,'actor':a.actor,'action':a.action,'detail':a.detail,'at':a.at} for a in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(50))]
