"""Import selected legacy companion state without touching source databases."""
from contextlib import closing
from pathlib import Path
import json
import sqlite3

import yaml

from ..core.store import Store, digest, encode

PERSONA_TABLES = ('persona_global_state', 'persona_session_state', 'persona_events', 'persona_exchange_log')
TABLES = (*PERSONA_TABLES, 'reminders')


def source_config(root):
    base = next((root / name for name in ('config.yaml', 'config.yml') if (root / name).is_file()), None)
    cfg = {}
    for path in (base, root / 'state/config.runtime.yaml'):
        if path is None or not path.is_file():
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError('旧配置路径不能超出所选备份目录')
        try:
            values = yaml.safe_load(path.read_text('utf-8')) or {}
        except yaml.YAMLError:
            raise ValueError('旧配置无法解析为 YAML') from None
        if not isinstance(values, dict):
            raise ValueError('旧配置必须是 YAML 对象')
        for key, value in values.items():
            cfg[key] = {**cfg[key], **value} if isinstance(cfg.get(key), dict) and isinstance(value, dict) else value
    return cfg


def database_path(root, configured, defaults, warnings):
    # A backup can contain obsolete absolute deployment paths. Never follow
    # those paths out of the explicitly selected source root.
    if configured:
        path = (root / str(configured)).resolve()
        if path.is_relative_to(root) and path.is_file():
            return path
        warnings.append('配置中的状态库路径未在所选旧库内找到；仅检查备份内的标准位置。')
    paths = { (root / name).resolve() for name in defaults
              if (root / name).resolve().is_relative_to(root) and (root / name).is_file() }
    if len(paths) > 1:
        raise ValueError('发现多个同类状态库，请在旧备份 config.yaml 中明确相对路径后重试')
    return next(iter(paths), None)


def read_tables(path, names):
    if path is None:
        return {}
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('旧状态库完整性检查失败')
        existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {name: [dict(row) for row in conn.execute(f'SELECT * FROM {name}')]
                for name in names if name in existing}


def scan_companion(root):
    root = Path(root).resolve()
    cfg = source_config(root)
    persona = cfg.get('persona') or {}
    gateway = cfg.get('gateway') or {}
    if not isinstance(persona, dict) or not isinstance(gateway, dict):
        raise ValueError('旧 Persona 或 gateway 配置格式错误')
    warnings = []
    state_dir = str(cfg.get('state_dir') or 'state')
    persona_file = database_path(root, persona.get('db_path'),
        (f'{state_dir}/persona_state.db', 'state/persona_state.db', 'buckets/persona_state.db'), warnings)
    memo_file = database_path(root, cfg.get('reminder_db_path'),
        (f'{state_dir}/reminders.sqlite', 'state/reminders.sqlite'), warnings)
    tables = read_tables(persona_file, PERSONA_TABLES)
    if persona_file and not tables:
        raise ValueError('所选 Persona 状态库不含可识别的状态表')
    profiles = {row['profile_id'] for rows in tables.values() for row in rows}
    profile = str(persona.get('profile_id') or '')
    if not profile and len(profiles) > 1:
        raise ValueError('旧 Persona 有多个档案，请在备份 config.yaml 的 persona.profile_id 指定要迁入的档案')
    profile = profile or next(iter(profiles), '')
    if profiles and profile not in profiles:
        raise ValueError('配置的旧 Persona 档案不存在')
    tables = {name: [row for row in rows if row['profile_id'] == profile] for name, rows in tables.items()}
    if len(profiles) > 1:
        warnings.append('只迁入配置选中的 Persona 档案；其他档案保留在原备份。')
    memos = read_tables(memo_file, ('reminders',))
    if memo_file and not memos:
        raise ValueError('所选备忘库不含 reminders 表')
    tables.update(memos)
    if not persona_file:
        warnings.append('未找到 Persona 状态库，本次无心绪状态可迁入。')
    if not memo_file:
        warnings.append('未找到照顾备忘库，本次无备忘可迁入。')
    rounds = {}
    round_file = root / 'buckets/gateway_state.db'
    if any(tables.values()) and round_file.is_file() and round_file.resolve().is_relative_to(root):
        # Only aggregate counters; do not copy gateway conversation/debug logs.
        with closing(sqlite3.connect(round_file.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='request_rounds'").fetchone()
            if exists:
                rounds = dict(conn.execute('SELECT session_id, MAX(round_id) FROM request_rounds GROUP BY session_id'))
    default_session = str(gateway.get('default_session_id') or 'main').strip() or 'main'
    if not gateway.get('default_session_id') and any(
            row.get('session_id') not in (None, '', 'main') for rows in tables.values() for row in rows):
        warnings.append('旧配置未声明默认会话，按 main 处理；其他会话 ID 保留，客户端可填写原 ID 延续状态。')
    # Missing gateway history cannot justify resetting reminder progress.
    for row in tables.get('reminders', []):
        session = row.get('session_id') or default_session
        last = int(row.get('last_reminded_round') or 0)
        if last > int(rounds.get(session, 0)):
            rounds[session] = last
            warnings.append('部分会话缺少完整轮次记录，以最近提醒轮次作为下限继续计数。')
    result = {'tables': tables, 'rounds': rounds, 'profile': profile, 'default_session': default_session,
              'files': [str(p.relative_to(root)) for p in (persona_file, memo_file) if p]}
    # Stable ordering also makes a database VACUUM harmless to continuation.
    result['tables'] = {name: sorted(rows, key=encode) for name, rows in tables.items()}
    result['fingerprint'] = digest(encode(result))
    result['summary'] = {'rows': {name: len(rows) for name, rows in tables.items()},
                         'profile_mapping': {profile: 'default'} if profile else {},
                         'session_mapping': {default_session: 'main'},
                         'files': result['files'], 'warnings': list(dict.fromkeys(warnings))}
    return result


def import_companion(database, source, origin):
    from ..compat.persona_engine import PersonaStateEngine
    from ..compat.memo_store import ReminderStore
    PersonaStateEngine({'serein_database': database, 'persona': {'enabled': False}})
    ReminderStore({'serein_database': database})
    receipt_key = 'legacy_companion:' + digest(origin)
    def session(value):
        return 'main' if value == source['default_session'] else value
    with Store(database) as store, store.transaction(immediate=True):
        receipt = store.conn.execute('SELECT value_json FROM background_state WHERE name=?', (receipt_key,)).fetchone()
        if receipt:
            previous = json.loads(receipt[0])
            if previous['fingerprint'] != source['fingerprint']:
                raise ValueError('旧 Persona／备忘来源已改变，请使用原快照续跑；不会覆盖已导入状态')
            return {**previous, 'status': 'idempotent'}
        for table in PERSONA_TABLES:
            if source['tables'].get(table) and store.conn.execute(
                    f'SELECT 1 FROM {table} WHERE profile_id=? LIMIT 1', ('default',)).fetchone():
                raise ValueError('新实例已有 Persona 状态，未覆盖；请使用空实例迁移')
        counts = {}
        for table in TABLES:
            rows = source['tables'].get(table, [])
            columns = {row['name'] for row in store.conn.execute(f'PRAGMA table_info({table})')}
            for original in rows:
                row = {key: value for key, value in original.items() if key in columns}
                if table in PERSONA_TABLES:
                    row['profile_id'] = 'default'
                    if table in ('persona_events', 'persona_exchange_log'):
                        row.pop('id', None)  # Local history row IDs are not stable object IDs.
                if 'session_id' in row:
                    row['session_id'] = session(row['session_id'])
                if table == 'reminders' and store.conn.execute('SELECT 1 FROM reminders WHERE id=?', (row['id'],)).fetchone():
                    raise ValueError('备忘 ID 与新实例现有记录冲突，未覆盖；请使用空实例迁移')
                names = list(row)
                store.conn.execute(f'INSERT INTO {table} ({",".join(names)}) VALUES ({",".join("?" for _ in names)})',
                                   tuple(row[name] for name in names))
            counts[table] = len(rows)
        rounds = {}
        for key, value in source['rounds'].items():
            key = session(key)
            rounds[key] = max(rounds.get(key, 0), int(value))
        for key, value in rounds.items():
            name = 'feature_round:' + key
            existing = store.conn.execute('SELECT value_json FROM background_state WHERE name=?', (name,)).fetchone()
            if existing:
                raise ValueError('新实例已有会话轮次，未覆盖；请使用空实例迁移')
            store.conn.execute('INSERT INTO background_state(name,value_json) VALUES (?,?)', (name, encode(value)))
        receipt = {'status': 'imported', 'fingerprint': source['fingerprint'], 'rows': counts,
                   'profile_mapping': {source['profile']: 'default'} if source['profile'] else {},
                   'session_mapping': {source['default_session']: 'main'}, 'rounds': rounds}
        store.conn.execute('INSERT INTO background_state(name,value_json) VALUES (?,?)', (receipt_key, encode(receipt)))
        return receipt
