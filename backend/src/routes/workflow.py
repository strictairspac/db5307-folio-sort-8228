from fastapi import APIRouter, Depends, Response
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import download
from src.models.tables import audit, backups, cases, evidence, restores, runs
from src.schemas.workflow import CaseForm, LoginForm, RequeueForm, RestoreForm, ReviewForm
from src.services import backup, domain, workflow
from src.services.security import current_user, login, operator, reviewer

router = APIRouter()


@router.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1')).scalar_one()
    cache.ping()
    return {'ok': True}


@router.post('/login')
def sign_in(form: LoginForm):
    return login(form.username, form.password)


@router.get('/spec')
def spec(_=Depends(current_user)):
    return domain.SPEC


@router.get('/cases')
def list_cases(_=Depends(current_user)):
    return workflow.recent(cases)


@router.post('/cases', status_code=201)
def create_case(form: CaseForm, actor=Depends(operator)):
    return workflow.make_case(form.title, form.payload, actor)


@router.get('/runs')
def list_runs(_=Depends(current_user)):
    return workflow.recent(runs)


@router.post('/cases/{case_id}/runs', status_code=202)
def start_run(case_id: int, actor=Depends(operator)):
    return workflow.enqueue(case_id, actor)


@router.get('/evidence')
def list_evidence(_=Depends(current_user)):
    return workflow.recent(evidence)


@router.get('/evidence/{identity}/download')
def export_evidence(identity: int, _=Depends(current_user)):
    return download(identity)


@router.post('/evidence/{identity}/review')
def review_evidence(identity: int, form: ReviewForm, actor=Depends(reviewer)):
    return workflow.review(identity, form.note, actor)


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)


@router.get('/backups')
def list_backups(_=Depends(current_user)):
    return workflow.recent(backups)


@router.post('/backups', status_code=201)
def create_backup(actor=Depends(operator)):
    return backup.take_backup(actor)


@router.get('/backups/{identity}/download')
def download_backup(identity: int, _=Depends(current_user)):
    payload, filename = backup.read_backup(identity)
    return Response(payload, media_type='application/json', headers={
        'Content-Disposition': f'attachment; filename="{filename}"'})


@router.get('/restores')
def list_restores(_=Depends(current_user)):
    return workflow.recent(restores)


@router.post('/backups/{identity}/restores', status_code=201)
def start_restore(identity: int, form: RestoreForm, actor=Depends(operator)):
    return backup.restore(identity, None, form.target, actor)


@router.post('/backups/snapshot/{snapshot_id}/restores', status_code=201)
def start_restore_by_snapshot(snapshot_id: str, form: RestoreForm, actor=Depends(operator)):
    return backup.restore(None, snapshot_id, form.target, actor)


@router.get('/restores/{identity}')
def inspect_restore(identity: int, _=Depends(current_user)):
    return backup.inspect_restore(identity)


@router.post('/restores/{identity}/requeue')
def requeue_restored(identity: int, form: RequeueForm, actor=Depends(operator)):
    return backup.requeue(identity, form.runs, actor)
