"""原因码字典与跨版本映射。

同一票舱单会按多版规则复核，旧批次写下的原因码在新字典里可能被改写。
本模块把映射过程显式化、可复核：

* DIRECT   一对一对应，且新旧证据能同时解释 -> 统一改写为现行码
* SPLIT    旧码一对多，凭历史证据拆分到唯一现行码
* RETAINED 字典无对应或证据不能同时解释 -> 保留历史原码，当前归类记 UNMAPPED，
            绝不静默改名，也不丢弃记录
* CURRENT  当前规则版本直接产生的发现，原码即现行码

字典本身带版本号，清单逐行记录采用依据，报关员可沿历史原码保持可比，
也可按现行代码统一处理阻断。
"""

DICT_VERSION = '2026.11.17-d1'

# 现行原因码字典：码 -> （中文说明，来源模块，是否阻断封存）
CURRENT_DICT = {
    # 采信 / 包装
    'COLUMN_COUNT': ('列数错误', 'intake', True),
    'MISSING_ID': ('编号缺失', 'intake', True),
    'DUPLICATE_CONFLICT': ('重复编号冲突', 'intake', True),
    'MISSING_VALUE': ('数值缺失', 'intake', True),
    'BAD_NUMBER': ('数值格式错误', 'packaging', True),
    'BAD_TIME': ('时间格式错误', 'intake', True),
    'BAD_UNIT': ('单位不受支持', 'packaging', True),
    'UNKNOWN_REFERENCE': ('引用对象不存在', 'grouping', True),
    'INVALID_RANGE': ('范围错误', 'packaging', True),
    'VERSION_CONFLICT': ('版本冲突', 'intake', True),
    'EMPTY_FILE': ('空文件', 'intake', True),
    'BLANK_FILE': ('全空行', 'intake', True),
    'HEADER_ONLY': ('仅有表头', 'intake', True),
    # 拆并
    'MISSING_PARENT': ('缺失父级', 'grouping', True),
    'DEPENDENCY_CYCLE': ('依赖环', 'grouping', True),
    # 危险品
    'UNKNOWN_HAZARD': ('未知危险品分类', 'hazard', True),
    'MISSING_UN': ('UN编号缺失或不合规', 'hazard', True),
    'INCOMPATIBLE': ('不相容危险品同箱', 'hazard', True),
    # 重量 / 报关
    'WEIGHT_MISMATCH': ('逐层重量或件数不符', 'weight', True),
    'BALANCED': ('守恒一致', 'weight', False),
    'DECLARATION_MISMATCH': ('报关总量与申报不符', 'reconcile', True),
    # 无法对应旧码时的显式占位
    'UNMAPPED': ('未映射-保留历史原码', 'legacy', True),
}

# 上一版规则（r1）遗留原因码，仅存在于历史批次，现行规则不再产生
LEGACY_REASONS = {
    'E001': '列数异常',
    'E002': '编号空缺',
    'E003': '同号冲突',
    'E101': '数值问题',      # 一对多：缺失 / 越界 / 格式
    'E201': '结构异常',      # 一对多：缺父级 / 依赖环
    'E301': '重量不符',
    'E401': '危险品问题',    # 一对多：未知分类 / 缺UN / 不相容
    'E999': '其他待查',      # 新字典无对应
}

# 一对一映射：旧码 -> 现行码
DIRECT_MAP = {
    'E001': 'COLUMN_COUNT',
    'E002': 'MISSING_ID',
    'E003': 'DUPLICATE_CONFLICT',
    'E301': 'WEIGHT_MISMATCH',
}

# 一对多映射：旧码 -> 候选现行码，最终取值由证据决定
SPLIT_MAP = {
    'E101': ['MISSING_VALUE', 'INVALID_RANGE', 'BAD_NUMBER'],
    'E201': ['MISSING_PARENT', 'DEPENDENCY_CYCLE'],
    'E401': ['UNKNOWN_HAZARD', 'MISSING_UN', 'INCOMPATIBLE'],
}

# 新字典完全没有对应的旧码
UNMAPPED_LEGACY = {'E999'}

STATUS_CURRENT = 'CURRENT'
STATUS_DIRECT = 'DIRECT'
STATUS_SPLIT = 'SPLIT'
STATUS_RETAINED = 'RETAINED'

STATUS_LABELS = {
    STATUS_CURRENT: '现行码',
    STATUS_DIRECT: '一对一改写',
    STATUS_SPLIT: '按证据拆分',
    STATUS_RETAINED: '保留历史原码',
}

DECISION_BLOCKING = 'BLOCKING'
DECISION_RESOLVED = 'RESOLVED'
DECISION_MANUAL = 'MANUAL_REVIEW'

DECISION_LABELS = {
    DECISION_BLOCKING: '当前仍阻断',
    DECISION_RESOLVED: '现行复核已消除',
    DECISION_MANUAL: '待人工判定',
}

UNROOTED_ROOT = '__UNROOTED__'
VOYAGE_ROOT = '__VOYAGE__'
ROOT_PLACEHOLDER_LABELS = {
    UNROOTED_ROOT: '未归属（隔离记录）',
    VOYAGE_ROOT: '航次级总量',
}


def current_label(code):
    return CURRENT_DICT.get(code, ('未知代码', 'unknown', True))[0]


def legacy_label(code):
    return LEGACY_REASONS.get(code, code)


def is_current_code(code):
    return code in CURRENT_DICT


def _split_choice(code, evidence):
    """按历史证据从一对多候选里挑出唯一现行码；证据不足返回 None。"""
    evidence = evidence or {}
    if code == 'E101':
        raw = evidence.get('raw') or {}
        packages = str(raw.get('packages', '')).strip()
        net = str(raw.get('net', '')).strip()
        gross = str(raw.get('gross', '')).strip()
        sentinels = {'', '-', 'N/A', 'NULL', '未知'}
        if packages in sentinels or net in sentinels or gross in sentinels:
            return 'MISSING_VALUE'
        try:
            net_v = float(net)
            gross_v = float(gross)
        except (TypeError, ValueError):
            return 'BAD_NUMBER'
        if net_v < 0 or gross_v < net_v:
            return 'INVALID_RANGE'
        return 'BAD_NUMBER'
    if code == 'E201':
        path = evidence.get('path') or []
        if not path:
            return None
        # 成环路径首尾同一节点；缺父级证据只挂一个不存在的上级
        if len(path) >= 2 and path[0] == path[-1]:
            return 'DEPENDENCY_CYCLE'
        return 'MISSING_PARENT'
    if code == 'E401':
        if evidence.get('classes'):
            return 'INCOMPATIBLE'
        if evidence.get('missing_un'):
            return 'MISSING_UN'
        if evidence.get('unknown_category'):
            return 'UNKNOWN_HAZARD'
        return None
    return None


def classify_legacy(original_code, evidence, current_codes):
    """把一条历史发现归类到现行字典。

    original_code : 历史批次写下的原因码（永不改写、永不丢失）
    evidence      : 历史证据（raw/path/classes 等），用于一对多拆分与比对
    current_codes : 同一节点在现行规则复核下出现的现行码集合

    返回 dict：original_code/current_code/mapping_status/basis/evidence_consistent
    """
    current_codes = set(current_codes or ())
    if original_code in DIRECT_MAP:
        target = DIRECT_MAP[original_code]
        consistent = target in current_codes
        if consistent:
            basis = (f'字典{DICT_VERSION}：{original_code}（{legacy_label(original_code)}）'
                     f'与{target}（{current_label(target)}）一对一对应，'
                     f'历史证据与现行复核在同一节点同时成立，统一改写为现行码')
        else:
            basis = (f'字典{DICT_VERSION}：{original_code}一对一对应{target}，'
                     f'现行复核未再现该问题，仍按字典改写并标记结论变化，历史原码保留备查')
        return {'original_code': original_code, 'current_code': target,
                'mapping_status': STATUS_DIRECT, 'basis': basis,
                'evidence_consistent': consistent}

    if original_code in SPLIT_MAP:
        choice = _split_choice(original_code, evidence)
        if choice is None:
            basis = (f'字典{DICT_VERSION}：{original_code}（{legacy_label(original_code)}）'
                     f'可拆分为{"/".join(SPLIT_MAP[original_code])}，但历史证据不足以区分，'
                     f'新旧证据无法同时解释任一现行码，保留历史原码待人工判定')
            return {'original_code': original_code, 'current_code': 'UNMAPPED',
                    'mapping_status': STATUS_RETAINED, 'basis': basis,
                    'evidence_consistent': False}
        consistent = choice in current_codes
        basis = (f'字典{DICT_VERSION}：{original_code}为一对多旧码，'
                 f'依据历史证据{_evidence_brief(evidence)}拆分采用{choice}'
                 f'（{current_label(choice)}）'
                 + ('，与现行复核证据一致' if consistent else '，现行复核未再现，结论按新证据重判'))
        return {'original_code': original_code, 'current_code': choice,
                'mapping_status': STATUS_SPLIT, 'basis': basis,
                'evidence_consistent': consistent}

    # 无对应字典项：不丢记录、不静默改名
    basis = (f'字典{DICT_VERSION}无{original_code}（{legacy_label(original_code)}）对应项，'
             f'不做静默改名，原码沿历史保留，当前归类记UNMAPPED并阻断待人工处理')
    return {'original_code': original_code, 'current_code': 'UNMAPPED',
            'mapping_status': STATUS_RETAINED, 'basis': basis,
            'evidence_consistent': False}


def classify_current(code):
    """当前规则版本直接产生的发现。"""
    return {'original_code': code, 'current_code': code,
            'mapping_status': STATUS_CURRENT,
            'basis': f'现行规则直接产生，原码即现行码（字典{DICT_VERSION}）',
            'evidence_consistent': True}


def _evidence_brief(evidence):
    if not evidence:
        return '（无证据）'
    if 'raw' in evidence:
        raw = evidence['raw']
        return '（原值' + '/'.join(f'{k}={v}' for k, v in raw.items() if v not in (None, '')) + '）'
    if 'path' in evidence:
        return f'（引用路径{"->".join(evidence["path"])}）'
    if 'classes' in evidence:
        return f'（同箱类别{evidence["classes"]}）'
    return '（历史证据）'
