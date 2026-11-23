"""Consistent backup capture and transactional restore drills.

A backup is captured inside one read-only snapshot transaction, so login
batches, runs, shelving order lists and review signatures all come from the
same point in time even while the source keeps accepting submissions and
signatures.  A restore validates the whole bundle first and then writes the
target in a single transaction, so a rejected drill leaves the empty target
database untouched.

Runs that were queued or running at capture time are restored behind a hold:
the worker treats them as not ready until someone confirms the re-enqueue, and
confirming twice never queues the same run twice.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from src.config.settings import ARTIFACTS, DATABASE_URL, engine
from src.constants.workflow import Status
from src.models.tables import audit, backups, cases, evidence, restores, run_holds, runs, schema
from src.services.workflow import found, log
from src.utils.encoding import canonical
from src.utils.time import utc_time

FORMAT = 'folio-backup/1'
BACKUP_DIR = Path(ARTIFACTS) / 'backups'
RESTORE_DIR = Path(ARTIFACTS) / 'restores'
BUSINESS_TABLES = (('cases', cases), ('runs', runs), ('evidence', evidence), ('audit', audit))
HELD_STATUSES = (Status.QUEUED, Status.RUNNING)
VALID_STATUSES = {status.value for status in Status}
IDENTIFIER = re.compile(r'[a-z][a-z0-9_]{0,62}')
SNAPSHOT = re.compile(r'[0-9a-f]{32}')


def _checksum(document):
    body = {key: value for key, value in document.items() if key != 'checksum'}
    return hashlib.sha256(canonical(body)).hexdigest()


def _write_atomically(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.pending')
    pending.write_bytes(payload)
    pending.replace(path)


def _capture():
    """Read every business table from one read-only snapshot transaction."""
    connection = engine.connect()
    try:
        if connection.dialect.name in ('postgresql', 'mysql'):
            connection = connection.execution_options(isolation_level='REPEATABLE READ')
        with connection.begin():
            if connection.dialect.name == 'postgresql':
                connection.exec_driver_sql('SET TRANSACTION READ ONLY')
            # Open the snapshot before any table read: concurrent submissions
            # then land wholly inside or wholly outside the captured instant.
            connection.exec_driver_sql('SELECT 1')
            return {
                name: [dict(row) for row in connection.execute(
                    select(table).order_by(table.c.id)).mappings()]
                for name, table in BUSINESS_TABLES
            }
    finally:
        connection.close()


def take_backup(actor):
    tables = _capture()
    document = {
        'format': FORMAT,
        'snapshot_id': secrets.token_hex(16),
        'taken_at': utc_time(),
        'counts': {name: len(rows) for name, rows in tables.items()},
        'tables': tables,
    }
    document['checksum'] = _checksum(document)
    payload = canonical(document)
    filename = f"backup-{document['snapshot_id']}.json"
    _write_atomically(BACKUP_DIR / filename, payload)
    with engine.begin() as connection:
        identity = connection.execute(backups.insert().values(
            snapshot_id=document['snapshot_id'], actor=actor, created_at=utc_time(),
            checksum=document['checksum'], counts=document['counts'], bytes=len(payload),
            filename=filename)).inserted_primary_key[0]
        log(connection, actor, 'backup', document['snapshot_id'])
        return found(connection, backups, identity)


def _backup_row(identity=None, snapshot_id=None):
    with engine.connect() as connection:
        query = select(backups)
        if identity is not None:
            query = query.where(backups.c.id == identity)
        else:
            query = query.where(backups.c.snapshot_id == snapshot_id)
        row = connection.execute(query).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该备份')
    return dict(row)


def _backup_path(row):
    path = (BACKUP_DIR / row['filename']).resolve()
    if path.parent != BACKUP_DIR.resolve():
        raise HTTPException(404, '备份文件不存在')
    return path


def read_backup(identity):
    row = _backup_row(identity=identity)
    path = _backup_path(row)
    if not path.is_file():
        raise HTTPException(404, '备份文件不存在')
    return path.read_bytes(), row['filename']


def _reject(message):
    raise HTTPException(422, message)


def _load_bundle(row):
    path = _backup_path(row)
    if not path.is_file():
        _reject('备份文件缺失，已拒绝整份恢复')
    try:
        document = json.loads(path.read_bytes())
    except (OSError, ValueError):
        _reject('备份文件无法读取，已拒绝整份恢复')
    if not isinstance(document, dict):
        _reject('备份格式无效，已拒绝整份恢复')
    if document.get('checksum') != _checksum(document) or document.get('checksum') != row['checksum']:
        _reject('备份校验码不匹配，已拒绝整份恢复')
    return document


def _unique_ids(rows, label):
    seen = set()
    for row in rows:
        identity = row.get('id') if isinstance(row, dict) else None
        if type(identity) is not int or identity < 1:
            _reject(f'备份中的{label}编号无效，已拒绝整份恢复')
        if identity in seen:
            _reject(f'备份中的{label}编号重复，已拒绝整份恢复')
        seen.add(identity)
    return seen


def _validate(document):
    if document.get('format') != FORMAT:
        _reject('备份格式不受支持，已拒绝整份恢复')
    if not isinstance(document.get('snapshot_id'), str) or not SNAPSHOT.fullmatch(document['snapshot_id']):
        _reject('备份缺少快照信息，已拒绝整份恢复')
    if not isinstance(document.get('taken_at'), str):
        _reject('备份缺少捕获时点，已拒绝整份恢复')
    if 'checksum' not in document:
        _reject('备份缺少校验码，已拒绝整份恢复')
    tables, counts = document.get('tables'), document.get('counts')
    if not isinstance(tables, dict) or not isinstance(counts, dict):
        _reject('备份缺少业务数据，已拒绝整份恢复')
    for name, _table in BUSINESS_TABLES:
        if not isinstance(tables.get(name), list):
            _reject(f'备份缺少 {name} 数据，已拒绝整份恢复')
        if counts.get(name) != len(tables[name]):
            _reject('备份计数与内容不一致，已拒绝整份恢复')

    case_ids = _unique_ids(tables['cases'], '登录批次')
    run_ids = _unique_ids(tables['runs'], '执行')
    _unique_ids(tables['evidence'], '上架顺序单')
    _unique_ids(tables['audit'], '操作记录')
    for row in tables['cases']:
        if not isinstance(row.get('payload'), dict) or not row.get('slug') or not row.get('title'):
            _reject('备份中的登录批次不完整，已拒绝整份恢复')
    for row in tables['runs']:
        if row.get('case_id') not in case_ids:
            _reject('备份缺少执行引用的登录批次，已拒绝整份恢复')
        if row.get('status') not in VALID_STATUSES:
            _reject('备份中的执行状态无效，已拒绝整份恢复')
    for row in tables['audit']:
        if not row.get('actor') or not row.get('action') or row.get('reference') is None:
            _reject('备份中的操作记录不完整，已拒绝整份恢复')
    signatures = 0
    for row in tables['evidence']:
        if row.get('run_id') not in run_ids:
            _reject('备份缺少上架顺序单引用的执行，已拒绝整份恢复')
        if not isinstance(row.get('report'), dict) or not isinstance(row.get('digest'), str):
            _reject('备份中的上架顺序单不完整，已拒绝整份恢复')
        if hashlib.sha256(canonical(row['report'])).hexdigest() != row['digest']:
            _reject('备份中的上架结果校验码不匹配，已拒绝整份恢复')
        if row.get('reviewed_at'):
            if not row.get('reviewer') or not row.get('review_note'):
                _reject('备份中的复核签署不完整，已拒绝整份恢复')
            signatures += 1
    # Reject any reference the bundle would need but does not carry.
    for row in tables['runs']:
        if not isinstance(row.get('owner'), str) or not row['owner']:
            _reject('备份中的执行缺少经办人，已拒绝整份恢复')
    return tables, {'references': len(tables['runs']) + len(tables['evidence']),
                    'evidence': len(tables['evidence']), 'signatures': signatures}


def _backend():
    return make_url(DATABASE_URL).get_backend_name()


def _target_url(name):
    url = make_url(DATABASE_URL)
    if url.get_backend_name() == 'sqlite':
        return url.set(database=str(RESTORE_DIR / f'{name}.sqlite'))
    return url.set(database=name)


def _engine_for_target(name):
    return create_engine(_target_url(name), pool_pre_ping=True, poolclass=NullPool)


def _source_database():
    url = make_url(DATABASE_URL)
    return Path(str(url.database)).stem if url.get_backend_name() == 'sqlite' else str(url.database)


def _target_exists(name):
    backend = _backend()
    if backend == 'sqlite':
        return (RESTORE_DIR / f'{name}.sqlite').exists()
    with engine.execution_options(isolation_level='AUTOCOMMIT').connect() as connection:
        if backend == 'postgresql':
            return connection.execute(text('SELECT 1 FROM pg_database WHERE datname = :name'),
                                      {'name': name}).first() is not None
        return connection.execute(text('SELECT SCHEMA_NAME FROM information_schema.SCHEMATA '
                                       'WHERE SCHEMA_NAME = :name'), {'name': name}).first() is not None


def _create_target(name):
    backend = _backend()
    if backend == 'sqlite':
        RESTORE_DIR.mkdir(parents=True, exist_ok=True)
        return
    with engine.execution_options(isolation_level='AUTOCOMMIT').connect() as connection:
        quoted = connection.dialect.identifier_preparer.quote(name)
        if backend == 'postgresql':
            connection.exec_driver_sql(f'CREATE DATABASE {quoted} TEMPLATE template0')
        else:
            connection.exec_driver_sql(f'CREATE DATABASE {quoted} CHARACTER SET utf8mb4')


def _choose_target(requested):
    source = _source_database()
    if requested is not None:
        name = requested.strip()
        if not IDENTIFIER.fullmatch(name):
            _reject('演练目标库名只能包含小写字母、数字和下划线，且以字母开头')
        if name == source:
            raise HTTPException(409, '演练目标不能是源库')
        return name
    for _ in range(5):
        name = f'workflow_drill_{secrets.token_hex(8)}'
        if not _target_exists(name):
            return name
    raise HTTPException(409, '无法分配演练目标库')


def _prepare_target(name):
    """Return True when this call created the target, False for a pre-existing one."""
    if not _target_exists(name):
        try:
            _create_target(name)
        except Exception as exc:
            raise HTTPException(503, f'无法建立演练目标库：{exc}') from None
        return True
    target = _engine_for_target(name)
    try:
        existing = inspect(target).get_table_names()
    finally:
        target.dispose()
    # A drill target must be an empty library; anything already in it is left alone.
    if existing:
        raise HTTPException(409, '目标库不是空库，请使用空库演练')
    return False


def _discard_target(name, created):
    """Best-effort cleanup so a failed restore leaves no half-written target.

    PostgreSQL rolls DDL back with the write transaction; this covers engines
    with non-transactional DDL and removes a target this call created.
    """
    backend = _backend()
    try:
        if backend == 'sqlite':
            path = RESTORE_DIR / f'{name}.sqlite'
            if created:
                path.unlink(missing_ok=True)
            return
        with engine.execution_options(isolation_level='AUTOCOMMIT').connect() as connection:
            quoted = connection.dialect.identifier_preparer.quote(name)
            if created:
                connection.exec_driver_sql(f'DROP DATABASE IF EXISTS {quoted}')
                return
            if backend != 'mysql':
                return
            target = _engine_for_target(name)
            try:
                with target.begin() as connection:
                    for table in reversed(schema.sorted_tables):
                        table.drop(connection, checkfirst=True)
            finally:
                target.dispose()
    except Exception:
        pass


def _insert(connection, table, rows):
    for row in rows:
        connection.execute(table.insert().values(
            {column.name: row.get(column.name) for column in table.c}))


def _restore_sequences(connection):
    if connection.dialect.name != 'postgresql':
        return
    for _name, table in BUSINESS_TABLES:
        sequence = connection.execute(
            text("SELECT pg_get_serial_sequence(:table, 'id')"), {'table': table.name}).scalar()
        if not sequence:
            continue
        highest = connection.execute(select(func.max(table.c.id))).scalar()
        if highest is None:
            connection.execute(text('SELECT setval(:sequence, 1, false)'), {'sequence': sequence})
        else:
            connection.execute(text('SELECT setval(:sequence, :value, true)'),
                               {'sequence': sequence, 'value': int(highest)})


def _verify(connection, counts):
    for name, table in BUSINESS_TABLES:
        observed = connection.execute(select(func.count()).select_from(table)).scalar_one()
        if observed != counts[name]:
            raise RuntimeError(f'{name} 数量核对不一致')
    orphan_runs = connection.execute(
        select(func.count()).select_from(runs)
        .where(~runs.c.case_id.in_(select(cases.c.id)))).scalar_one()
    orphan_evidence = connection.execute(
        select(func.count()).select_from(evidence)
        .where(~evidence.c.run_id.in_(select(runs.c.id)))).scalar_one()
    if orphan_runs or orphan_evidence:
        raise RuntimeError('恢复后引用核对不一致')
    for row in connection.execute(select(evidence)).mappings():
        if hashlib.sha256(canonical(row['report'])).hexdigest() != row['digest']:
            raise RuntimeError('恢复后上架结果校验码不一致')


def _holds(tables):
    return [{'run_id': row['id'], 'captured_status': row['status']}
            for row in tables['runs'] if row['status'] in HELD_STATUSES]


def _record_failure(backup_row, target, actor, message, report):
    try:
        with engine.begin() as connection:
            connection.execute(restores.insert().values(
                backup_id=backup_row['id'], token=secrets.token_hex(16), target=(target or '')[:63],
                actor=actor, created_at=utc_time(), status='failed',
                checksum=backup_row['checksum'],
                report={**report, 'error': message[:500]}, error=message[:500]))
            log(connection, actor, 'restore_failed', backup_row['snapshot_id'])
    except Exception:
        pass


def restore(identity, snapshot_id, requested_target, actor):
    backup_row = _backup_row(identity=identity, snapshot_id=snapshot_id)
    target = ''
    created = False
    try:
        document = _load_bundle(backup_row)
        tables, verified = _validate(document)
        target = _choose_target(requested_target)
        created = _prepare_target(target)

        holds = _holds(tables)
        token = secrets.token_hex(16)
        target_engine = _engine_for_target(target)
        try:
            with target_engine.begin() as connection:
                # The drill target gets the full working schema, so replayed runs
                # can be claimed and finished exactly like on the source.
                schema.create_all(connection)
                for name, table in BUSINESS_TABLES:
                    _insert(connection, table, tables[name])
                for hold in holds:
                    connection.execute(run_holds.insert().values(
                        run_id=hold['run_id'], restore_token=token,
                        captured_status=hold['captured_status'], created_at=utc_time()))
                _restore_sequences(connection)
                _verify(connection, backup_row['counts'])
        finally:
            target_engine.dispose()

        report = {
            'snapshot_id': document['snapshot_id'],
            'taken_at': document['taken_at'],
            'target': target,
            'counts': {name: len(tables[name]) for name, _table in BUSINESS_TABLES},
            'checksum': backup_row['checksum'],
            'verified': verified,
            'pending_requeue': holds,
            'summary': (f"已从 {document['taken_at']} 的备份恢复到演练库 {target}；"
                        f"核对 {verified['references']} 处引用、{verified['evidence']} 份上架结果，"
                        f"其中 {verified['signatures']} 份带复核签署；"
                        f"{len(holds)} 个排队或执行中作业待确认重新入队。"),
        }
        with engine.begin() as connection:
            restore_id = connection.execute(restores.insert().values(
                backup_id=backup_row['id'], token=token, target=target, actor=actor,
                created_at=utc_time(), status='restored', checksum=backup_row['checksum'],
                report=report, error=None)).inserted_primary_key[0]
            log(connection, actor, 'restore', backup_row['snapshot_id'])
            return found(connection, restores, restore_id)
    except HTTPException as exc:
        _discard_target(target, created)
        _record_failure(backup_row, target, actor, str(exc.detail),
                        {'snapshot_id': backup_row['snapshot_id']})
        raise
    except Exception:
        _discard_target(target, created)
        _record_failure(backup_row, target, actor, '恢复演练失败，目标库未写入业务数据',
                        {'snapshot_id': backup_row['snapshot_id']})
        raise HTTPException(500, '恢复演练失败，目标库未写入业务数据') from None


def _restore_row(identity):
    with engine.connect() as connection:
        return found(connection, restores, identity)


def inspect_restore(identity):
    row = _restore_row(identity)
    if row['status'] != 'restored':
        raise HTTPException(409, '该次恢复未成功，无法查验')
    if not _target_exists(row['target']):
        raise HTTPException(404, '演练目标库不存在')
    target = _engine_for_target(row['target'])
    try:
        with target.connect() as connection:
            payload = {
                'restore': row,
                'target': row['target'],
                'cases': [dict(item) for item in connection.execute(
                    select(cases).order_by(cases.c.id)).mappings()],
                'runs': [dict(item) for item in connection.execute(
                    select(runs).order_by(runs.c.id)).mappings()],
                'evidence': [dict(item) for item in connection.execute(
                    select(evidence).order_by(evidence.c.id)).mappings()],
                'audit': [dict(item) for item in connection.execute(
                    select(audit).order_by(audit.c.id)).mappings()],
            }
            holds = {item['run_id']: item for item in connection.execute(select(run_holds)).mappings()}
    finally:
        target.dispose()
    for item in payload['runs']:
        hold = holds.get(item['id'])
        item['requeue'] = None if hold is None else ('confirmed' if hold['confirmed_at'] else 'pending')
    return payload


def requeue(identity, run_ids, actor):
    row = _restore_row(identity)
    if row['status'] != 'restored':
        raise HTTPException(409, '该次恢复未成功，无法重新入队')
    target = _engine_for_target(row['target'])
    confirmed, already = [], []
    try:
        with target.begin() as connection:
            holds = {item['run_id']: item for item in connection.execute(
                select(run_holds).where(run_holds.c.restore_token == row['token'])).mappings()}
            requested = sorted(holds) if run_ids is None else list(dict.fromkeys(run_ids))
            unknown = [run_id for run_id in requested if run_id not in holds]
            # Reject the whole request before touching any hold, so a partly
            # invalid confirmation never queues some runs and fails the rest.
            if unknown:
                raise HTTPException(422, f'以下作业不属于本次恢复：{sorted(unknown)}')
            for run_id in requested:
                if holds[run_id]['confirmed_at']:
                    already.append(run_id)
                    continue
                updated = connection.execute(
                    run_holds.update().where(run_holds.c.run_id == run_id,
                                             run_holds.c.confirmed_at.is_(None))
                    .values(confirmed_at=utc_time(), confirmed_by=actor)).rowcount
                if not updated:
                    already.append(run_id)
                    continue
                # Re-enqueue exactly once; confirmation is guarded above.
                connection.execute(runs.update().where(runs.c.id == run_id).values(
                    status=Status.QUEUED, started_at=None, attempt_token=None,
                    finished_at=None, error=None))
                confirmed.append(run_id)
            remaining = [item['run_id'] for item in connection.execute(
                select(run_holds).where(run_holds.c.restore_token == row['token'],
                                        run_holds.c.confirmed_at.is_(None))).mappings()]
    finally:
        target.dispose()
    return {'id': row['id'], 'confirmed': sorted(confirmed), 'already': sorted(already),
            'pending': sorted(remaining)}
