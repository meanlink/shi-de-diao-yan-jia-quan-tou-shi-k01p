"""Per-point observation ledger: aliases, adjudication, versioned daily-table batches.

Design rules encoded here:
- Observations and merges are append-only events stamped with a ledger version.
- A batch pins one ledger version and replays observations, identity corrections,
  merges, revocations and adjudications as of that version.
- Revoking one merge never erases later independent merges; resolution replays
  every still-active edge up to the pinned version.
- Same canonical point + day + team counts once. A differing conclusion or zone
  becomes an adjudication and blocks publication until cleared.
- Publication is idempotent per batch/idempotency key and resumed, never duplicated.
"""
from __future__ import annotations

import hashlib
import os
import time
from collections import defaultdict
from datetime import date

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from src.models import (
    Adjudication, Dataset, IdentityCorrection, Job, LedgerBatch, LedgerDatasetFlag,
    LedgerFrame, LedgerFrameEvent, LedgerState, Observation, PointMerge, PublishAttempt,
)
from src.schemas.ledger import ObservationInput
from src.services.audit import record
from src.utils.serialization import checksum

TEAM_VALUES = ('A队', 'B队')


# ---------------------------------------------------------------- clock / lookup

def _now() -> float:
    return time.time()


def _ensure_state(db) -> LedgerState:
    """Create the clock row once; two concurrent first writers must not fail each other."""
    state = db.get(LedgerState, 1)
    if state is not None:
        return state
    savepoint = db.begin_nested()
    try:
        db.add(LedgerState(id=1, version=0))
        db.flush()
        savepoint.commit()
    except IntegrityError:
        # Another request won the insert; its row is the one we read next.
        savepoint.rollback()
    state = db.get(LedgerState, 1)
    if state is None:
        raise HTTPException(503, '台账时钟初始化冲突，请重试')
    return state


def current_version(db) -> int:
    return int(_ensure_state(db).version)


def bump_version(db) -> int:
    """Atomic increment. The UPDATE row lock is held until commit, so versions stay dense."""
    state = _ensure_state(db)
    db.execute(update(LedgerState).where(LedgerState.id == 1)
               .values(version=LedgerState.version + 1)
               .execution_options(synchronize_session='fetch'))
    db.expire(state)
    db.flush()
    return int(db.get(LedgerState, 1).version)


def _normalize_token(value) -> str:
    token = ' '.join(str(value or '').split())
    if not token:
        return ''
    if len(token) > 220:
        raise HTTPException(422, '服务点编号不能超过 220 个字符')
    return token


def _normalize_name(value) -> str:
    return ' '.join(str(value or '').split())


def _derive_token(zone: str, name: str) -> str:
    return f'point:{zone}:{name}'


def _parse_day(value) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise HTTPException(422, '观察日必须是 YYYY-MM-DD 格式') from None


def _parse_passed(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in ('true', '1', '通过', '合格', 'yes'):
        return True
    if text in ('false', '0', '不通过', '不合格', 'no'):
        return False
    raise HTTPException(422, '结论必须是布尔值（合格 / 不合格）')


def _validate_observation_fields(data) -> dict:
    day = _parse_day(getattr(data, 'day', ''))
    team = str(getattr(data, 'team', '')).strip()
    if team not in TEAM_VALUES:
        raise HTTPException(422, '采样队必须是 A队 或 B队')
    zone = _normalize_name(getattr(data, 'zone', ''))
    if not zone:
        raise HTTPException(422, '区域层不能为空')
    name = _normalize_name(getattr(data, 'point_name', ''))
    if not name:
        raise HTTPException(422, '服务点名称不能为空')
    token = _normalize_token(getattr(data, 'token', '')) or _derive_token(zone, name)
    return {
        'day': day, 'team': team, 'zone': zone, 'point_name': name,
        'token': token, 'passed': _parse_passed(getattr(data, 'passed', None)),
        'note': str(getattr(data, 'note', '') or '').strip()[:500],
        'source': str(getattr(data, 'source', '') or 'manual').strip()[:200],
    }


def _fingerprint(token: str, day: str, team: str, passed: bool) -> str:
    return hashlib.sha256(f'{token}|{day}|{team}|{int(passed)}'.encode()).hexdigest()


# ---------------------------------------------------------------- writes

def create_observation(db, data, actor: str) -> Observation:
    values = _validate_observation_fields(data)
    existing = db.scalars(select(Observation).where(Observation.token == values['token'],
                                                    Observation.day == values['day'],
                                                    Observation.team == values['team'])).all()
    for row in existing:
        if row.passed == values['passed'] and row.zone == values['zone']:
            # Same point/day/team/zone/conclusion is counted once; repeats are no-ops.
            return row
    version = bump_version(db)
    fingerprint = _fingerprint(values['token'], values['day'], values['team'], values['passed'])
    observation = Observation(version=version, fingerprint=fingerprint, created_by=actor,
                              created_at=_now(), **values)
    db.add(observation)
    try:
        db.flush()
    except IntegrityError:
        # A concurrent writer inserted the same point/day/team/conclusion first.
        db.rollback()
        existing = db.scalar(select(Observation).where(
            Observation.token == values['token'], Observation.day == values['day'],
            Observation.team == values['team'], Observation.zone == values['zone'],
            Observation.passed == values['passed']))
        if existing is None:
            raise HTTPException(409, '同一服务点并发写入冲突，请重试') from None
        return existing
    refresh_adjudications(db, actor)
    record(db, actor, 'ledger.observation.create', {'id': observation.id, 'token': observation.token,
                                                    'day': observation.day, 'team': observation.team,
                                                    'zone': observation.zone, 'passed': observation.passed,
                                                    'version': version})
    return observation


def create_observations_bulk(db, items, actor: str, source: str = 'import') -> list[Observation]:
    """Import many observations under one version. Per-row audit would drown the trail."""
    values = [_validate_observation_fields(item) for item in items]
    if not values:
        return []
    existing = {(row.token, row.day, row.team, row.zone, bool(row.passed))
                for row in db.scalars(select(Observation))}
    version = bump_version(db)
    created = []
    seen: set[tuple[str, str, str, str, bool]] = set()
    for value in values:
        key = (value['token'], value['day'], value['team'], value['zone'], bool(value['passed']))
        if key in seen or key in existing:
            continue
        seen.add(key)
        observation = Observation(version=version,
                                  fingerprint=_fingerprint(value['token'], value['day'], value['team'], value['passed']),
                                  created_by=actor, created_at=_now(), **value)
        db.add(observation)
        created.append(observation)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '导入数据与其他写入并发冲突，请重试；本次导入未生效，不会重复计样') from None
    refresh_adjudications(db, actor)
    record(db, actor, 'ledger.observation.import', {'count': len(created), 'submitted': len(values),
                                                    'source': source, 'version': version})
    return created


def _known_tokens(db, version: int) -> set[str]:
    tokens = set(db.scalars(select(Observation.token).where(Observation.version <= version)))
    for correction in db.scalars(select(IdentityCorrection).where(IdentityCorrection.version <= version)):
        tokens.add(correction.old_token)
        tokens.add(correction.new_token)
    for merge in db.scalars(select(PointMerge).where(PointMerge.version <= version)):
        tokens.add(merge.source_token)
        tokens.add(merge.target_token)
    return tokens


def _active_merges(merges, version: int):
    """Merges that exist as of a pinned version. Revoking one never touches the others."""
    return [merge for merge in merges
            if merge.version <= version
            and (merge.revoked_version is None or merge.revoked_version > version)]


def _components(merges) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Undirected alias graph -> (token -> canonical, canonical -> members).

    Edges are equivalence relations, so later independent merges stay grouped even
    after an earlier edge is revoked. The canonical token is the latest merge target
    in the component (the name a rename points at), falling back to lexical order.
    """
    parent: dict[str, str] = {}

    def find(token: str) -> str:
        parent.setdefault(token, token)
        while parent[token] != token:
            parent[token] = parent[parent[token]]
            token = parent[token]
        return token

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    incoming: dict[str, tuple[int, int]] = {}
    for merge in merges:
        union(merge.source_token, merge.target_token)
        rank = (merge.version, merge.id)
        if rank > incoming.get(merge.target_token, (-1, -1)):
            incoming[merge.target_token] = rank
    groups: dict[str, list[str]] = defaultdict(list)
    for token in list(parent):
        groups[find(token)].append(token)
    canonical_of, members = {}, {}
    for tokens in groups.values():
        chosen = min(tokens, key=lambda token: (-incoming.get(token, (0, 0))[0], -incoming.get(token, (0, 0))[1], token))
        canonical_of.update({token: chosen for token in tokens})
        members[chosen] = sorted(tokens)
    return canonical_of, members


def _resolve(token: str, canonical_of: dict[str, str]) -> tuple[str, list[str]]:
    canonical = canonical_of.get(token, token)
    if canonical == token:
        return token, []
    return canonical, [token]


def create_merge(db, source, target, reason, actor: str) -> PointMerge:
    source = _normalize_token(source)
    target = _normalize_token(target)
    if not source or not target:
        raise HTTPException(422, '归并必须给出源服务点与目标服务点编号')
    if source == target:
        raise HTTPException(422, '不能把服务点归并到自身')
    version = bump_version(db)
    known = _known_tokens(db, version)
    for token in (source, target):
        if token not in known:
            db.rollback()
            raise HTTPException(404, f'服务点 {token} 在台账中没有观察记录')
    merges = list(db.scalars(select(PointMerge).where(PointMerge.version <= version).order_by(PointMerge.version, PointMerge.id)))
    canonical_of, _ = _components(_active_merges(merges, version))
    if canonical_of.get(source, source) == canonical_of.get(target, target):
        db.rollback()
        raise HTTPException(409, '两个服务点已在同一别名组中，无需重复归并')
    merge = PointMerge(version=version, source_token=source, target_token=target,
                       reason=str(reason or '').strip()[:500], active=True,
                       created_by=actor, created_at=_now())
    db.add(merge)
    db.flush()
    refresh_adjudications(db, actor)
    record(db, actor, 'ledger.merge.create', {'id': merge.id, 'source': source,
                                              'target': target, 'version': version})
    return merge


def preview_merge(db, source, target) -> dict:
    """Show what a merge would swallow before it is written."""
    source = _normalize_token(source)
    target = _normalize_token(target)
    if not source or not target:
        raise HTTPException(422, '归并必须给出源服务点与目标服务点编号')
    version = current_version(db)
    merges = list(db.scalars(select(PointMerge).where(PointMerge.version <= version).order_by(PointMerge.version, PointMerge.id)))
    canonical_of, members = _components(_active_merges(merges, version))
    if canonical_of.get(source, source) == canonical_of.get(target, target):
        raise HTTPException(409, '两个服务点已在同一别名组中')
    observations = list(db.scalars(select(Observation).where(Observation.version <= version)))
    source_rows = [row for row in observations if canonical_of.get(row.token, row.token) == canonical_of.get(source, source)]
    target_rows = [row for row in observations if canonical_of.get(row.token, row.token) == canonical_of.get(target, target)]
    paired: dict[tuple[str, str], set] = defaultdict(set)
    for row in source_rows + target_rows:
        paired[(row.day, row.team)].add(bool(row.passed))
    zones = sorted({row.zone for row in source_rows + target_rows})
    conflicts = [{'day': day, 'team': team, 'conclusions': sorted(values)}
                 for (day, team), values in paired.items() if len(values) > 1]
    source_names = sorted({row.point_name for row in source_rows})
    target_names = sorted({row.point_name for row in target_rows})
    return {'source': source, 'target': target,
            'source_names': source_names, 'target_names': target_names,
            'distinct_names': set(source_names) != set(target_names),
            'source_points': len(members.get(canonical_of.get(source, source), [source])),
            'target_points': len(members.get(canonical_of.get(target, target), [target])),
            'source_observations': len(source_rows), 'target_observations': len(target_rows),
            'zones': zones, 'would_conflict_on_conclusion': conflicts,
            'would_conflict_on_zone': len(zones) > 1}


def revoke_merge(db, merge_id: int, reason, actor: str) -> PointMerge:
    merge = db.get(PointMerge, merge_id)
    if not merge:
        raise HTTPException(404, '归并记录不存在')
    if merge.revoked_version is not None:
        raise HTTPException(409, '该归并已撤销')
    version = bump_version(db)
    merge.active = False
    merge.revoked_version = version
    merge.revoked_by = actor
    merge.revoked_reason = str(reason or '').strip()[:500]
    merge.revoked_at = _now()
    db.flush()
    refresh_adjudications(db, actor)
    record(db, actor, 'ledger.merge.revoke', {'id': merge.id, 'source': merge.source_token,
                                              'target': merge.target_token, 'version': version})
    return merge


def set_frame(db, zone: str, size: int, actor: str) -> LedgerFrame:
    zone = _normalize_name(zone)
    if not zone:
        raise HTTPException(422, '区域层不能为空')
    try:
        value = int(size)
    except (TypeError, ValueError):
        raise HTTPException(422, '抽样框规模必须是整数') from None
    if not 1 <= value <= 1_000_000_000:
        raise HTTPException(422, '抽样框规模必须为 1 至 10 亿之间的整数')
    version = bump_version(db)
    frame = db.get(LedgerFrame, zone)
    if frame is None:
        frame = LedgerFrame(zone=zone, size=value, version=version)
        db.add(frame)
    else:
        frame.size = value
        frame.version = version
    db.add(LedgerFrameEvent(version=version, zone=zone, size=value,
                            created_by=actor, created_at=_now()))
    db.flush()
    record(db, actor, 'ledger.frame.set', {'zone': zone, 'size': value, 'version': version})
    return frame


# ---------------------------------------------------------------- replay / compute

def _corrections_at(db, version: int) -> dict[int, IdentityCorrection]:
    latest: dict[int, IdentityCorrection] = {}
    for correction in db.scalars(select(IdentityCorrection).where(IdentityCorrection.version <= version).order_by(IdentityCorrection.version, IdentityCorrection.id)):
        latest[correction.observation_id] = correction
    return latest


def _adjudications_by_signature(db) -> dict[str, Adjudication]:
    return {row.signature: row for row in db.scalars(select(Adjudication))}


def signature_for(kind: str, day: str, team: str, cluster: str) -> str:
    return hashlib.sha256(f'{kind}|{day}|{team}|{cluster}'.encode()).hexdigest()


def compute_snapshot(db, version: int, require_frames: bool = True) -> dict:
    observations = list(db.scalars(select(Observation).where(Observation.version <= version).order_by(Observation.version, Observation.id)))
    merges = list(db.scalars(select(PointMerge).where(PointMerge.version <= version).order_by(PointMerge.version, PointMerge.id)))
    active = _active_merges(merges, version)
    canonical_of, member_of = _components(active)
    corrections = _corrections_at(db, version)
    frames: dict[str, int] = {}
    for event in db.scalars(select(LedgerFrameEvent).where(LedgerFrameEvent.version <= version).order_by(LedgerFrameEvent.version, LedgerFrameEvent.id)):
        frames[event.zone] = event.size
    if not frames:  # databases upgraded from the first snapshot only have the current row
        frames = {frame.zone: frame.size for frame in db.scalars(select(LedgerFrame))}
    adjudications = _adjudications_by_signature(db)

    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    identity_map: dict[str, dict] = {}
    observation_ids: list[int] = []
    for obs in observations:
        observation_ids.append(obs.id)
        correction = corrections.get(obs.id)
        token = correction.new_token if correction else obs.token
        name = correction.new_name if correction else obs.point_name
        canonical, chain = _resolve(token, canonical_of)
        entry = {
            'observation_id': obs.id, 'token': token, 'original_token': obs.token,
            'name': name, 'original_name': obs.point_name, 'zone': obs.zone,
            'passed': bool(obs.passed), 'day': obs.day, 'team': obs.team,
            'version': obs.version, 'note': obs.note, 'source': obs.source,
            'canonical': canonical, 'chain': chain,
            'corrected': correction is not None,
        }
        grouped[(canonical, obs.day, obs.team)].append(entry)
        info = identity_map.setdefault(canonical, {'token': canonical, 'name': name, 'members': [],
                                                   'observations': 0, 'names': []})
        info['observations'] += 1
        if token not in info['members']:
            info['members'].append(token)
        if name not in info['names']:
            info['names'].append(name)
        info['name'] = name

    conflicts: list[dict] = []
    rows: dict[tuple[str, str, str], dict] = {}
    trace: list[dict] = []
    for (canonical, day, team), entries in sorted(grouped.items()):
        zones = sorted({entry['zone'] for entry in entries})
        conclusions = sorted({entry['passed'] for entry in entries})
        zone_conflict = len(zones) > 1
        conclusion_conflict = len(conclusions) > 1
        entry_adjudications = []
        zone = zones[0] if not zone_conflict else None
        passed = conclusions[0] if not conclusion_conflict else None
        blocked = False
        local_decisions = []
        for kind, conflicted, options in (
            ('zone', zone_conflict, zones),
            ('conclusion', conclusion_conflict, conclusions),
        ):
            if not conflicted:
                continue
            signature = signature_for(kind, day, team, canonical)
            adjudication = adjudications.get(signature)
            decision, resolved, stale_decision = None, False, False
            if adjudication and adjudication.status == 'resolved' and adjudication.decided_version is not None and adjudication.decided_version <= version:
                decision = adjudication.decision
                field = 'zone' if kind == 'zone' else 'passed'
                covered = set(adjudication.decided_entry_ids or [])
                fresh = [entry[field] for entry in entries if entry['observation_id'] not in covered]
                if kind == 'zone' and decision in options:
                    zone, resolved = decision, True
                elif kind == 'conclusion' and isinstance(decision, bool) and decision in options:
                    passed, resolved = decision, True
                if resolved and any(value != decision for value in fresh):
                    # Evidence that contradicts the ruling arrived afterwards: re-decide.
                    resolved = False
                    stale_decision = True
            if not resolved:
                blocked = True
            conflicts.append({
                'signature': signature, 'kind': kind, 'day': day, 'team': team,
                'cluster': canonical, 'options': options,
                'entries': [entry['observation_id'] for entry in entries],
                'adjudication_id': adjudication.id if adjudication else None,
                'resolved': resolved, 'decision': decision, 'stale_decision': stale_decision,
            })
            if adjudication:
                entry_adjudications.append(adjudication.id)
            if resolved:
                local_decisions.append({'kind': kind, 'decision': decision})
        if blocked or zone is None or passed is None:
            continue
        row = rows.setdefault((day, team, zone), {
            'day': day, 'team': team, 'zone': zone, 'observed': 0, 'passed': 0,
            'frame_size': None, 'points': [], 'adjudications': [],
        })
        row['observed'] += 1
        row['passed'] += int(bool(passed))
        row['points'].append({
            'token': canonical, 'name': identity_map.get(canonical, {}).get('name', ''),
            'observation_ids': [entry['observation_id'] for entry in entries],
            'aliases': sorted({entry['token'] for entry in entries}),
            'passed': bool(passed), 'decisions': list(local_decisions),
        })
        for adjudication_id in entry_adjudications:
            if adjudication_id and adjudication_id not in row['adjudications']:
                row['adjudications'].append(adjudication_id)

    missing_frames = sorted({key[2] for key in rows} - set(frames))
    if missing_frames and require_frames:
        raise HTTPException(422, '请先登记区域抽样框规模：' + '、'.join(missing_frames))
    output = []
    for (day, team, zone), row in sorted(rows.items()):
        row['frame_size'] = frames.get(zone)
        output.append(row)
        trace.append({
            'day': day, 'team': team, 'zone': zone, 'observed': row['observed'],
            'passed': row['passed'], 'frame_size': frames.get(zone),
            'points': row['points'], 'adjudications': row['adjudications'],
        })

    used_merges = [
        {'id': merge.id, 'source': merge.source_token, 'target': merge.target_token,
         'version': merge.version, 'revoked_version': merge.revoked_version,
         'reason': merge.reason, 'created_by': merge.created_by}
        for merge in active
    ]
    return {
        'version': version, 'rows': output, 'trace': trace, 'conflicts': conflicts,
        'identity_map': {token: {**info, 'members': member_of.get(token, info['members'])}
                         for token, info in identity_map.items()},
        'merges': used_merges,
        'observation_ids': observation_ids,
        'warnings': [f"服务点 {token} 归并了不同名称：{'、'.join(info['names'])}；请确认没有把不同服务点并在一起。"
                     for token, info in sorted(identity_map.items()) if len(info['names']) > 1],
        'unresolved_conflict_count': sum(1 for conflict in conflicts if not conflict['resolved']),
        'summary': f'版本 {version}：{len(observation_ids)} 条逐点观察，归并为 {len(identity_map)} 个服务点，'
                   f'生成 {len(output)} 行日表；未裁定冲突 {sum(1 for c in conflicts if not c["resolved"])} 项。',
    }


def _reconcile_adjudications(db, snapshot, actor: str) -> list[int]:
    """Create missing adjudication rows; refresh current evidence on all of them."""
    ids: list[int] = []
    current: set[str] = set()
    existing = _adjudications_by_signature(db)
    for conflict in snapshot['conflicts']:
        current.add(conflict['signature'])
        adjudication = existing.get(conflict['signature'])
        if adjudication is None:
            adjudication = Adjudication(
                signature=conflict['signature'], kind=conflict['kind'], day=conflict['day'],
                team=conflict['team'], cluster=conflict['cluster'], entry_ids=conflict['entries'],
                options=conflict['options'], status='pending', created_at=_now())
            db.add(adjudication)
            db.flush()
            existing[adjudication.signature] = adjudication
        else:
            adjudication.entry_ids = conflict['entries']
            adjudication.options = conflict['options']
            if adjudication.status == 'resolved' and not conflict['resolved']:
                # New evidence invalidated the earlier ruling: surface it for a fresh decision.
                # decided_version/decision stay intact so older pinned versions still replay.
                adjudication.reopened = True
                conflict['stale_decision'] = True
        conflict['adjudication_id'] = adjudication.id
        if adjudication.id not in ids:
            ids.append(adjudication.id)
    for signature, adjudication in existing.items():
        if signature not in current and adjudication.status == 'pending':
            adjudication.status = 'superseded'
            adjudication.resolution_note = adjudication.resolution_note or '冲突随台账更新消失，自动收起'
        elif signature in current and adjudication.status == 'superseded':
            adjudication.status = 'pending'
    return ids


def refresh_adjudications(db, actor: str = 'ledger') -> list[int]:
    """Detect conclusion/zone conflicts as observations arrive, ahead of batch creation."""
    snapshot = compute_snapshot(db, current_version(db), require_frames=False)
    ids = _reconcile_adjudications(db, snapshot, actor)
    db.flush()
    return ids


def store_batch_result(db, batch: LedgerBatch, version: int, snapshot: dict, actor: str) -> bool:
    """Version-guarded write. A late, older computation must never replace newer rows."""
    if version < batch.result_stamp:
        return False
    batch.version = version
    batch.result_stamp = version
    batch.rows = snapshot['rows']
    batch.trace = snapshot['trace']
    batch.identity_map = snapshot['identity_map']
    batch.merges = snapshot['merges']
    batch.conflicts = snapshot['conflicts']
    batch.warnings = snapshot['warnings']
    batch.conflict_count = snapshot['unresolved_conflict_count']
    batch.snapshot = {'version': version, 'observation_ids': snapshot['observation_ids'],
                      'merge_ids': [merge['id'] for merge in snapshot['merges']]}
    batch.adjudication_ids = _reconcile_adjudications(db, snapshot, actor)
    batch.checksum = checksum({'version': version, 'rows': snapshot['rows'],
                               'conflicts': [c['signature'] for c in snapshot['conflicts']]})
    batch.computed_at = _now()
    batch.computed_by = actor
    batch.stale = False
    batch.correction_count = batch.correction_count or 0
    db.flush()
    return True


def create_batch(db, name, actor: str) -> LedgerBatch:
    name = _normalize_name(name)
    if len(name) < 2:
        raise HTTPException(422, '批次名称至少需要两个字符')
    if db.scalar(select(LedgerBatch).where(LedgerBatch.name == name)):
        raise HTTPException(409, '该日表批次名已存在')
    version = current_version(db)
    batch = LedgerBatch(name=name, version=version, status='pending_review', created_by=actor, created_at=_now())
    db.add(batch)
    db.flush()
    snapshot = compute_snapshot(db, version)
    store_batch_result(db, batch, version, snapshot, actor)
    record(db, actor, 'ledger.batch.create', {'id': batch.id, 'version': version,
                                              'rows': len(batch.rows), 'conflicts': batch.conflict_count})
    return batch


def recompute_batch(db, batch_id: int, actor: str, pinned_version: int | None = None) -> LedgerBatch:
    """Replay at the newest version, or at a caller-pinned version to model a late computation.

    When concurrent writes have already pushed the batch further, the late result is
    refused instead of overwriting the newer conclusion.
    """
    batch = db.get(LedgerBatch, batch_id, with_for_update=True)
    if not batch:
        raise HTTPException(404, '日表批次不存在')
    if batch.status == 'published':
        raise HTTPException(409, '已发布批次不可重算；请基于最新台账新建批次')
    version = current_version(db) if pinned_version is None else int(pinned_version)
    if version < 0 or version > current_version(db):
        raise HTTPException(422, '不能固定到尚未发生的台账版本')
    snapshot = compute_snapshot(db, version)
    if not store_batch_result(db, batch, version, snapshot, actor):
        raise HTTPException(409, f'较旧的台账版本 {version} 计算结果晚回，已拒绝覆盖版本 {batch.result_stamp} 的新结论')
    record(db, actor, 'ledger.batch.recompute', {'id': batch.id, 'version': version,
                                                 'rows': len(batch.rows), 'conflicts': batch.conflict_count})
    return batch


def batch_stale(db, batch: LedgerBatch) -> bool:
    return batch.status != 'published' and current_version(db) > batch.version


# ---------------------------------------------------------------- adjudication

def decide_adjudication(db, adjudication_id: int, decision, note, actor: str) -> Adjudication:
    adjudication = db.get(Adjudication, adjudication_id)
    if not adjudication:
        raise HTTPException(404, '裁定事项不存在')
    if adjudication.kind == 'conclusion':
        if not isinstance(decision, bool):
            raise HTTPException(422, '结论裁定必须选择 合格 或 不合格')
        normalized = decision
    else:
        if not isinstance(decision, str) or decision not in (adjudication.options or []):
            raise HTTPException(422, '区域裁定必须从冲突区域中选择一个')
        normalized = decision
    version = bump_version(db)
    adjudication.decision = normalized
    adjudication.status = 'resolved'
    adjudication.decided_entry_ids = list(adjudication.entry_ids or [])
    adjudication.reopened = False
    adjudication.resolution_note = str(note or '').strip()[:500]
    adjudication.decided_by = actor
    adjudication.decided_at = _now()
    adjudication.decided_version = version
    db.flush()
    record(db, actor, 'ledger.adjudication.resolve', {'id': adjudication.id, 'kind': adjudication.kind,
                                                      'decision': normalized, 'version': version})
    return adjudication


# ---------------------------------------------------------------- identity correction

def _datasets_for_batches(db, batches) -> list[Dataset]:
    datasets = []
    for batch in batches:
        dataset = db.scalar(select(Dataset).where(Dataset.ledger_batch_id == batch.id))
        if dataset:
            datasets.append(dataset)
    return datasets


def _flags(db, dataset_ids) -> dict[int, LedgerDatasetFlag]:
    if not dataset_ids:
        return {}
    return {flag.dataset_id: flag for flag in db.scalars(select(LedgerDatasetFlag).where(LedgerDatasetFlag.dataset_id.in_(dataset_ids)))}


def invalidate_derived(db, observation_id: int, reason: str, actor: str, version: int) -> dict:
    """Apply identity-correction fallout: unapproved analyses only; approved stays put."""
    affected_batches = [
        batch for batch in db.scalars(select(LedgerBatch).where(LedgerBatch.status == 'published'))
        if observation_id in (batch.snapshot or {}).get('observation_ids', [])
    ]
    for batch in affected_batches:
        batch.correction_count = int(batch.correction_count or 0) + 1
        batch.last_corrected_at = _now()
    invalidated_datasets = []
    for dataset in _datasets_for_batches(db, affected_batches):
        flag = db.get(LedgerDatasetFlag, dataset.id)
        if flag is None:
            flag = LedgerDatasetFlag(dataset_id=dataset.id, batch_id=dataset.ledger_batch_id,
                                     version=version, invalidated=True, reason=reason, invalidated_at=_now())
            db.add(flag)
        else:
            flag.invalidated = True
            flag.reason = reason
            flag.invalidated_at = _now()
            flag.version = version
        invalidated_datasets.append(dataset.id)
    invalidated_jobs, kept_approved = [], []
    if invalidated_datasets:
        for job in db.scalars(select(Job).where(Job.dataset_id.in_(invalidated_datasets))):
            if job.approved:
                kept_approved.append(job.id)  # approved material keeps its original values
                continue
            if job.invalidated:
                continue
            job.invalidated = True
            job.invalid_reason = reason
            job.invalidated_at = _now()
            if job.status in ('queued', 'running'):
                job.status = 'invalid'
            invalidated_jobs.append(job.id)
    return {'datasets': sorted(set(invalidated_datasets)), 'jobs': invalidated_jobs,
            'kept_approved_jobs': kept_approved, 'batches': [batch.id for batch in affected_batches]}


def correct_identity(db, observation_id: int, new_token, new_name, reason, actor: str) -> dict:
    observation = db.get(Observation, observation_id)
    if not observation:
        raise HTTPException(404, '观察记录不存在')
    token = _normalize_token(new_token) or observation.token
    name = _normalize_name(new_name) or observation.point_name
    if token == observation.token and name == observation.point_name:
        raise HTTPException(422, '更正后的编号与名称均未变化')
    version = bump_version(db)
    correction = IdentityCorrection(
        version=version, observation_id=observation.id, old_token=observation.token,
        new_token=token, old_name=observation.point_name, new_name=name,
        reason=str(reason or '').strip()[:500], created_by=actor, created_at=_now())
    db.add(correction)
    db.flush()
    refresh_adjudications(db, actor)
    fallout = invalidate_derived(db, observation.id, f'身份更正：观察 #{observation.id} 编号改为 {token}', actor, version)
    record(db, actor, 'ledger.identity.correct', {
        'observation': observation.id, 'old_token': observation.token, 'new_token': token,
        'old_name': observation.point_name, 'new_name': name, 'version': version, **fallout})
    return {'correction_id': correction.id, 'version': version, 'observation_id': observation.id,
            'old_token': observation.token, 'new_token': token, **fallout}


# ---------------------------------------------------------------- publication

def _publish_key(batch: LedgerBatch, key) -> str:
    key = str(key or '').strip()
    return key[:160] if key else f'ledger-batch-{batch.id}-v{batch.version}'


def _safe_start_commit(db) -> bool:
    """Commit the durable attempt, tolerating a concurrent writer that won the unique key.

    A lost race must not produce a second attempt row; the other request already owns it.
    """
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _validate_publish(db, batch: LedgerBatch):
    if batch.status == 'published':
        return
    if batch.stale or current_version(db) > batch.version:
        raise HTTPException(409, f'台账已推进到版本 {current_version(db)}，批次仍固定在版本 {batch.version}；请先重算批次')
    unresolved = [conflict for conflict in (batch.conflicts or []) if not conflict.get('resolved')]
    if unresolved:
        ids = '、'.join(str(conflict.get('adjudication_id')) for conflict in unresolved if conflict.get('adjudication_id'))
        raise HTTPException(409, f'仍有 {len(unresolved)} 项结论或区域冲突待裁定，未清不发布（裁定事项 {ids or "待建"}）')
    if not batch.rows:
        raise HTTPException(409, '该批次没有可用日表行（全部记录仍在冲突中），待裁定清除后才能发布')


def publish_batch(db, batch_id: int, idempotency_key, actor: str, interrupt_after_attempt: bool = False) -> dict:
    """Idempotent publish. Retry resumes the same attempt and never duplicates the dataset."""
    batch = db.get(LedgerBatch, batch_id, with_for_update=True)
    if not batch:
        raise HTTPException(404, '日表批次不存在')
    _validate_publish(db, batch)
    if batch.status == 'published' and batch.published_dataset_id:
        dataset = db.get(Dataset, batch.published_dataset_id)
        return {'batch_id': batch.id, 'dataset_id': batch.published_dataset_id,
                'resumed': True, 'published': True,
                'dataset': {'id': dataset.id, 'name': dataset.name} if dataset else None}
    key = _publish_key(batch, idempotency_key)
    attempt = db.scalar(select(PublishAttempt).where(PublishAttempt.idempotency_key == key))
    if attempt and attempt.status == 'completed' and attempt.dataset_id:
        batch.status = 'published'
        batch.published_dataset_id = attempt.dataset_id
        batch.published_at = attempt.finished_at or _now()
        db.commit()
        dataset = db.get(Dataset, attempt.dataset_id)
        return {'batch_id': batch.id, 'dataset_id': attempt.dataset_id, 'resumed': True,
                'published': True, 'dataset': {'id': dataset.id, 'name': dataset.name} if dataset else None}
    if attempt is None:
        attempt = PublishAttempt(batch_id=batch.id, idempotency_key=key, attempt_no=1,
                                 status='started', started_at=_now())
        db.add(attempt)
    else:
        attempt.attempt_no = int(attempt.attempt_no) + 1
        attempt.status = 'started'
        attempt.error = ''
        attempt.started_at = _now()
    if not _safe_start_commit(db):
        raise HTTPException(409, '同一次发布正在被另一请求处理，请稍后用相同幂等键重试')
    # Re-load after the commit (expire_on_commit is off, but keep the flow explicit).
    batch = db.get(LedgerBatch, batch_id)
    attempt = db.scalar(select(PublishAttempt).where(PublishAttempt.idempotency_key == key))
    try:
        if interrupt_after_attempt and os.getenv('LEDGER_FAULT_INJECTION', 'false').lower() == 'true':
            # Test-only hook: prove that a crash after the durable attempt is resumable.
            raise RuntimeError('simulated interruption after durable attempt')
        name = f'台账日表 · {batch.name}'
        dataset = db.scalar(select(Dataset).where(Dataset.ledger_batch_id == batch.id))
        if dataset is None:
            dataset = db.scalar(select(Dataset).where(Dataset.name == name))
            if dataset is not None:
                raise HTTPException(409, f'日表名称「{name}」已被其他批次占用')
            records = [{'day': row['day'], 'team': row['team'], 'zone': row['zone'],
                        'observed': row['observed'], 'passed': row['passed'],
                        'frame_size': row['frame_size']} for row in batch.rows]
            dataset = Dataset(name=name, source=f'ledger-batch-{batch.id}', records=records,
                              checksum=checksum(records), created_at=_now(),
                              ledger_batch_id=batch.id, ledger_version=batch.version)
            db.add(dataset)
            db.flush()
        attempt.dataset_id = dataset.id
        attempt.status = 'completed'
        attempt.finished_at = _now()
        batch.status = 'published'
        batch.published_dataset_id = dataset.id
        batch.published_at = _now()
        record(db, actor, 'ledger.batch.publish', {'id': batch.id, 'dataset': dataset.id,
                                                   'version': batch.version, 'rows': len(batch.rows),
                                                   'attempt': attempt.attempt_no})
        db.commit()
        return {'batch_id': batch.id, 'dataset_id': dataset.id, 'resumed': attempt.attempt_no > 1,
                'published': True, 'dataset': {'id': dataset.id, 'name': dataset.name},
                'rows': len(batch.rows)}
    except Exception as error:
        db.rollback()
        attempt = db.scalar(select(PublishAttempt).where(PublishAttempt.idempotency_key == key))
        if attempt:
            attempt.status = 'failed'
            attempt.error = str(error)[:990]
            attempt.finished_at = _now()
            record(db, actor, 'ledger.batch.publish_failed', {'id': batch_id, 'key': key, 'error': attempt.error})
            db.commit()
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(500, f'发布中断，已保留原次处理；用同一幂等键重试可续作：{error}') from error


# ---------------------------------------------------------------- legacy day-table import

def _record_value(record, *keys, default=''):
    for key in keys:
        if key in record and record[key] not in (None, ''):
            return record[key]
    return default


def import_daily_table(db, dataset, actor: str) -> dict:
    """Expand an imported daily table into per-point observations, idempotently.

    Each daily row carries a point count and a passed count; expansion writes that many
    per-point observations so the ledger total reconciles with the legacy analysis.
    Rows that carry point_id/point_token/point_name keep a real identity usable for merges.
    """
    if dataset.ledger_batch_id is not None:
        raise HTTPException(409, '该数据批次由台账发布产生，不能反向导入台账')
    marker = f'dataset:{dataset.id}'
    if db.scalar(select(Observation).where(Observation.source == marker).limit(1)):
        raise HTTPException(409, '该数据批次已导入台账，重复导入会重复计样')
    pending, frames, skipped = [], set(), []
    for index, record in enumerate(dataset.records):
        try:
            observed = max(0, int(float(_record_value(record, 'observed', default=0))))
            passed = max(0, min(observed, int(float(_record_value(record, 'passed', default=0)))))
            day = _parse_day(_record_value(record, 'day'))
            zone = _normalize_name(_record_value(record, 'zone'))
            team = str(_record_value(record, 'team')).strip()
            if team not in TEAM_VALUES or not zone:
                raise ValueError('队伍或区域不合法')
        except (HTTPException, ValueError) as error:
            skipped.append({'row': index + 1, 'reason': str(getattr(error, 'detail', error))})
            continue
        frame = record.get('frame_size')
        if frame not in (None, '') and zone not in frames:
            try:
                set_frame(db, zone, int(float(frame)), actor)
                frames.add(zone)
            except (HTTPException, TypeError, ValueError):
                pass
        explicit = str(_record_value(record, 'point_id', 'point_token', 'point_name', default=''))
        if explicit and observed == 1:
            token, name = explicit, explicit
        else:
            # A summed daily row has no single identity; keep one distinct token per point
            # so nothing collapses into a duplicate and each stays individually addressable.
            token = f'import:{dataset.id}:{index}:0' if observed == 1 and not explicit else ''
            name = explicit or f'{zone}导入点{index + 1}'
        for offset in range(observed):
            pending.append(ObservationInput(
                day=day, team=team, zone=zone,
                point_name=name if observed == 1 else f'{name}-{offset + 1}',
                token=token if observed == 1 else f'import:{dataset.id}:{index}:{offset}',
                passed=offset < passed, note='由日表导入展开', source=marker))
    created = create_observations_bulk(db, pending, actor, source=marker) if pending else []
    return {'dataset': dataset.id, 'observations': len(created), 'submitted': len(pending),
            'skipped': skipped}


# ---------------------------------------------------------------- read models

def observation_dict(observation: Observation) -> dict:
    return {'id': observation.id, 'version': observation.version, 'token': observation.token,
            'point_name': observation.point_name, 'day': observation.day, 'team': observation.team,
            'zone': observation.zone, 'passed': observation.passed, 'note': observation.note,
            'source': observation.source, 'created_by': observation.created_by,
            'created_at': observation.created_at}


def merge_dict(merge: PointMerge) -> dict:
    return {'id': merge.id, 'version': merge.version, 'source_token': merge.source_token,
            'target_token': merge.target_token, 'reason': merge.reason, 'active': merge.active,
            'created_by': merge.created_by, 'created_at': merge.created_at,
            'revoked_by': merge.revoked_by, 'revoked_reason': merge.revoked_reason,
            'revoked_at': merge.revoked_at, 'revoked_version': merge.revoked_version}


def adjudication_dict(adjudication: Adjudication) -> dict:
    return {'id': adjudication.id, 'signature': adjudication.signature, 'kind': adjudication.kind,
            'day': adjudication.day, 'team': adjudication.team, 'cluster': adjudication.cluster,
            'entry_ids': adjudication.entry_ids, 'options': adjudication.options,
            'status': adjudication.status, 'decision': adjudication.decision,
            'resolution_note': adjudication.resolution_note, 'decided_by': adjudication.decided_by,
            'decided_at': adjudication.decided_at, 'decided_version': adjudication.decided_version,
            'decided_entry_ids': adjudication.decided_entry_ids or [],
            'reopened': bool(adjudication.reopened), 'created_at': adjudication.created_at}


def batch_dict(db, batch: LedgerBatch) -> dict:
    return {'id': batch.id, 'name': batch.name, 'version': batch.version,
            'status': batch.status, 'row_count': len(batch.rows or []),
            'conflict_count': batch.conflict_count, 'conflicts': batch.conflicts or [],
            'rows': batch.rows or [], 'trace': batch.trace or [], 'warnings': batch.warnings or [],
            'identity_map': batch.identity_map or {}, 'merges': batch.merges or [],
            'adjudication_ids': batch.adjudication_ids or [],
            'snapshot': batch.snapshot or {}, 'checksum': batch.checksum,
            'stale': batch_stale(db, batch), 'current_version': current_version(db),
            'result_stamp': batch.result_stamp, 'computed_at': batch.computed_at,
            'computed_by': batch.computed_by, 'created_by': batch.created_by,
            'created_at': batch.created_at, 'published_at': batch.published_at,
            'published_dataset_id': batch.published_dataset_id,
            'correction_count': batch.correction_count, 'last_corrected_at': batch.last_corrected_at}


def state_dict(db) -> dict:
    return {
        'version': current_version(db),
        'observations': db.scalar(select(func.count()).select_from(Observation)),
        'merges': db.scalar(select(func.count()).select_from(PointMerge)),
        'merges_active': db.scalar(select(func.count()).select_from(PointMerge).where(PointMerge.revoked_version.is_(None))),
        'pending_adjudications': db.scalar(select(func.count()).select_from(Adjudication).where(Adjudication.status == 'pending')),
        'batches': db.scalar(select(func.count()).select_from(LedgerBatch)),
        'frames': {frame.zone: frame.size for frame in db.scalars(select(LedgerFrame).order_by(LedgerFrame.zone))},
    }
