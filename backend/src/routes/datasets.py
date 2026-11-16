from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.models import Dataset, LedgerDatasetFlag
from src.schemas.dataset import DatasetInput
from src.middlewares.auth import current_user, can_write
from src.services import datasets
from src.services.audit import record
from src.utils.serialization import dataset_dict
router=APIRouter(prefix='/datasets')
@router.get('')
def listing(db=Depends(get_db),u=Depends(current_user)):
    flags={flag.dataset_id:flag for flag in db.scalars(select(LedgerDatasetFlag))}
    return [{**dataset_dict(d),'invalidated':bool(flags.get(d.id) and flags[d.id].invalidated),
             'invalid_reason':flags[d.id].reason if flags.get(d.id) else ''}
            for d in db.scalars(select(Dataset).order_by(Dataset.id.desc()))]
@router.post('',status_code=201)
def create(data:DatasetInput,db=Depends(get_db),u=Depends(can_write)):
    d=datasets.create(db,data); record(db,u.email,'dataset.create',{'id':d.id,'rows':len(d.records)})
    db.commit(); return dataset_dict(d)
@router.get('/{id}')
def detail(id:int,db=Depends(get_db),u=Depends(current_user)):
    d=db.get(Dataset,id)
    if not d:raise HTTPException(404,'数据批次不存在')
    flag=db.get(LedgerDatasetFlag,d.id)
    return {**dataset_dict(d),'records':d.records,
            'invalidated':bool(flag and flag.invalidated),
            'invalid_reason':flag.reason if flag else ''}
