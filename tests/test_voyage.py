"""航次质量对照清单测试。

覆盖：
* 旧码一对一改写 / 一对多按证据拆分 / 无映射保留原码，不丢记录不静默改名
* 新旧证据能否同时解释决定归类方式与结论
* 拆并或重量结论变化后重新判定
* 可下载 CSV，逐主单保留原码、当前归类、采用依据
* 同一规则版本重跑不新增对照；输出失败保留库内旧版
* 跨航次同类问题折叠对照
"""
import csv
import io

import pytest

from app import dictionary as dic
from app import store, voyage
from app.engine import evaluate
from app.errors import BusinessError


# ---------- 字典映射 ----------

def test_direct_map_rewrites_when_evidence_consistent():
    mapping = dic.classify_legacy('E001', {'line': 50}, {'COLUMN_COUNT'})
    assert mapping['current_code'] == 'COLUMN_COUNT'
    assert mapping['mapping_status'] == dic.STATUS_DIRECT
    assert mapping['evidence_consistent'] is True
    assert 'E001' in mapping['basis'] and 'COLUMN_COUNT' in mapping['basis']
    # 原码永不改名
    assert mapping['original_code'] == 'E001'


def test_direct_map_marks_change_when_current_rule_clears_it():
    mapping = dic.classify_legacy('E301', {'field': 'gross_kg'}, set())
    assert mapping['current_code'] == 'WEIGHT_MISMATCH'
    assert mapping['evidence_consistent'] is False
    assert '现行复核未再现' in mapping['basis']


def test_split_e101_chooses_by_evidence():
    missing = dic.classify_legacy('E101', {'raw': {'packages': 'N/A', 'net': '1', 'gross': '2'}}, {'MISSING_VALUE'})
    assert missing['current_code'] == 'MISSING_VALUE'
    assert missing['mapping_status'] == dic.STATUS_SPLIT

    bad = dic.classify_legacy('E101', {'raw': {'packages': 'abc', 'net': '1', 'gross': '2'}}, {'BAD_NUMBER'})
    assert bad['current_code'] == 'BAD_NUMBER'

    out_of_range = dic.classify_legacy('E101', {'raw': {'packages': '1', 'net': '5', 'gross': '2'}}, {'INVALID_RANGE'})
    assert out_of_range['current_code'] == 'INVALID_RANGE'


def test_split_e201_distinguishes_cycle_and_missing_parent():
    cycle = dic.classify_legacy('E201', {'path': ['LOOP-A', 'LOOP-B', 'LOOP-A']}, {'DEPENDENCY_CYCLE'})
    assert cycle['current_code'] == 'DEPENDENCY_CYCLE'
    missing = dic.classify_legacy('E201', {'path': ['UNKNOWN']}, {'MISSING_PARENT'})
    assert missing['current_code'] == 'MISSING_PARENT'


def test_split_without_evidence_is_retained_not_silently_renamed():
    mapping = dic.classify_legacy('E201', {}, set())
    assert mapping['current_code'] == 'UNMAPPED'
    assert mapping['mapping_status'] == dic.STATUS_RETAINED
    assert mapping['original_code'] == 'E201'
    assert '保留历史原码' in mapping['basis']


def test_unmapped_code_keeps_record_and_original():
    mapping = dic.classify_legacy('E999', {'note': '旧系统杂项'}, set())
    assert mapping['current_code'] == 'UNMAPPED'
    assert mapping['mapping_status'] == dic.STATUS_RETAINED
    assert mapping['original_code'] == 'E999'
    assert mapping['evidence_consistent'] is False


def test_e401_three_way_split():
    assert dic.classify_legacy('E401', {'classes': '3/5.1'}, set())['current_code'] == 'INCOMPATIBLE'
    assert dic.classify_legacy('E401', {'missing_un': True}, set())['current_code'] == 'MISSING_UN'
    assert dic.classify_legacy('E401', {'unknown_category': '9'}, set())['current_code'] == 'UNKNOWN_HAZARD'
    assert dic.classify_legacy('E401', {}, set())['current_code'] == 'UNMAPPED'


# ---------- 清单构建：现行批次 ----------

def test_checklist_keeps_one_row_per_master_bill_finding(sample):
    checklist = voyage.build_checklist(sample)
    rows = checklist['rows']
    # 脏批的阻断项都要出现，数量与封存阻断口径一致
    assert checklist['summary']['blocking_rows'] == sample['archive']['blocked']
    # 全部归属到某张主单或显式占位
    assert all(r['root_id'] for r in rows)
    # 现行发现原码即现行码，依据可复核
    assert all(r['mapping_status'] == dic.STATUS_CURRENT for r in rows)
    assert all(r['original_code'] == r['current_code'] for r in rows)
    assert all(dic.DICT_VERSION in r['basis'] for r in rows)


def test_unrooted_quarantine_records_are_not_lost(sample):
    checklist = voyage.build_checklist(sample)
    roots = {r['root_id'] for r in checklist['rows']}
    # ORPHAN / LOOP / CONFLICT / COLUMN 等无主单记录保留在未归属分组
    assert dic.UNROOTED_ROOT in roots
    unrooted = [r for r in checklist['rows'] if r['root_id'] == dic.UNROOTED_ROOT]
    codes = {r['original_code'] for r in unrooted}
    assert {'COLUMN_COUNT', 'DUPLICATE_CONFLICT', 'MISSING_PARENT', 'DEPENDENCY_CYCLE'} <= codes


def test_voyage_level_declaration_difference(sample):
    checklist = voyage.build_checklist(sample)
    voyage_rows = [r for r in checklist['rows'] if r['root_id'] == dic.VOYAGE_ROOT]
    assert any(r['current_code'] == 'DECLARATION_MISMATCH' for r in voyage_rows)


def test_clean_batch_checklist_empty(clean):
    checklist = voyage.build_checklist(clean)
    assert checklist['rows'] == []
    assert checklist['summary']['total_rows'] == 0
    assert checklist['summary']['blocking_rows'] == 0


# ---------- 历史旧码并入当前批次 ----------

def test_legacy_findings_coexist_with_current_and_keep_original(sample):
    legacy = [
        {'node_id': 'M001', 'code': 'E301', 'evidence': {'field': 'gross_kg', 'difference': '1'}},
        {'node_id': 'GHOST-1', 'code': 'E999', 'evidence': {'note': '旧系统'}},
    ]
    checklist = voyage.build_checklist(sample, legacy, legacy_rule_version='2026.10.01-r1')
    rows = checklist['rows']
    e301 = next(r for r in rows if r['original_code'] == 'E301')
    assert e301['current_code'] == 'WEIGHT_MISMATCH'
    assert e301['rule_version'] == '2026.10.01-r1'
    # M001 当前确实仍有 1kg 毛重差异 -> 新旧证据同时解释，仍阻断
    assert e301['evidence_consistent'] == '是'
    assert e301['current_decision'] == dic.DECISION_BLOCKING

    ghost = next(r for r in rows if r['original_code'] == 'E999')
    assert ghost['current_code'] == 'UNMAPPED'
    assert ghost['current_decision'] == dic.DECISION_MANUAL
    assert ghost['blocking'] == '是'  # 无映射不能被悄悄放过
    assert checklist['summary']['retained_rows'] == 1


def test_weight_conclusion_change_redecides_resolved(clean):
    # 干净批当前全部 BALANCED；历史上 M001 有重量差异 -> 现行口径重判为已消除
    legacy = [{'node_id': 'M001', 'code': 'E301',
               'evidence': {'field': 'gross_kg', 'difference': '5'}}]
    checklist = voyage.build_checklist(clean, legacy, '2026.10.01-r1')
    row = next(r for r in checklist['rows'] if r['original_code'] == 'E301')
    assert row['current_decision'] == dic.DECISION_RESOLVED
    assert row['blocking'] == '否'
    assert row['evidence_consistent'] == '否'


def test_grouping_conclusion_change_redecides(clean):
    legacy = [
        {'node_id': 'LOOP-A', 'code': 'E201', 'evidence': {'path': ['LOOP-A', 'LOOP-B', 'LOOP-A']}},
        {'node_id': 'ORPHAN', 'code': 'E201', 'evidence': {'path': ['UNKNOWN']}},
    ]
    checklist = voyage.build_checklist(clean, legacy, '2026.10.01-r1')
    by_node = {r['node_id']: r for r in checklist['rows'] if r['mapping_status'] == dic.STATUS_SPLIT}
    # 干净批图中两类结构错误均已不存在 -> 拆并变化后重新判定为消除
    assert by_node['LOOP-A']['current_code'] == 'DEPENDENCY_CYCLE'
    assert by_node['LOOP-A']['current_decision'] == dic.DECISION_RESOLVED
    assert by_node['ORPHAN']['current_code'] == 'MISSING_PARENT'
    assert by_node['ORPHAN']['current_decision'] == dic.DECISION_RESOLVED


def test_split_without_evidence_blocks_manual(clean):
    legacy = [{'node_id': 'M002', 'code': 'E401', 'evidence': {}}]
    checklist = voyage.build_checklist(clean, legacy, '2026.10.01-r1')
    row = next(r for r in checklist['rows'] if r['original_code'] == 'E401')
    assert row['current_code'] == 'UNMAPPED'
    assert row['current_decision'] == dic.DECISION_MANUAL
    assert row['blocking'] == '是'


# ---------- CSV 下载 ----------

def test_csv_download_contains_all_rows_and_headers(sample):
    checklist = voyage.build_checklist(sample)
    text = voyage.to_csv(checklist)
    reader = list(csv.reader(io.StringIO(text.lstrip('﻿'))))
    assert reader[0][0] == '主单号'
    assert len(reader) - 1 == checklist['summary']['total_rows']
    # 原码、当前归类、依据三列都在
    header = reader[0]
    for name in ('历史原因码', '当前归类', '采用依据', '规则版本', '字典版本'):
        assert name in header


def test_csv_formula_injection_escaped(sample):
    checklist = voyage.build_checklist(sample, [
        {'node_id': '=CMD', 'code': 'E999', 'evidence': {}}], 'r1')
    text = voyage.to_csv(checklist)
    assert "'=CMD" in text


# ---------- 存储：幂等与失败保留旧版 ----------

def test_store_idempotent_same_version_no_new_row(tmp_path):
    db = str(tmp_path / 'v.sqlite')
    state = evaluate({'dataset': 'clean'})
    checklist = voyage.build_checklist(state)
    first = store.save_checklist(db, state['manifest']['run_id'], checklist)
    second = store.save_checklist(db, state['manifest']['run_id'], checklist)
    assert first == second
    assert len(store.list_checklists(db)) == 1


def test_build_failure_keeps_previous_checklist(tmp_path, monkeypatch):
    db = str(tmp_path / 'v.sqlite')
    state = evaluate({'dataset': 'clean'})
    store.save(db, state)
    good = voyage.build_checklist(state)
    store.save_checklist(db, state['manifest']['run_id'], good)

    def boom(*args, **kwargs):
        raise RuntimeError('模拟构建失败')
    monkeypatch.setattr(voyage, 'build_checklist', boom)
    from app.voyage_handler import _build_or_fallback
    fallback, warning = _build_or_fallback(db, state['manifest']['run_id'], [], None)
    assert fallback['summary']['run_id'] == good['summary']['run_id']
    assert '已保留旧版' in warning
    # 库内仍是旧版，且没有新增行
    assert store.get_checklist(db, state['manifest']['run_id'])['summary'] == good['summary']
    assert len(store.list_checklists(db)) == 1


def test_build_failure_without_previous_raises(tmp_path, monkeypatch):
    db = str(tmp_path / 'v.sqlite')
    state = evaluate({'dataset': 'clean'})
    store.save(db, state)

    def boom(*args, **kwargs):
        raise RuntimeError('首次构建即失败')
    monkeypatch.setattr(voyage, 'build_checklist', boom)
    from app.voyage_handler import _build_or_fallback
    with pytest.raises(BusinessError) as exc:
        _build_or_fallback(db, state['manifest']['run_id'], [], None)
    assert exc.value.code == 'CHECKLIST_BUILD_FAILED'
    assert store.list_checklists(db) == []


# ---------- API 端到端 ----------

def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'api.sqlite'))
    return TestClient(main.app)


def test_api_checklist_lifecycle_and_idempotent_rerun(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'dirty'}).json()
    rid = run['manifest']['run_id']
    # 生成前查询 404
    assert c.get(f'/api/runs/{rid}/checklist').status_code == 404
    resp = c.post(f'/api/runs/{rid}/checklist', json={})
    assert resp.status_code == 200
    data = resp.json()['checklist']
    assert data['summary']['total_rows'] >= 1
    # 下载
    dl = c.get(f'/api/runs/{rid}/checklist/download')
    assert dl.status_code == 200
    assert dl.headers['content-type'].startswith('text/csv')
    assert 'attachment' in dl.headers['content-disposition']
    # 同版重跑：批号不变，再生成不新增对照
    assert c.post(f'/api/runs/{rid}/rerun').json()['manifest']['run_id'] == rid
    c.post(f'/api/runs/{rid}/checklist', json={})
    assert len(c.get('/api/runs').json()) == 1
    # 清单表仍只有一行
    assert len(store.list_checklists(str(tmp_path / 'api.sqlite'))) == 1
    # 页面可打开
    assert c.get(f'/voyage?run_id={rid}').status_code == 200


def test_api_legacy_payload_validation(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    bad = c.post(f'/api/runs/{rid}/checklist', json={'legacy_findings': [{'node_id': 'X'}]})
    assert bad.status_code == 422
    ok = c.post(f'/api/runs/{rid}/checklist',
                json={'legacy_findings': [{'node_id': 'X', 'code': 'E999', 'evidence': {}}]})
    assert ok.status_code == 200
    row = next(r for r in ok.json()['checklist']['rows'] if r['original_code'] == 'E999')
    assert row['current_code'] == 'UNMAPPED'


def test_api_cross_voyage_comparison_folds_same_key(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid_a = c.post('/api/runs', json={'dataset': 'dirty'}).json()['manifest']['run_id']
    rid_b = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    for rid in (rid_a, rid_b):
        c.post(f'/api/runs/{rid}/checklist', json={})
    resp = c.get('/api/voyage/compare', params={'run_ids': f'{rid_a},{rid_b}'})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload['missing'] == []
    # 每个对照键唯一，同节点同原码不重复
    keys = [(x['root_id'], x['node_id'], x['original_code']) for x in payload['comparison']]
    assert len(keys) == len(set(keys))
    dl = c.get('/api/voyage/compare/download', params={'run_ids': f'{rid_a},{rid_b}'})
    assert dl.status_code == 200
    assert 'attachment' in dl.headers['content-disposition']


def test_cross_voyage_same_version_rerun_does_not_add_entry(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'dirty'}).json()['manifest']['run_id']
    c.post(f'/api/runs/{rid}/checklist', json={})
    once = voyage.compare_voyages([store.get_checklist(str(tmp_path / 'api.sqlite'), rid)])
    # 模拟同版重跑后再次对照（同批号同内容）
    twice = voyage.compare_voyages([
        store.get_checklist(str(tmp_path / 'api.sqlite'), rid),
        store.get_checklist(str(tmp_path / 'api.sqlite'), rid)])
    assert len(twice) == len(once)
    assert all(len(x['history']) == 1 for x in twice)


def test_compare_missing_run_reported(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    c.post(f'/api/runs/{rid}/checklist', json={})
    payload = c.get('/api/voyage/compare', params={'run_ids': f'{rid},not-there'}).json()
    assert payload['missing'] == ['not-there']


def test_api_build_failure_returns_previous_with_warning(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    import app.voyage as vmod
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    good = c.post(f'/api/runs/{rid}/checklist', json={}).json()['checklist']

    def boom(*args, **kwargs):
        raise RuntimeError('模拟下游异常')
    monkeypatch.setattr(vmod, 'build_checklist', boom)
    resp = c.post(f'/api/runs/{rid}/checklist', json={})
    assert resp.status_code == 200
    body = resp.json()
    assert 'warning' in body and '已保留旧版' in body['warning']
    assert body['checklist']['summary']['run_id'] == good['summary']['run_id']
