from sqlalchemy import (JSON, Column, ForeignKey, Integer, MetaData, String,
                        Table, Text)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))
backups = Table('backup_artifacts', schema,
    Column('id', Integer, primary_key=True),
    Column('snapshot_id', String(32), nullable=False, unique=True),
    Column('actor', String(40), nullable=False),
    Column('created_at', String(32), nullable=False),
    Column('checksum', String(64), nullable=False),
    Column('counts', JSON, nullable=False),
    Column('bytes', Integer, nullable=False),
    Column('filename', String(120), nullable=False))
restores = Table('restore_drills', schema,
    Column('id', Integer, primary_key=True),
    Column('backup_id', ForeignKey('backup_artifacts.id'), nullable=False, index=True),
    Column('token', String(32), nullable=False, unique=True),
    Column('target', String(63), nullable=False),
    Column('actor', String(40), nullable=False),
    Column('created_at', String(32), nullable=False),
    Column('status', String(20), nullable=False),
    Column('checksum', String(64), nullable=False),
    Column('report', JSON, nullable=False),
    Column('error', Text))
# Restored runs keep their captured status but are held out of the queue until a
# re-enqueue confirmation arrives, so concurrent workers cannot pick them up twice.
run_holds = Table('run_holds', schema,
    Column('run_id', ForeignKey('business_runs.id'), primary_key=True),
    Column('restore_token', String(32), nullable=False, index=True),
    Column('captured_status', String(20), nullable=False),
    Column('created_at', String(32), nullable=False),
    Column('confirmed_at', String(32)),
    Column('confirmed_by', String(40)))
