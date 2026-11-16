"""Contract tests for the per-point ledger.

Run from backend/:  python -m pytest tests -q
Uses a throwaway SQLite database; no Redis/Postgres required.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False).name
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB)
os.environ.setdefault('JWT_SECRET', 'ledger-tests')
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:1/0')
os.environ.setdefault('ARTIFACT_DIR', tempfile.mkdtemp())

import pytest
from fastapi import HTTPException

from src.config.database import Base, Session, engine
from src import models
from src.models import Dataset, Job, PublishAttempt, User
from src.schemas.job import JobInput
from src.schemas.report import ReportInput
from src.services import jobs, ledger, reports


class Row:
    def __init__(self, **values):
        self.__dict__.update(values)


def obs(db, token, name, zone='中心区', passed=True, day='2026-10-01', team='A队'):
    return ledger.create_observation(
        db, Row(day=day, team=team, zone=zone, point_name=name, token=token,
                passed=passed, note='', source='test'), 'tester')


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as session:
        ledger.set_frame(session, '中心区', 6200, 'tester')
        ledger.set_frame(session, '外围区', 3800, 'tester')
        session.commit()
        yield session


# ------------------------------------------------------------ counting / aliases

def test_same_point_same_day_same_team_counts_once(db):
    first = obs(db, 'p1', '东门站', passed=True)
    again = obs(db, 'p1', '东门站', passed=True)
    db.commit()
    assert first.id == again.id
    snapshot = ledger.compute_snapshot(db, ledger.current_version(db))
    assert snapshot['rows'][0]['observed'] == 1
    assert snapshot['rows'][0]['passed'] == 1


def test_renamed_point_merges_into_one_count(db):
    obs(db, 'p1', '东门站', passed=True)
    obs(db, 'p1-renamed', '东门服务站', passed=True)
    db.commit()
    before = ledger.compute_snapshot(db, ledger.current_version(db))
    assert before['rows'][0]['observed'] == 2  # duplicated before the alias is known
    ledger.create_merge(db, 'p1-renamed', 'p1', '换名', 'tester')
    db.commit()
    after = ledger.compute_snapshot(db, ledger.current_version(db))
    assert after['rows'][0]['observed'] == 1
    assert after['rows'][0]['passed'] == 1
    assert after['identity_map']['p1']['members'] == ['p1', 'p1-renamed']


def test_revoking_one_merge_keeps_later_independent_merge(db):
    obs(db, 'a', '甲点')
    obs(db, 'b', '乙点')
    obs(db, 'c', '丙点')
    db.commit()
    first = ledger.create_merge(db, 'a', 'b', '甲=乙', 'tester')
    db.commit()
    ledger.create_merge(db, 'b', 'c', '乙=丙', 'tester')
    db.commit()
    assert ledger.compute_snapshot(db, ledger.current_version(db))['rows'][0]['observed'] == 1
    ledger.revoke_merge(db, first.id, '误并', 'tester')
    db.commit()
    snapshot = ledger.compute_snapshot(db, ledger.current_version(db))
    assert snapshot['rows'][0]['observed'] == 2
    groups = {key: value['members'] for key, value in snapshot['identity_map'].items()}
    assert groups['a'] == ['a']
    assert groups['c'] == ['b', 'c']  # later independent merge survives


def test_merge_that_would_swallow_distinct_points_is_flagged_before_commit(db):
    obs(db, 'p1', '东门站', passed=True)
    obs(db, 'p2', '西门站', passed=False)
    db.commit()
    preview = ledger.preview_merge(db, 'p2', 'p1')
    assert preview['would_conflict_on_conclusion'] == [
        {'day': '2026-10-01', 'team': 'A队', 'conclusions': [False, True]}]
    assert preview['source_points'] == 1 and preview['target_points'] == 1


def test_a_merge_cannot_be_repeated_within_the_same_group(db):
    obs(db, 'a', '甲点')
    obs(db, 'b', '乙点')
    db.commit()
    ledger.create_merge(db, 'a', 'b', '别名', 'tester')
    db.commit()
    with pytest.raises(HTTPException) as error:
        ledger.create_merge(db, 'b', 'a', '反向', 'tester')
    assert error.value.status_code == 409


def test_merge_requires_known_points(db):
    with pytest.raises(HTTPException) as error:
        ledger.create_merge(db, 'ghost', 'phantom', '', 'tester')
    assert error.value.status_code == 404


# ------------------------------------------------------------ conflict handling

def test_conflicting_conclusion_blocks_publication_until_adjudicated(db):
    obs(db, 'p1', '东门站', passed=True)
    obs(db, 'p1', '东门站', passed=False)
    db.commit()
    batch = ledger.create_batch(db, '日表批次一', 'tester')
    db.commit()
    assert batch.conflict_count == 1
    assert batch.rows == []
    with pytest.raises(HTTPException) as error:
        ledger.publish_batch(db, batch.id, 'key-1', 'admin')
    assert error.value.status_code == 409
    assert '待裁定' in error.value.detail
    adjudication_id = batch.adjudication_ids[0]
    ledger.decide_adjudication(db, adjudication_id, True, '现场复核合格', 'admin')
    db.commit()
    ledger.recompute_batch(db, batch.id, 'tester')
    db.commit()
    assert batch.conflict_count == 0
    assert batch.rows[0]['observed'] == 1 and batch.rows[0]['passed'] == 1
    result = ledger.publish_batch(db, batch.id, 'key-1', 'admin')
    db.commit()
    assert result['published'] is True


def test_zone_conflict_between_merged_aliases_needs_adjudication(db):
    obs(db, 'p1', '东门站', zone='中心区')
    obs(db, 'p1-b', '东门站', zone='外围区')
    db.commit()
    ledger.create_merge(db, 'p1-b', 'p1', '换名后区域录错', 'tester')
    db.commit()
    batch = ledger.create_batch(db, '日表区域冲突', 'tester')
    db.commit()
    kinds = [conflict['kind'] for conflict in batch.conflicts]
    assert kinds == ['zone']
    with pytest.raises(HTTPException):
        ledger.publish_batch(db, batch.id, 'k', 'admin')
    ledger.decide_adjudication(db, batch.adjudication_ids[0], '外围区', '复核后属外围', 'admin')
    db.commit()
    ledger.recompute_batch(db, batch.id, 'tester')
    db.commit()
    assert batch.conflict_count == 0
    assert [row['zone'] for row in batch.rows] == ['外围区']


def test_adjudication_decision_must_be_one_of_the_options(db):
    obs(db, 'p1', '东门站', zone='中心区')
    obs(db, 'p1-b', '东门站', zone='外围区')
    db.commit()
    ledger.create_merge(db, 'p1-b', 'p1', '别名', 'tester')
    db.commit()
    batch = ledger.create_batch(db, '选项校验', 'tester')
    db.commit()
    with pytest.raises(HTTPException) as error:
        ledger.decide_adjudication(db, batch.adjudication_ids[0], '中间区', '', 'admin')
    assert error.value.status_code == 422


# ------------------------------------------------------------ versioning

def test_batch_pins_a_full_version_and_ignores_later_observations(db):
    obs(db, 'p1', '东门站')
    db.commit()
    batch = ledger.create_batch(db, '固定版本', 'tester')
    db.commit()
    pinned = batch.version
    assert len(batch.rows) == 1
    obs(db, 'p2', '西门站')
    db.commit()
    assert batch.version == pinned
    assert len(batch.rows) == 1  # the pinned snapshot did not move
    assert ledger.batch_stale(db, batch) is True


def test_late_computation_from_an_older_version_cannot_overwrite(db):
    obs(db, 'p1', '东门站', passed=True)
    db.commit()
    old_version = ledger.current_version(db)
    batch = ledger.create_batch(db, '并发批次', 'tester')
    db.commit()
    obs(db, 'p2', '西门站', passed=False)
    db.commit()
    ledger.recompute_batch(db, batch.id, 'tester')  # advances to newest
    db.commit()
    newest = batch.version
    assert len(batch.rows) == 1 and batch.rows[0]['observed'] == 2
    with pytest.raises(HTTPException) as error:
        ledger.recompute_batch(db, batch.id, 'tester', pinned_version=old_version)
    assert error.value.status_code == 409
    assert '晚回' in error.value.detail
    assert batch.version == newest
    assert batch.rows[0]['observed'] == 2  # untouched


def test_published_batch_cannot_be_recomputed(db):
    obs(db, 'p1', '东门站')
    db.commit()
    batch = ledger.create_batch(db, '发布后锁定', 'tester')
    db.commit()
    ledger.publish_batch(db, batch.id, 'lock-key', 'admin')
    db.commit()
    with pytest.raises(HTTPException) as error:
        ledger.recompute_batch(db, batch.id, 'tester')
    assert error.value.status_code == 409


def test_stale_batch_cannot_publish(db):
    obs(db, 'p1', '东门站')
    db.commit()
    batch = ledger.create_batch(db, '过期批次', 'tester')
    db.commit()
    obs(db, 'p2', '西门站')
    db.commit()
    with pytest.raises(HTTPException) as error:
        ledger.publish_batch(db, batch.id, 'stale-key', 'admin')
    assert error.value.status_code == 409
    assert '重算' in error.value.detail


# ------------------------------------------------------------ publication

def test_interrupted_publish_resumes_instead_of_creating_a_second_batch(db, monkeypatch):
    obs(db, 'p1', '东门站')
    db.commit()
    batch = ledger.create_batch(db, '中断恢复', 'tester')
    db.commit()
    monkeypatch.setenv('LEDGER_FAULT_INJECTION', 'true')
    with pytest.raises(HTTPException):
        ledger.publish_batch(db, batch.id, 'job-42', 'admin', interrupt_after_attempt=True)
    attempts = db.query(PublishAttempt).filter_by(idempotency_key='job-42').all()
    assert len(attempts) == 1 and attempts[0].status == 'failed'
    result = ledger.publish_batch(db, batch.id, 'job-42', 'admin')
    db.commit()
    assert result['resumed'] is True and result['published'] is True
    again = ledger.publish_batch(db, batch.id, 'job-42', 'admin')
    assert again['dataset_id'] == result['dataset_id']
    assert db.query(Dataset).count() == 1
    assert db.query(PublishAttempt).count() == 1


def test_published_rows_feed_the_existing_daily_table_pipeline(db):
    for team in ('A队', 'B队'):
        obs(db, f'{team}-c', '中心点', zone='中心区', passed=True, team=team)
        obs(db, f'{team}-o', '外围点', zone='外围区', passed=False, team=team)
    db.commit()
    batch = ledger.create_batch(db, '接入日表', 'tester')
    db.commit()
    result = ledger.publish_batch(db, batch.id, 'pipeline', 'admin')
    db.commit()
    from src.services.business import analyze
    dataset = db.get(Dataset, result['dataset_id'])
    assert sorted(dataset.records[0]) == ['day', 'frame_size', 'observed', 'passed', 'team', 'zone']
    analyzed = analyze(dataset.records, {'weight_mode': 'population', 'minimum_cell': 10})
    assert len(analyzed['table']['rows']) == 4


def test_batch_trace_links_original_observation_and_ruling(db):
    obs(db, 'p1', '东门站', passed=True)
    obs(db, 'p1', '东门站', passed=False)
    db.commit()
    batch = ledger.create_batch(db, '可追溯批次', 'tester')
    db.commit()
    ledger.decide_adjudication(db, batch.adjudication_ids[0], False, '现场复核不合格', 'admin')
    db.commit()
    ledger.recompute_batch(db, batch.id, 'tester')
    db.commit()
    point = batch.trace[0]['points'][0]
    assert point['observation_ids'] == [1, 2]
    assert batch.trace[0]['adjudications'] == batch.adjudication_ids
    assert point['decisions'] == [{'kind': 'conclusion', 'decision': False}]


# ------------------------------------------------------------ identity correction

def _user(db, role='admin'):
    user = User(email=f'{role}@test.local', name=role, role=role, password='x')
    db.add(user)
    db.flush()
    return user


def test_identity_correction_invalidates_unapproved_analysis(db):
    for team in ('A队', 'B队'):
        obs(db, f'{team}-c', '中心点', zone='中心区', passed=True, team=team)
        obs(db, f'{team}-o', '外围点', zone='外围区', passed=False, team=team)
    db.commit()
    batch = ledger.create_batch(db, '更正批次', 'tester')
    db.commit()
    dataset_id = ledger.publish_batch(db, batch.id, 'correction', 'admin')['dataset_id']
    db.commit()
    user = _user(db)
    job = jobs.create(db, JobInput(name='未批准分析', dataset_id=dataset_id, params={}), user)
    db.commit()
    job.status = 'completed'
    db.commit()
    ledger.correct_identity(db, 1, 'center-a-fixed', '中心点A', '编号录错', 'admin')
    db.commit()
    assert job.invalidated is True
    assert '身份更正' in job.invalid_reason
    with pytest.raises(HTTPException) as error:
        jobs.approve(db, job)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        reports.create(db, ReportInput(name='分发计划', job_id=job.id))
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        jobs.create(db, JobInput(name='新分析', dataset_id=dataset_id, params={}), user)
    assert error.value.status_code == 409


def test_identity_correction_leaves_approved_material_untouched(db):
    obs(db, 'p1', '东门站')
    db.commit()
    batch = ledger.create_batch(db, '保留批准', 'tester')
    db.commit()
    dataset_id = ledger.publish_batch(db, batch.id, 'keep', 'admin')['dataset_id']
    db.commit()
    user = _user(db)
    original_result = {'summary': '原结论', 'table': {'columns': [], 'rows': []}}
    job = Job(name='已批准分析', dataset_id=dataset_id, params={'weight_mode': 'population'},
              status='completed', result=original_result, approved=True,
              created_by=user.id, created_at=1.0)
    db.add(job)
    db.flush()
    ledger.correct_identity(db, 1, 'p1-fixed', '东门站更正', '编号录错', 'admin')
    db.commit()
    assert job.approved is True and job.invalidated is False
    assert job.result == original_result and job.params == {'weight_mode': 'population'}
    report = reports.create(db, ReportInput(name='沿用批准', job_id=job.id))
    assert report.id is not None


def test_corrected_identity_can_then_be_merged(db):
    obs(db, 'p1', '甲点')
    obs(db, 'p2', '乙点')
    db.commit()
    ledger.correct_identity(db, 1, 'p1-new', '甲点(更正)', '编号录入错误', 'admin')
    db.commit()
    ledger.create_merge(db, 'p1-new', 'p2', '更正后确认同点', 'tester')
    db.commit()
    snapshot = ledger.compute_snapshot(db, ledger.current_version(db))
    assert snapshot['rows'][0]['observed'] == 1
    assert snapshot['identity_map']['p2']['members'] == ['p1-new', 'p2']


def test_agreeing_evidence_keeps_a_settled_conclusion(db):
    obs(db, 'p1', '东门站', passed=True)
    obs(db, 'p1', '东门站', passed=False)
    db.commit()
    batch = ledger.create_batch(db, '一致证据', 'tester')
    db.commit()
    adjudication_id = batch.adjudication_ids[0]
    ledger.decide_adjudication(db, adjudication_id, True, '现场复核合格', 'admin')
    db.commit()
    settled_version = ledger.current_version(db)
    obs(db, 'p1-third', '东门站', passed=True)  # agrees with the ruling
    db.commit()
    ledger.create_merge(db, 'p1-third', 'p1', '别名', 'tester')
    db.commit()
    adjudication = db.get(models.Adjudication, adjudication_id)
    assert adjudication.status == 'resolved' and adjudication.reopened is False
    current = ledger.compute_snapshot(db, ledger.current_version(db))
    conclusion = [c for c in current['conflicts'] if c['kind'] == 'conclusion'][0]
    assert conclusion['resolved'] is True and conclusion['decision'] is True
    assert current['rows'][0]['observed'] == 1
    # The older pinned replay also still honours the ruling.
    replay = ledger.compute_snapshot(db, settled_version)
    assert [c for c in replay['conflicts'] if c['kind'] == 'conclusion'][0]['resolved'] is True


def test_contradicting_evidence_reopens_a_settled_conclusion(db):
    obs(db, 'q1', '北门站', passed=True)
    obs(db, 'q1', '北门站', passed=False)
    db.commit()
    batch = ledger.create_batch(db, '反转结论', 'tester')
    db.commit()
    adjudication_id = batch.adjudication_ids[0]
    ledger.decide_adjudication(db, adjudication_id, True, '当时合格', 'admin')
    db.commit()
    settled_version = ledger.current_version(db)
    obs(db, 'q1-late', '北门站', passed=False)
    db.commit()
    ledger.create_merge(db, 'q1-late', 'q1', '别名', 'tester')
    db.commit()
    adjudication = db.get(models.Adjudication, adjudication_id)
    assert adjudication.status == 'resolved'          # history is not rewritten
    assert adjudication.reopened is True              # but it is surfaced for re-decision
    snapshot = ledger.compute_snapshot(db, ledger.current_version(db))
    conclusion = [c for c in snapshot['conflicts'] if c['kind'] == 'conclusion'][0]
    assert conclusion['resolved'] is False and conclusion['stale_decision'] is True
    assert snapshot['rows'] == []                      # blocked until re-decided
    old = ledger.compute_snapshot(db, settled_version)
    assert [c for c in old['conflicts'] if c['kind'] == 'conclusion'][0]['resolved'] is True
    ledger.decide_adjudication(db, adjudication_id, True, '再次复核仍合格', 'admin')
    db.commit()
    assert adjudication.reopened is False
    assert adjudication.decided_version > settled_version
    assert ledger.compute_snapshot(db, ledger.current_version(db))['rows']


def test_zone_conflict_on_a_new_alias_reopens_it(db):
    obs(db, 'p1', '东门站', zone='中心区')
    obs(db, 'p1-2', '东门站', zone='外围区')
    db.commit()
    ledger.create_merge(db, 'p1-2', 'p1', '别名', 'tester')
    db.commit()
    batch = ledger.create_batch(db, '区域重开', 'tester')
    db.commit()
    zone_adjudication = [row for row in db.query(models.Adjudication).all() if row.kind == 'zone'][0]
    ledger.decide_adjudication(db, zone_adjudication.id, '中心区', '复核归中心', 'admin')
    db.commit()
    assert ledger.compute_snapshot(db, ledger.current_version(db))['rows'][0]['zone'] == '中心区'
    obs(db, 'p1-3', '东门站', zone='外围区')
    db.commit()
    ledger.create_merge(db, 'p1-3', 'p1', '第三条别名', 'tester')
    db.commit()
    assert zone_adjudication.reopened is True
    assert ledger.compute_snapshot(db, ledger.current_version(db))['rows'] == []


def test_importing_an_existing_daily_table_mirrors_totals(db):
    from src.models import Dataset
    from src.utils.serialization import checksum
    records = [
        {'day': '2026-11-05', 'team': team, 'zone': zone, 'observed': 4, 'passed': 3, 'frame_size': size}
        for team in ('A队', 'B队') for zone, size in (('中心区', 6200), ('外围区', 3800))
    ]
    dataset = Dataset(name='待导入日表', source='legacy.csv', records=records,
                      checksum=checksum(records), created_at=1.0)
    db.add(dataset)
    db.flush()
    result = ledger.import_daily_table(db, dataset, 'tester')
    db.commit()
    assert result['observations'] == 16
    snapshot = ledger.compute_snapshot(db, ledger.current_version(db))
    assert sum(row['observed'] for row in snapshot['rows']) == 16
    assert sum(row['passed'] for row in snapshot['rows']) == 12
    with pytest.raises(HTTPException) as error:
        ledger.import_daily_table(db, dataset, 'tester')
    assert error.value.status_code == 409


def test_concurrent_writes_never_duplicate_a_point_or_lose_a_version(db, tmp_path):
    import threading
    from src.config.database import Session
    ledger.set_frame(db, '中心区', 6200, 'tester')
    for token in ('p1', 'p2', 'p3'):
        obs(db, token, token)
    db.commit()
    outcomes = []

    def write_observation():
        with Session() as other:
            try:
                ledger.create_observation(
                    other, Row(day='2026-10-01', team='A队', zone='中心区', point_name='p1',
                               token='p1', passed=True, note='', source='t'), 'tester')
                other.commit()
                outcomes.append('obs')
            except Exception:
                other.rollback()
                outcomes.append('obs-error')

    def write_merge():
        with Session() as other:
            try:
                ledger.create_merge(other, 'p3', 'p2', '并发归并', 'tester')
                other.commit()
                outcomes.append('merge')
            except Exception:
                other.rollback()
                outcomes.append('merge-error')

    threads = [threading.Thread(target=write_observation), threading.Thread(target=write_merge)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert 'obs' in outcomes and 'merge' in outcomes
    db.commit()  # drop this session's snapshot so it sees the concurrent commits
    snapshot = ledger.compute_snapshot(db, ledger.current_version(db))
    assert [row['observed'] for row in snapshot['rows']] == [2]  # p1 plus merged p2+p3
    versions = [row.version for row in db.query(models.Observation).all()]
    assert len(set(versions)) >= 1 and ledger.current_version(db) >= max(versions)
