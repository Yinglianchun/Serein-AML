"""Import notebook and historical works independently of model-based migration."""
import base64
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
import yaml

from ..core.store import Store, digest, encode
from ..compat.diaries import Diaries, project_diaries
from ..ingest.legacy_archive import import_archives
from .companion import source_config, database_path, read_tables
from .darkroom import room_groups, import_darkroom, unlock_time

TABLES = ('diaries', 'comments', 'diary_revisions', 'darkroom_sessions')


def scan_history(root):
    root = Path(root).resolve()
    cfg = source_config(root)
    warnings = []
    state = str(cfg.get('state_dir') or 'state')
    diary_cfg = cfg.get('diary') or {}
    dream_cfg = cfg.get('dream') or {}
    if not isinstance(diary_cfg, dict) or not isinstance(dream_cfg, dict):
        raise ValueError('旧 diary / dream 配置必须是对象')
    diary = database_path(root, diary_cfg.get('db_path'),
                          (f'{state}/diary.db', 'state/diary.db'), warnings)
    shadow = database_path(root, None,
                           (f'{state}/window_shadows.sqlite', 'state/window_shadows.sqlite'), warnings)
    tables = read_tables(diary, TABLES)
    if diary and 'diaries' not in tables:
        raise ValueError('旧日记库缺少 diaries 表')
    shadows = read_tables(shadow, ('window_shadows',))
    if shadow and 'window_shadows' not in shadows:
        raise ValueError('旧窗影库缺少 window_shadows 表')

    def directory(configured, defaults):
        if configured:
            path = (root / str(configured)).resolve()
            if path.is_relative_to(root) and path.is_dir():
                return path
            warnings.append('配置中的历史目录不在所选备份内；仅检查备份内标准位置。')
        paths = {(root / name).resolve() for name in defaults
                 if (root / name).is_dir() and (root / name).resolve().is_relative_to(root)}
        if len(paths) > 1:
            raise ValueError('备份含多个历史目录，请在 config.yaml 中明确相对路径')
        return next(iter(paths), None)

    dreams = directory(dream_cfg.get('data_dir'), (f'{state}/dreams', 'state/dreams'))
    darkroom = directory(None, (f'{state}/darkroom', 'state/darkroom'))
    files = []
    def capture(path, name):
        if not path.is_file():
            return
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('历史文件不能超出所选备份目录或使用符号链接')
        raw = path.read_bytes()
        files.append({'path': name, 'sha256': digest(raw),
                      'bytes_b64': base64.b64encode(raw).decode('ascii')})
    if dreams:
        for path in sorted(dreams.glob('dream_*.md')):
            capture(path, 'dreams/' + path.name)
        capture(dreams / 'logs/events.jsonl', 'dreams/logs/events.jsonl')
    if darkroom:
        for name in ('entries.jsonl', 'state.json', 'releases.jsonl'):
            capture(darkroom / name, 'darkroom/' + name)
    data = {'tables': {name: sorted(tables.get(name, []), key=encode) for name in TABLES},
            'window_shadows': sorted(shadows.get('window_shadows', []), key=encode), 'files': files}
    fingerprint = digest(encode(data))
    if not diary and not dreams and not darkroom and not shadow:
        warnings.append('未找到历史数据目录；如旧版有梦境或暗房，请提供包含 state 的完整备份。')
    source_ids = {r.get('source_id') for r in tables.get('diaries', [])}
    groups = room_groups(files)
    new_rooms = [rows for rows in groups.values() if not any('legacy_darkroom:' + r['id'] in source_ids for r in rows)]
    return {**data, 'fingerprint': fingerprint,
            'summary': {'diaries': sum(r['entry_type'] != 'darkroom' for r in tables.get('diaries', [])),
                        'darkroom': sum(r['entry_type'] == 'darkroom' for r in tables.get('diaries', [])) + len(new_rooms),
                        'comments': len(tables.get('comments', [])),
                        'revisions': len(tables.get('diary_revisions', [])) + sum(len(rows)-1 for rows in new_rooms),
                        'sessions': len(tables.get('darkroom_sessions', [])) + sum(bool(unlock_time(rows[-1])) and (rows[-1].get('visibility') or 'active')=='active' for rows in new_rooms),
                        'dreams': sum(f['path'].endswith('.md') for f in files),
                        'shadows': len(data['window_shadows']), 'legacy_darkroom': sum(map(len, groups.values())),
                        'json_darkroom_rooms': len(new_rooms),
                        'warnings': warnings}}


def import_history(database, source, source_key):
    """One transaction includes original snapshots, ID maps, projections and receipt."""
    Diaries(database, initialize=True)
    receipt_key = 'legacy-history:' + source_key
    origin = receipt_key
    if not any(source['tables'].values()) and not source['window_shadows'] and not source['files']:
        return {'fingerprint': source['fingerprint'], 'summary': source['summary'], 'id_maps': {},
                'archives': {}, 'held': [], 'repeated': False, 'empty': True}
    with Store(database) as store, store.transaction(immediate=True):
        prior = store.conn.execute('SELECT value_json FROM background_state WHERE name=?', (receipt_key,)).fetchone()
        if prior:
            result = json.loads(prior[0])
            if result['fingerprint'] != source['fingerprint']:
                raise ValueError('此来源的历史数据已变化；请使用原快照补漏，不能覆盖已迁入或后来编辑的内容')
            return {**result, 'repeated': True}
        store.save_import_record(origin, 'history-source.json', encode(source).encode('utf-8'))
        maps = {name: {} for name in TABLES}
        held = []
        projections = {'diaries': 'diary_entries', 'comments': 'diary_comments',
                       'diary_revisions': 'diary_history', 'darkroom_sessions': 'diary_sessions'}
        for table in TABLES:
            used = {r[0] for r in store.conn.execute('SELECT id FROM ' + table)}
            used.update(r[0] for r in store.conn.execute('SELECT id FROM ' + projections[table]))
            old_ids = [r['id'] for r in source['tables'][table]]
            if len(set(old_ids)) != len(old_ids) or any(type(key) is not int or key < 1 for key in old_ids):
                raise ValueError('旧日记表包含重复或无效 ID：' + table)
            next_id = max([0, *used, *old_ids]) + 1
            for key in old_ids:
                target = key
                if target in used:
                    target = next_id
                    next_id += 1
                used.add(target)
                maps[table][str(key)] = target

        for table in TABLES:
            allowed = {r[1] for r in store.conn.execute('PRAGMA table_info(' + table + ')')}
            for original in source['tables'][table]:
                row = {k: v for k, v in original.items() if k in allowed}
                row['id'] = maps[table][str(original['id'])]
                if table == 'diaries':
                    row['source_id'] = row.get('source_id') or f'{origin}:diary:{original["id"]}'
                    if store.conn.execute('SELECT 1 FROM diaries WHERE source_id=?', (row['source_id'],)).fetchone():
                        raise ValueError('已有同来源日记但没有本批补漏收据，未覆盖；请先核对：' + row['source_id'])
                elif row.get('diary_id') is not None:
                    target = maps['diaries'].get(str(row['diary_id']))
                    if target is None:
                        held.append({'table': table, 'id': original['id'], 'reason': '原日记缺失，原记录保留在历史快照中，未挂到其他日记'})
                        continue
                    row['diary_id'] = target
                columns = sorted(row)
                store.conn.execute('INSERT INTO ' + table + ' (' + ','.join(columns) + ') VALUES (' +
                                   ','.join('?' for _ in columns) + ')', [row[k] for k in columns])
        archive_files = [f for f in source['files'] if f['path'].startswith('dreams/')]
        work_ids = [r['window_id'] for r in source['window_shadows']]
        for file in archive_files:
            raw = base64.b64decode(file['bytes_b64'], validate=True)
            if file['path'].endswith('.md'):
                match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', raw.decode('utf-8'), re.S)
                meta = yaml.safe_load(match[1]) if match else None
                key = meta.get('dream_id') if isinstance(meta, dict) else None
                if not isinstance(key, str) or not key.startswith('dream_'):
                    raise ValueError('梦境缺少有效 dream_id 或原始元数据')
                work_ids.append(key)
            else:
                work_ids.extend(json.loads(line).get('dream_id') for line in raw.splitlines() if line.strip())
        for key in work_ids:
            if key and store.conn.execute('SELECT 1 FROM documents WHERE id=?', (key,)).fetchone():
                raise ValueError('历史作品 ID 与现有记忆冲突，未覆盖：' + str(key))
        archive = {'format': 'serein-archive-snapshot-v1', 'origin': origin,
                   'captured_at': '', 'window_shadows': source['window_shadows'], 'files': archive_files}
        archives = import_archives(store, archive)
        for file in source['files']:
            if not file['path'].startswith('darkroom/'):
                continue
            raw = base64.b64decode(file['bytes_b64'], validate=True)
            if digest(raw) != file['sha256']:
                raise ValueError('旧暗房文件校验失败')
            store.save_import_record(origin, file['path'], raw)
        darkroom_result = import_darkroom(store, source['files'])
        held.extend(darkroom_result['held'])
        maps['legacy_darkroom'] = darkroom_result['id_map']
        project_diaries(store.conn)
        result = {'fingerprint': source['fingerprint'], 'summary': source['summary'],
                  'id_maps': maps, 'archives': archives, 'darkroom': darkroom_result, 'held': held, 'repeated': False}
        store.conn.execute('INSERT INTO background_state(name,value_json) VALUES (?,?)', (receipt_key, encode(result)))
        return result


def run_history_repair(settings, plan):
    """Back up and supplement only history; never invokes models or Scene import."""
    source = scan_history(plan['root'])
    if source['fingerprint'] != plan['history']['fingerprint']:
        raise ValueError('预览后历史数据发生变化，请停止旧服务写入并重新预览')
    folder = settings.database.parent / 'migrations/history-repair' / uuid4().hex
    folder.mkdir(parents=True)
    snapshot = folder / 'before-history-repair.db'
    with closing(sqlite3.connect(settings.database.resolve().as_uri() + '?mode=ro', uri=True)) as src, \
            closing(sqlite3.connect(snapshot)) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('历史补漏前备份未通过完整性检查')
    result = import_history(settings.database, source, plan['fingerprint'])
    from .originals import scan_originals, import_originals
    originals=scan_originals(plan['root'])
    if 'originals' not in plan or originals['fingerprint']!=plan['originals']['fingerprint']:
        raise ValueError('请重新预览停止写入后的完整备份，以检查旧原文库')
    from .dates import repair_dates
    from .comments import import_comments
    result={**result,'originals':import_originals(settings.database,originals),'dates':repair_dates(settings.database,plan),'memory_comments':import_comments(settings.database,plan)}
    report = folder / 'report.json'
    report.write_text(encode(result), 'utf-8')
    return {**result, 'backup': str(snapshot), 'report': str(report)}
