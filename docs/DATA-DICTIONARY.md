# 舱单数据定义

id是主单、分组单或箱货明细唯一字符串编号，parent为空表示主单。节点申报件数、净重和毛重是其完整子树的申报总量，校验时只加直接子节点，避免多层重复计数。包装carton=1件，pallet=10件；重量kg与t统一为kg。件数必须非负整数，净重非负且毛重不得低于净重。
拆并结构采用每节点最多一个父级的有向图；自环、长环、缺父级均阻断封存，保留环路径。不会以平面求和替代逐层校验。
危险品按明确分类与UN编号检查，3类与5.1类禁止同箱；未知分类和缺失编号需复核。此演示规则只覆盖文中三类，不替代运输合规审批。
报关总量只汇总主单，与context.json内的申报件数、净重和毛重逐项比较。重复同值折叠，重复编号冲突全部隔离。原始计数=采纳+隔离+空行+重复表头+重复同值行。
封存需所有图、重量、危险品和采信问题处理完毕。数值均Decimal，对外字符串保留精度，编号不转整数。

## 原因码跨版本字典（航次质量对照）

现行原因码定义在 `app/dictionary.py` 的 CURRENT_DICT，字典版本 `DICT_VERSION` 随每行清单持久化。历史批次原因码（E001/E002/E003/E101/E201/E301/E401/E999）不直接出现在现行规则输出里，只经映射进入对照清单：

- 一对一 DIRECT：E001→COLUMN_COUNT、E002→MISSING_ID、E003→DUPLICATE_CONFLICT、E301→WEIGHT_MISMATCH。
- 一对多 SPLIT：E101→MISSING_VALUE/INVALID_RANGE/BAD_NUMBER（看空值哨兵、数值可解析性与净毛重范围）；E201→MISSING_PARENT/DEPENDENCY_CYCLE（看引用路径是否成环）；E401→UNKNOWN_HAZARD/MISSING_UN/INCOMPATIBLE（看 classes/missing_un/unknown_category 证据）。
- RETAINED：E999 及证据不足的拆分，当前归类记 UNMAPPED，原码沿历史保留，继续阻断待人工判定。

清单行字段见 `app/voyage.py` 的 CSV_FIELDS：主单号、问题节点、来源模块、历史原因码与说明、当前归类与说明、归类方式、新旧证据是否同时解释、当前结论、是否阻断、采用依据、规则版本、字典版本。无主单的隔离记录归入 `__UNROOTED__`，航次级报关总量差异归入 `__VOYAGE__`，两类占位记录同样不丢失。
