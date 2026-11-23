import sqlite3, json
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("CREATE TABLE IF NOT EXISTS checklists (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, dict_version TEXT NOT NULL, checklist TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(run_id,dict_version))")
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def save_checklist(path, run_id, checklist):
    """持久化航次质量对照清单。

    清单已在内存构建成功后才写库；(run_id,dict_version) 唯一，
    同一规则版本重跑复用同批号，不新增对照。
    写入失败时库内旧版保持不动，由调用方回退返回旧版。
    """
    payload=json.dumps(checklist,ensure_ascii=False)
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO checklists(run_id,dict_version,checklist) VALUES(?,?,?)',
                    (run_id,checklist['summary']['dict_version'],payload))
        row=con.execute('SELECT checklist FROM checklists WHERE run_id=? AND dict_version=?',
                        (run_id,checklist['summary']['dict_version'])).fetchone()
    return json.loads(row['checklist'])

def get_checklist(path, run_id, dict_version=None):
    with connect(path) as con:
        if dict_version is None:
            row=con.execute('SELECT checklist FROM checklists WHERE run_id=? ORDER BY id DESC LIMIT 1',(run_id,)).fetchone()
        else:
            row=con.execute('SELECT checklist FROM checklists WHERE run_id=? AND dict_version=?',(run_id,dict_version)).fetchone()
    if not row:
        raise BusinessError('CHECKLIST_NOT_FOUND','该批次尚无航次质量对照清单',404)
    return json.loads(row['checklist'])

def list_checklists(path):
    with connect(path) as con:
        rows=con.execute('SELECT run_id,dict_version,created_at FROM checklists ORDER BY id DESC LIMIT 100').fetchall()
    return [dict(r) for r in rows]

def checklists_for_runs(path, run_ids):
    if not run_ids:
        return []
    placeholders=','.join('?' for _ in run_ids)
    with connect(path) as con:
        rows=con.execute(f'SELECT checklist FROM checklists WHERE run_id IN ({placeholders}) ORDER BY id',
                         run_ids).fetchall()
    return [json.loads(r['checklist']) for r in rows]
