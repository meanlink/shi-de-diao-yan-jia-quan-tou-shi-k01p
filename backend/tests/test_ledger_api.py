"""HTTP-level checks: permissions, ledger flow, and the legacy import path.

Run from backend/:  python -m pytest tests -q
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False).name
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB)
os.environ.setdefault('JWT_SECRET', 'ledger-http-tests')
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:1/0')
os.environ.setdefault('ARTIFACT_DIR', tempfile.mkdtemp())
os.environ.setdefault('SEED_DEMO', 'false')

import pytest
from fastapi.testclient import TestClient

from src.config.database import Base, Session, engine
from src.main import app
from src.models import Adjudication, Dataset, Job, LedgerBatch, Observation, User
from src.services import ledger


def token(client, email):
    response = client.post('/auth/login', json={'email': email, 'password': 'Demo123!'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['access_token']}


@pytest.fixture(scope='module')
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as db:
        from src.utils.security import hash_password
        for role, name in (('admin', '负责人'), ('analyst', '分析员'), ('viewer', '观察员')):
            db.add(User(email=role + '@demo.local', name=name, role=role,
                        password=hash_password('Demo123!')))
        db.commit()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope='module')
def auth(client):
    return {'admin': token(client, 'admin@demo.local'),
            'analyst': token(client, 'analyst@demo.local'),
            'viewer': token(client, 'viewer@demo.local')}


OBSERVATION = {'day': '2026-11-01', 'team': 'A队', 'zone': '中心区',
               'point_name': '东门站', 'token': 'east-gate', 'passed': True}


def seed_observations(client, auth):
    for zone, size in (('中心区', 6200), ('外围区', 3800)):
        response = client.put('/ledger/frames/' + zone, json={'size': size}, headers=auth['admin'])
        assert response.status_code == 200, response.text
    for team in ('A队', 'B队'):
        for zone, token_suffix, passed in (('中心区', 'c', True), ('外围区', 'o', False)):
            response = client.post('/ledger/observations', headers=auth['analyst'], json={
                'day': '2026-11-01', 'team': team, 'zone': zone,
                'point_name': f'{team}-{zone}', 'token': f'{team}-{token_suffix}', 'passed': passed})
            assert response.status_code == 201, response.text


def test_meta_and_overview_expose_ledger_counts(client, auth):
    meta = client.get('/meta').json()
    assert meta['name']
    overview = client.get('/overview', headers=auth['viewer']).json()
    assert 'observations' in overview and 'pending_adjudications' in overview


def test_ledger_endpoints_require_login(client):
    assert client.get('/ledger').status_code == 401


def test_viewer_cannot_write_observations(client, auth):
    response = client.post('/ledger/observations', headers=auth['viewer'], json=OBSERVATION)
    assert response.status_code == 403
    assert client.post('/ledger/merges', headers=auth['viewer'],
                       json={'source_token': 'a', 'target_token': 'b'}).status_code == 403


def test_only_admin_approves_publishes_and_adjudicates(client, auth):
    seed_observations(client, auth)
    created = client.post('/ledger/batches', headers=auth['analyst'], json={'name': 'HTTP 批次一'})
    assert created.status_code == 201, created.text
    batch = created.json()
    assert batch['status'] == 'pending_review'
    assert client.post(f"/ledger/batches/{batch['id']}/publish", headers=auth['analyst'],
                       json={'idempotency_key': 'nope'}).status_code == 403
    published = client.post(f"/ledger/batches/{batch['id']}/publish", headers=auth['admin'],
                            json={'idempotency_key': 'http-1'})
    assert published.status_code == 200, published.text
    assert published.json()['published'] is True
    dataset_id = published.json()['dataset_id']
    dataset = client.get(f'/datasets/{dataset_id}', headers=auth['viewer']).json()
    assert dataset['ledger_batch_id'] == batch['id']


def test_duplicate_observation_is_idempotent_over_http(client, auth):
    payload = dict(OBSERVATION, token='dup-token', point_name='重复点', zone='中心区')
    first = client.post('/ledger/observations', headers=auth['analyst'], json=payload)
    second = client.post('/ledger/observations', headers=auth['analyst'], json=payload)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()['id'] == second.json()['id']


def test_merge_revoke_and_conflict_flow_over_http(client, auth):
    client.post('/ledger/observations', headers=auth['analyst'], json={
        'day': '2026-11-02', 'team': 'A队', 'zone': '中心区',
        'point_name': '西门站', 'token': 'west', 'passed': True})
    client.post('/ledger/observations', headers=auth['analyst'], json={
        'day': '2026-11-02', 'team': 'A队', 'zone': '中心区',
        'point_name': '西门站(新名)', 'token': 'west-renamed', 'passed': False})
    preview = client.post('/ledger/merges/preview', headers=auth['analyst'],
                          json={'source_token': 'west-renamed', 'target_token': 'west'})
    assert preview.status_code == 200
    assert preview.json()['would_conflict_on_conclusion']
    merged = client.post('/ledger/merges', headers=auth['analyst'],
                         json={'source_token': 'west-renamed', 'target_token': 'west', 'reason': '换名'})
    assert merged.status_code == 201, merged.text
    adjudications = client.get('/ledger/adjudications?status=pending', headers=auth['viewer']).json()
    assert adjudications, 'expected a pending conclusion conflict'
    adjudication = adjudications[0]
    assert client.post(f"/ledger/adjudications/{adjudication['id']}/decide",
                       headers=auth['analyst'], json={'decision': True}).status_code == 403
    decided = client.post(f"/ledger/adjudications/{adjudication['id']}/decide",
                          headers=auth['admin'], json={'decision': True, 'note': '现场复核'})
    assert decided.status_code == 200 and decided.json()['status'] == 'resolved'
    batch = client.post('/ledger/batches', headers=auth['analyst'], json={'name': 'HTTP 批次二'}).json()
    assert batch['conflict_count'] == 0
    revoked = client.post(f"/ledger/merges/{merged.json()['id']}/revoke",
                          headers=auth['admin'], json={'reason': '其实不是同一点'})
    assert revoked.status_code == 200
    assert revoked.json()['revoked_version'] is not None
    recomputed = client.post(f"/ledger/batches/{batch['id']}/recompute", headers=auth['analyst'])
    assert recomputed.status_code == 200


def test_identity_correction_flow_blocks_downstream(client, auth):
    dataset_id = None
    for batch in client.get('/ledger/batches', headers=auth['admin']).json():
        if batch['status'] == 'published':
            dataset_id = batch['published_dataset_id']
            break
    assert dataset_id
    jobs = client.get('/jobs', headers=auth['viewer']).json()
    created = client.post('/jobs', headers=auth['analyst'],
                          json={'name': '身份更正前分析', 'dataset_id': dataset_id, 'params': {}})
    assert created.status_code == 201, created.text
    job_id = created.json()['id']
    with Session() as db:
        job = db.get(Job, job_id)
        job.status = 'completed'
        db.commit()
        observation = db.query(Observation).filter_by(token='A队-c').first()
        observation_id = observation.id
    corrected = client.post(f'/ledger/observations/{observation_id}/correction', headers=auth['admin'],
                            json={'token': 'east-gate-fixed', 'point_name': '东门站(更正)', 'reason': '编号录错'})
    assert corrected.status_code == 200, corrected.text
    with Session() as db:
        assert db.get(Job, job_id).invalidated is True
    assert client.post(f'/jobs/{job_id}/approve', headers=auth['admin']).status_code == 409
    assert client.post('/reports', headers=auth['admin'],
                       json={'name': '失效分发', 'job_id': job_id}).status_code == 409
    history = client.get('/ledger/history', headers=auth['viewer']).json()
    assert history and history[0]['new_token'] == 'east-gate-fixed'


def test_legacy_daily_table_import_still_works(client, auth):
    records = [
        {'day': f'2026-11-{day:02d}', 'team': team, 'zone': zone,
         'observed': 10, 'passed': 7, 'frame_size': size}
        for day in (2, 3, 4)
        for team in ('A队', 'B队') for zone, size in (('中心区', 6200), ('外围区', 3800))
    ]
    created = client.post('/datasets', headers=auth['analyst'],
                          json={'name': '传统日表批次', 'source': 'legacy.csv', 'records': records})
    assert created.status_code == 201, created.text
    job = client.post('/jobs', headers=auth['analyst'],
                      json={'name': '传统日表分析', 'dataset_id': created.json()['id'],
                            'params': {'weight_mode': 'equal', 'minimum_cell': 10}})
    assert job.status_code == 201, job.text
    job_id = job.json()['id']
    with Session() as db:
        from src.worker import execute
        execute(job_id)
    finished = client.get(f'/jobs/{job_id}', headers=auth['viewer']).json()
    assert finished['status'] == 'completed'
    assert finished['result']['table']['rows']


def test_import_endpoint_mirrors_daily_table_once(client, auth):
    records = [
        {'day': f'2026-11-{day:02d}', 'team': team, 'zone': zone,
         'observed': 5, 'passed': 4, 'frame_size': size}
        for day in (6, 7)
        for team in ('A队', 'B队') for zone, size in (('中心区', 6200), ('外围区', 3800))
    ]
    created = client.post('/datasets', headers=auth['analyst'],
                          json={'name': '导入兼容日表', 'source': 'legacy.csv', 'records': records})
    assert created.status_code == 201, created.text
    dataset_id = created.json()['id']
    assert client.post(f'/ledger/import-dataset/{dataset_id}', headers=auth['analyst']).status_code == 403
    first = client.post(f'/ledger/import-dataset/{dataset_id}', headers=auth['admin'])
    assert first.status_code == 200, first.text
    assert first.json()['observations'] == 40
    second = client.post(f'/ledger/import-dataset/{dataset_id}', headers=auth['admin'])
    assert second.status_code == 409
