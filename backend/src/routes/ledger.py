from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select

from src.config.database import get_db
from src.middlewares.auth import admin_only, can_write, current_user
from src.models import Adjudication, Dataset, IdentityCorrection, LedgerBatch, LedgerFrame, Observation, PointMerge
from src.schemas.ledger import (
    AdjudicationInput, BatchInput, FrameInput, IdentityCorrectionInput, MergeInput,
    ObservationInput, PublishInput, RevokeInput,
)
from src.services import ledger

router = APIRouter(prefix='/ledger')


@router.get('')
def state(db=Depends(get_db), user=Depends(current_user)):
    return ledger.state_dict(db)


@router.get('/observations')
def observations(db=Depends(get_db), user=Depends(current_user), limit: int = 500):
    rows = db.scalars(select(Observation).order_by(Observation.id.desc()).limit(max(1, min(limit, 2000))))
    return [ledger.observation_dict(row) for row in rows]


@router.post('/observations', status_code=201)
def create_observation(data: ObservationInput, db=Depends(get_db), user=Depends(can_write)):
    observation = ledger.create_observation(db, data, user.email)
    db.commit()
    return ledger.observation_dict(observation)


@router.post('/observations/{id}/correction')
def correct_observation(id: int, data: IdentityCorrectionInput, db=Depends(get_db), user=Depends(admin_only)):
    result = ledger.correct_identity(db, id, data.token, data.point_name, data.reason, user.email)
    db.commit()
    return result


@router.post('/merges/preview')
def preview_merge(data: MergeInput, db=Depends(get_db), user=Depends(can_write)):
    return ledger.preview_merge(db, data.source_token, data.target_token)


@router.post('/import-dataset/{dataset_id}')
def import_dataset(dataset_id: int, db=Depends(get_db), user=Depends(admin_only)):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, '数据批次不存在')
    result = ledger.import_daily_table(db, dataset, user.email)
    db.commit()
    return result


@router.get('/merges')
def merges(db=Depends(get_db), user=Depends(current_user)):
    return [ledger.merge_dict(row) for row in db.scalars(select(PointMerge).order_by(PointMerge.id.desc()))]


@router.post('/merges', status_code=201)
def create_merge(data: MergeInput, db=Depends(get_db), user=Depends(can_write)):
    merge = ledger.create_merge(db, data.source_token, data.target_token, data.reason, user.email)
    db.commit()
    return ledger.merge_dict(merge)


@router.post('/merges/{id}/revoke')
def revoke_merge(id: int, data: RevokeInput, db=Depends(get_db), user=Depends(admin_only)):
    merge = ledger.revoke_merge(db, id, data.reason, user.email)
    db.commit()
    return ledger.merge_dict(merge)


@router.get('/adjudications')
def adjudications(db=Depends(get_db), user=Depends(current_user), status: str = ''):
    query = select(Adjudication).order_by(Adjudication.id.desc())
    if status == 'pending':
        query = query.where(or_(Adjudication.status == 'pending', Adjudication.reopened == True))
    elif status in ('resolved', 'superseded'):
        query = query.where(Adjudication.status == status, Adjudication.reopened == False)
    return [ledger.adjudication_dict(row) for row in db.scalars(query)]


@router.post('/adjudications/{id}/decide')
def decide(id: int, data: AdjudicationInput, db=Depends(get_db), user=Depends(admin_only)):
    adjudication = ledger.decide_adjudication(db, id, data.decision, data.note, user.email)
    db.commit()
    return ledger.adjudication_dict(adjudication)


@router.get('/frames')
def frames(db=Depends(get_db), user=Depends(current_user)):
    return [{'zone': frame.zone, 'size': frame.size, 'version': frame.version}
            for frame in db.scalars(select(LedgerFrame).order_by(LedgerFrame.zone))]


@router.put('/frames/{zone}')
def set_frame(zone: str, data: FrameInput, db=Depends(get_db), user=Depends(admin_only)):
    frame = ledger.set_frame(db, zone, data.size, user.email)
    db.commit()
    return {'zone': frame.zone, 'size': frame.size, 'version': frame.version}


@router.get('/batches')
def batches(db=Depends(get_db), user=Depends(current_user)):
    return [ledger.batch_dict(db, row) for row in db.scalars(select(LedgerBatch).order_by(LedgerBatch.id.desc()))]


@router.post('/batches', status_code=201)
def create_batch(data: BatchInput, db=Depends(get_db), user=Depends(can_write)):
    batch = ledger.create_batch(db, data.name, user.email)
    db.commit()
    return ledger.batch_dict(db, batch)


@router.get('/batches/{id}')
def batch_detail(id: int, db=Depends(get_db), user=Depends(current_user)):
    batch = db.get(LedgerBatch, id)
    if not batch:
        raise HTTPException(404, '日表批次不存在')
    return ledger.batch_dict(db, batch)


@router.post('/batches/{id}/recompute')
def recompute(id: int, db=Depends(get_db), user=Depends(can_write)):
    batch = ledger.recompute_batch(db, id, user.email)
    db.commit()
    return ledger.batch_dict(db, batch)


@router.post('/batches/{id}/publish')
def publish(id: int, data: PublishInput = PublishInput(), db=Depends(get_db), user=Depends(admin_only)):
    result = ledger.publish_batch(db, id, data.idempotency_key, user.email, data.interrupt_after_attempt)
    return result


@router.get('/history')
def history(db=Depends(get_db), user=Depends(current_user)):
    corrections = [{
        'kind': 'identity_correction', 'id': row.id, 'version': row.version,
        'observation_id': row.observation_id, 'old_token': row.old_token,
        'new_token': row.new_token, 'old_name': row.old_name, 'new_name': row.new_name,
        'reason': row.reason, 'actor': row.created_by, 'at': row.created_at,
    } for row in db.scalars(select(IdentityCorrection).order_by(IdentityCorrection.id.desc()).limit(100))]
    return corrections
