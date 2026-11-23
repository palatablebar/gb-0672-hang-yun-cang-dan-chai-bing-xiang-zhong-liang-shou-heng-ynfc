"""航次质量对照清单构建。

逐张主单保留：历史原原因码、当前归类、采用依据，并让拆并或重量结论变化后
重新判定。当前规则批次直接取现行发现；历史批次的旧码经 dictionary.classify_legacy
映射。无映射的旧码保留为 UNMAPPED，记录不丢失、不静默改名。
"""
import csv
import io

from app import dictionary as dic
from app.config import RULE_VERSION

CSV_FIELDS = [
    ('root_id', '主单号'), ('root_label', '主单名称'), ('node_id', '问题节点'),
    ('finding_source', '来源模块'), ('original_code', '历史原因码'),
    ('original_label', '原码说明'), ('current_code', '当前归类'),
    ('current_label', '现行码说明'), ('mapping_status', '归类方式'),
    ('mapping_label', '归类说明'), ('evidence_consistent', '新旧证据同时解释'),
    ('current_decision', '当前结论'), ('decision_label', '结论说明'),
    ('blocking', '是否阻断'), ('basis', '采用依据'), ('rule_version', '规则版本'),
    ('dict_version', '字典版本'),
]


def _root_of(node_id, parent_map, roots):
    """沿父级链找到主单；链断裂或成环归入未归属。"""
    if node_id == dic.VOYAGE_ROOT:
        return dic.VOYAGE_ROOT
    seen = set()
    cur = node_id
    while cur:
        if cur in roots:
            return cur
        if cur in seen:
            return dic.UNROOTED_ROOT
        seen.add(cur)
        cur = parent_map.get(cur, '')
        if not cur:
            return dic.UNROOTED_ROOT
    return dic.UNROOTED_ROOT


def build_parent_map(records):
    return {r['id']: (r.get('parent') or '').strip() for r in records}


def _root_label(root):
    return dic.ROOT_PLACEHOLDER_LABELS.get(root, root)


def _current_findings(state):
    """抽取当前批次的全部问题发现，按节点 id 归集。"""
    roots = set(state['graph']['roots'])
    parent_map = build_parent_map(state['records'])
    findings = []

    def add(node_id, source, code, evidence, blocking):
        findings.append({'node_id': node_id, 'source': source, 'code': code,
                         'evidence': evidence or {}, 'blocking': blocking,
                         'root': _root_of(node_id, parent_map, roots)})

    for q in state.get('quarantine', []):
        add(q.get('id') or dic.UNROOTED_ROOT, 'intake', q['code'],
            {'raw': q.get('raw', {}), 'line': q.get('line'), 'detail': q.get('detail')}, True)
    for err in state['graph'].get('errors', []):
        add(err['id'], 'grouping', err['code'], {'path': err.get('path', [])}, True)
    for hz in state.get('hazard_issues', []):
        evidence = {'line': hz.get('line'), 'classes': hz.get('classes')}
        add(hz['id'], 'hazard', hz['code'], evidence, True)
    for w in state.get('weight_checks', []):
        if w['code'] == 'BALANCED':
            continue
        add(w['id'], 'weight', w['code'],
            {'field': w['field'], 'declared': w['declared'],
             'children_sum': w['children_sum'], 'difference': w['difference']}, True)
    for field in state.get('balance', {}).get('declaration_differences', []):
        add(dic.VOYAGE_ROOT, 'reconcile', 'DECLARATION_MISMATCH',
            {'field': field,
             'declared': state['context']['declaration'].get(field),
             'manifest': state['balance'].get(field)}, True)
    return findings


def _current_code_set(findings):
    grouped = {}
    for f in findings:
        grouped.setdefault(f['node_id'], set()).add(f['code'])
    return grouped


def _weight_redecision(code, evidence, state):
    """按现行重量结论重新判定：同节点同核验项的现行差异为零即消除。"""
    if code != 'WEIGHT_MISMATCH':
        return None
    field = (evidence or {}).get('field')
    for w in state.get('weight_checks', []):
        if w['id'] == evidence.get('node_id') and (field is None or w['field'] == field):
            return dic.DECISION_RESOLVED if w['code'] == 'BALANCED' else dic.DECISION_BLOCKING
    # 现行批次已无该节点（拆并移除/隔离）：现行重量结论不再存在
    node_ids = {r['id'] for r in state.get('records', [])}
    if evidence.get('node_id') not in node_ids:
        return dic.DECISION_MANUAL
    return dic.DECISION_MANUAL


def _group_redecision(code, evidence, state):
    """按现行拆并结论重新判定结构类问题。"""
    node_id = (evidence or {}).get('node_id')
    for err in state['graph'].get('errors', []):
        if err['id'] == node_id and err['code'] == code:
            return dic.DECISION_BLOCKING
    # 现行图中该结构错误不再出现 -> 拆并变化后已消除
    if code in ('MISSING_PARENT', 'DEPENDENCY_CYCLE'):
        return dic.DECISION_RESOLVED
    return dic.DECISION_MANUAL


def _redecision(mapping, evidence, state):
    code = mapping['current_code']
    if code == 'UNMAPPED':
        return dic.DECISION_MANUAL
    if code in ('MISSING_PARENT', 'DEPENDENCY_CYCLE'):
        return _group_redecision(code, evidence, state)
    if code == 'WEIGHT_MISMATCH':
        return _weight_redecision(code, evidence, state)
    node_id = evidence.get('node_id')
    current = _current_code_set(_current_findings(state))
    if node_id in current and code in current[node_id]:
        return dic.DECISION_BLOCKING
    return dic.DECISION_RESOLVED


def _row(root, node_id, source, mapping, decision, blocking, rule_version, dict_version):
    return {
        'root_id': root, 'root_label': _root_label(root), 'node_id': node_id,
        'finding_source': source,
        'original_code': mapping['original_code'],
        'original_label': dic.legacy_label(mapping['original_code'])
            if mapping['original_code'] in dic.LEGACY_REASONS
            else dic.current_label(mapping['original_code']),
        'current_code': mapping['current_code'],
        'current_label': dic.current_label(mapping['current_code']),
        'mapping_status': mapping['mapping_status'],
        'mapping_label': dic.STATUS_LABELS[mapping['mapping_status']],
        'evidence_consistent': '是' if mapping['evidence_consistent'] else '否',
        'current_decision': decision,
        'decision_label': dic.DECISION_LABELS[decision],
        'blocking': '是' if blocking else '否',
        'basis': mapping['basis'], 'rule_version': rule_version,
        'dict_version': dict_version,
    }


def build_checklist(state, legacy_findings=None, legacy_rule_version=None):
    """构建航次质量对照清单。

    state            : 当前规则版本完整复核结果（engine.evaluate 产物）
    legacy_findings  : 历史批次发现 [{'node_id','code','evidence'}]，可选
    legacy_rule_version: 历史批次规则版本号，缺省记 LEGACY
    """
    findings = _current_findings(state)
    by_node = _current_code_set(findings)
    rows = []

    for f in sorted(findings, key=lambda x: (x['root'], x['node_id'], x['code'])):
        mapping = dic.classify_current(f['code'])
        rows.append(_row(f['root'], f['node_id'], f['source'], mapping,
                         dic.DECISION_BLOCKING if f['blocking'] else dic.DECISION_RESOLVED,
                         f['blocking'], RULE_VERSION, dic.DICT_VERSION))

    for legacy in sorted(legacy_findings or [], key=lambda x: (x['node_id'], x['code'])):
        node_id = legacy['node_id']
        evidence = dict(legacy.get('evidence') or {})
        evidence['node_id'] = node_id
        roots = set(state['graph']['roots'])
        root = _root_of(node_id, build_parent_map(state['records']), roots)
        mapping = dic.classify_legacy(legacy['code'], legacy.get('evidence'),
                                      by_node.get(node_id, set()))
        decision = _redecision(mapping, evidence, state)
        # UNMAPPED 与待人工项继续阻断，保证无映射旧码不被悄悄放过
        blocking = decision != dic.DECISION_RESOLVED
        rows.append(_row(root, node_id, legacy.get('source', 'legacy'), mapping,
                         decision, blocking,
                         legacy_rule_version or 'LEGACY', dic.DICT_VERSION))

    summary = {
        'run_id': state['manifest']['run_id'],
        'rule_version': state['manifest']['rule_version'],
        'dict_version': dic.DICT_VERSION,
        'source': state.get('source', ''),
        'total_rows': len(rows),
        'current_rows': sum(1 for r in rows if r['mapping_status'] == dic.STATUS_CURRENT),
        'legacy_rows': sum(1 for r in rows if r['mapping_status'] != dic.STATUS_CURRENT),
        'retained_rows': sum(1 for r in rows if r['mapping_status'] == dic.STATUS_RETAINED),
        'blocking_rows': sum(1 for r in rows if r['blocking'] == '是'),
        'resolved_rows': sum(1 for r in rows if r['current_decision'] == dic.DECISION_RESOLVED),
        'manual_rows': sum(1 for r in rows if r['current_decision'] == dic.DECISION_MANUAL),
        'roots': sorted({r['root_id'] for r in rows}),
    }
    return {'rows': rows, 'summary': summary}


def to_csv(checklist):
    """可下载的 CSV（utf-8-sig，Excel 直接打开不乱码；防公式注入）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow([label for _, label in CSV_FIELDS])
    for row in checklist['rows']:
        cells = []
        for key, _ in CSV_FIELDS:
            value = str(row[key])
            if value[:1] in ('=', '+', '-', '@'):
                value = "'" + value
            cells.append(value)
        writer.writerow(cells)
    return '﻿' + buffer.getvalue()


def compare_voyages(checklists):
    """跨航次同类问题对照：以 (主单,节点,历史原码) 为稳定键。

    同一规则版本重跑产生相同行键，对照只折叠不新增条目。
    """
    merged = {}
    order = []
    for checklist in checklists:
        run_id = checklist['summary']['run_id']
        rule_version = checklist['summary']['rule_version']
        for row in checklist['rows']:
            key = (row['root_id'], row['node_id'], row['original_code'])
            if key not in merged:
                merged[key] = {'key': key, 'root_id': row['root_id'],
                               'root_label': row['root_label'],
                               'node_id': row['node_id'],
                               'original_code': row['original_code'],
                               'original_label': row['original_label'],
                               'occurrences': [], 'history': []}
                order.append(key)
            item = merged[key]
            fingerprint = (run_id, row['current_code'], row['current_decision'],
                           row['mapping_status'])
            if fingerprint in item['occurrences']:
                # 同版重跑：不新增对照条目
                continue
            item['occurrences'].append(fingerprint)
            item['history'].append({
                'run_id': run_id, 'rule_version': rule_version,
                'current_code': row['current_code'],
                'current_label': row['current_label'],
                'mapping_status': row['mapping_status'],
                'mapping_label': row['mapping_label'],
                'current_decision': row['current_decision'],
                'decision_label': row['decision_label'],
                'blocking': row['blocking'], 'basis': row['basis'],
                'evidence_consistent': row['evidence_consistent'],
            })
    return [merged[k] for k in order]


def comparison_to_csv(comparison):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(['主单号', '主单名称', '问题节点', '历史原因码', '原码说明',
                     '出现次数', '批号', '规则版本', '当前归类', '归类方式',
                     '当前结论', '是否阻断', '新旧证据同时解释', '采用依据'])
    for item in comparison:
        for history in item['history']:
            writer.writerow([item['root_label'], item['root_id'], item['node_id'],
                             item['original_code'], item['original_label'],
                             len(item['occurrences']), history['run_id'],
                             history['rule_version'], history['current_code'],
                             history['mapping_label'], history['decision_label'],
                             history['blocking'], history['evidence_consistent'],
                             history['basis']])
    return '﻿' + buffer.getvalue()
