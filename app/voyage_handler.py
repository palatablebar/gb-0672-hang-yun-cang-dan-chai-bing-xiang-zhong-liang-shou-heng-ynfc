"""航次质量对照清单页面与下载入口。

构建在内存完成后才落库；任何构建异常都不覆盖库内旧版，调用方拿到旧版与失败标记。
同一规则版本重跑复用同批号，(run_id,dict_version) 唯一约束保证不新增对照。
"""
from fastapi import APIRouter, Request, Body
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from app import store, voyage
from app.config import META, ROOT
from app.engine import evaluate
from app.errors import BusinessError

router = APIRouter()
templates = Jinja2Templates(directory=str(ROOT / 'app/templates'))

ALLOWED_LEGACY_KEYS = {'node_id', 'code', 'evidence', 'source'}


def _validate_legacy_findings(raw):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise BusinessError('BAD_LEGACY', '历史发现须为数组')
    findings = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get('node_id'), str) \
                or not isinstance(item.get('code'), str) or not item['node_id'] or not item['code']:
            raise BusinessError('BAD_LEGACY', '历史发现须含 node_id 与 code 字符串')
        extra = set(item) - ALLOWED_LEGACY_KEYS
        if extra:
            raise BusinessError('BAD_LEGACY', f'历史发现含未知字段：{sorted(extra)}')
        evidence = item.get('evidence') or {}
        if not isinstance(evidence, dict):
            raise BusinessError('BAD_LEGACY', '历史证据须为对象')
        findings.append({'node_id': item['node_id'], 'code': item['code'],
                         'evidence': evidence, 'source': str(item.get('source', 'legacy'))})
    return findings


def _build_or_fallback(db_path, run_id, legacy_findings, legacy_rule_version):
    state = store.get(db_path, run_id)
    try:
        checklist = voyage.build_checklist(state, legacy_findings, legacy_rule_version)
        return store.save_checklist(db_path, run_id, checklist), None
    except Exception as exc:  # 输出失败：保留库内旧版，不抛 500 覆盖数据
        try:
            previous = store.get_checklist(db_path, run_id)
            return previous, f'清单构建失败，已保留旧版：{type(exc).__name__}: {exc}'
        except BusinessError:
            raise BusinessError('CHECKLIST_BUILD_FAILED',
                                f'清单构建失败且无旧版可保留：{type(exc).__name__}: {exc}', 500)


@router.get('/voyage')
def page(request: Request):
    from app.main import DB_PATH
    run_id = request.query_params.get('run_id')
    if run_id:
        state = store.get(DB_PATH, run_id)
    else:
        state = store.save(DB_PATH, evaluate({'dataset': request.query_params.get('dataset', 'dirty')}))
    try:
        checklist = store.get_checklist(DB_PATH, state['manifest']['run_id'])
    except BusinessError:
        checklist = None
    return templates.TemplateResponse(request=request, name='voyage.html',
                                      context={'meta': META, 'key': 'voyage',
                                               'state': state, 'checklist': checklist})


@router.post('/api/runs/{run_id}/checklist')
def build(run_id: str, payload: dict = Body(default={})):
    from app.main import DB_PATH
    legacy = _validate_legacy_findings(payload.get('legacy_findings'))
    legacy_rule_version = payload.get('legacy_rule_version')
    if legacy_rule_version is not None and not isinstance(legacy_rule_version, str):
        raise BusinessError('BAD_LEGACY', 'legacy_rule_version 须为字符串')
    checklist, warning = _build_or_fallback(DB_PATH, run_id, legacy, legacy_rule_version)
    if warning:
        return {'checklist': checklist, 'warning': warning}
    return {'checklist': checklist}


@router.get('/api/runs/{run_id}/checklist')
def detail(run_id: str):
    from app.main import DB_PATH
    return store.get_checklist(DB_PATH, run_id)


@router.get('/api/runs/{run_id}/checklist/download')
def download(run_id: str):
    from app.main import DB_PATH
    checklist = store.get_checklist(DB_PATH, run_id)
    csv_text = voyage.to_csv(checklist)
    filename = f'voyage-checklist-{run_id}.csv'
    return Response(csv_text.encode('utf-8'),
                    media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@router.get('/api/voyage/compare')
def compare(request: Request):
    from app.main import DB_PATH
    raw = request.query_params.get('run_ids', '')
    run_ids = [r.strip() for r in raw.split(',') if r.strip()]
    if not run_ids:
        raise BusinessError('BAD_RUN_IDS', 'run_ids 至少包含一个批号')
    checklists = store.checklists_for_runs(DB_PATH, run_ids)
    found = {c['summary']['run_id'] for c in checklists}
    missing = [r for r in run_ids if r not in found]
    comparison = voyage.compare_voyages(checklists)
    return {'comparison': comparison,
            'requested': run_ids, 'missing': missing,
            'issue_keys': len(comparison)}


@router.get('/api/voyage/compare/download')
def compare_download(request: Request):
    from app.main import DB_PATH
    raw = request.query_params.get('run_ids', '')
    run_ids = [r.strip() for r in raw.split(',') if r.strip()]
    if not run_ids:
        raise BusinessError('BAD_RUN_IDS', 'run_ids 至少包含一个批号')
    checklists = store.checklists_for_runs(DB_PATH, run_ids)
    csv_text = voyage.comparison_to_csv(voyage.compare_voyages(checklists))
    return Response(csv_text.encode('utf-8'),
                    media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':
                             'attachment; filename="voyage-comparison.csv"'})
