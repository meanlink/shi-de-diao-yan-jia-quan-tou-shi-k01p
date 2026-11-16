import time
from fastapi import HTTPException
from redis import Redis
from src.config.settings import REDIS_URL
from src.models import Dataset, Job
from src.services.business import metadata
def create(db, data, user):
    dataset=db.get(Dataset,data.dataset_id)
    if not dataset: raise HTTPException(404,'数据批次不存在')
    if dataset.ledger_batch_id is not None:
        from src.models import LedgerDatasetFlag
        flag=db.get(LedgerDatasetFlag,dataset.id)
        if flag and flag.invalidated:
            raise HTTPException(409,'该数据批次因身份更正已失效，不能据此创建新分析；请基于最新台账发布新批次')
    allowed={p['key']:p for p in metadata().get('parameters',[])}
    if set(data.params)-set(allowed): raise HTTPException(422,'包含未知分析参数')
    params={k:p['default'] for k,p in allowed.items()}
    params.update(data.params)
    for key,p in allowed.items():
        v=params[key]
        if p['type']=='number':
            try:
                v=float(v)
                if not (p.get('min',-1e9)<=v<=p.get('max',1e9)): raise ValueError()
                params[key]=v
            except (ValueError,TypeError): raise HTTPException(422, f'{p["label"]} 超出范围或非数值')
        elif str(v) not in [str(o['value']) for o in p.get('options',[])]:
            raise HTTPException(422,f'{p["label"]} 不是有效选项')
    j=Job(name=data.name, dataset_id=data.dataset_id, params=params, created_by=user.id, created_at=time.time(), status='queued')
    db.add(j); db.flush()
    return j
def enqueue(job_id):
    try:
        r=Redis.from_url(REDIS_URL); r.rpush('analysis:queue',job_id); r.close()
    except Exception:
        pass # SQL queued status is the durable fallback
def approve(db, job):
    if job.invalidated:
        raise HTTPException(409,'该分析因关联数据身份更正已失效，不能批准；请重新运行分析')
    if job.status!='completed':raise HTTPException(409,'仅已完成结果可以批准')
    job.approved=True
    return job
