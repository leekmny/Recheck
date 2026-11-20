from __future__ import annotations
import json
import re
import sqlite3
import threading
import uuid
import zipfile
from contextlib import closing
import psutil
from datetime import datetime, timezone
from pathlib import Path
from .models import atomic_json, data_root
from .porting_history import apply_update, initial_rows


_PORTING_STORAGE = '_porting_storage'
_LATEST_RUN_UPDATE = (
    'MAX(runs.updated, COALESCE((SELECT updated FROM porting_updates '
    'WHERE run_id = runs.run_id ORDER BY sequence DESC LIMIT 1), runs.updated))'
)
_UPSERT_RUN = (
    'INSERT INTO runs VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET '
    'status=excluded.status,updated=excluded.updated,payload=excluded.payload'
)


def now():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return prefix + '_' + uuid.uuid4().hex


class Store:
    def __init__(self, root=None):
        self.root = Path(root or data_root())
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute(
                'CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, status TEXT NOT NULL, updated TEXT NOT NULL, payload TEXT NOT NULL)'
            )
            db.execute(
                'CREATE TABLE IF NOT EXISTS workflows (workflow_id TEXT PRIMARY KEY, status TEXT NOT NULL, updated TEXT NOT NULL, payload TEXT NOT NULL)'
            )
            db.execute(
                'CREATE TABLE IF NOT EXISTS porting_updates (sequence INTEGER PRIMARY KEY, '
                'run_id TEXT NOT NULL, scenario TEXT NOT NULL, updated TEXT NOT NULL, payload TEXT NOT NULL)'
            )
            db.execute(
                'CREATE INDEX IF NOT EXISTS porting_updates_run ON porting_updates(run_id, sequence)'
            )

    def connect(self):
        db = sqlite3.connect(self.root / 'history.sqlite3', timeout=10)
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def run_dir(self, run_id):
        if not re.fullmatch(r'run_[a-f0-9]{32}', run_id):
            raise ValueError('잘못된 run_id')
        path = self.root / 'runs' / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, run):
        with self.lock, closing(self.connect()) as db, db:
            if run.get('mode') == 'porting':
                record = db.execute('SELECT payload FROM runs WHERE run_id=?',
                                    (run['run_id'],)).fetchone()
                if record and json.loads(record[0]).get(_PORTING_STORAGE) == 1:
                    raise ValueError('이 실행은 포팅 시나리오 비교에서 관리됩니다.')
            run['updated'] = now()
            atomic_json(self.run_dir(run['run_id']) / 'result.json', run)
            db.execute(
                _UPSERT_RUN,
                (run['run_id'], run['status'], run['updated'],
                 json.dumps(run, ensure_ascii=False, default=str)),
            )

    def save_porting_state(self, run):
        """Commit only metadata and stage state. Observations live in the same DB."""
        updated = now()
        header = {key: value for key, value in run.items() if key not in ('scenarios', 'behavior')}
        header[_PORTING_STORAGE] = 1
        header['updated'] = updated
        with self.lock, closing(self.connect()) as db, db:
            db.execute(_UPSERT_RUN, (run['run_id'], run['status'], updated,
                                     json.dumps(header, ensure_ascii=False, default=str)))
        run['updated'] = updated

    def checkpoint_porting(self, run, row, **update):
        updated = now()
        payload = json.dumps(update, ensure_ascii=False, default=str)
        with self.lock, closing(self.connect()) as db, db:
            changed = db.execute(
                'INSERT INTO porting_updates(run_id,scenario,updated,payload) '
                'SELECT ?,?,?,? WHERE EXISTS (SELECT 1 FROM runs WHERE run_id=? AND status=?)',
                (run['run_id'], row['id'], updated, payload, run['run_id'], 'running'),
            )
            if changed.rowcount != 1:
                raise ValueError('실행 중인 비교 기록이 필요합니다.')
        apply_update(row, update)
        run['updated'] = updated

    @staticmethod
    def _restore_porting(db, run, updated, *, include_results=True):
        run.pop(_PORTING_STORAGE)
        run['updated'] = updated
        if not include_results:
            return run
        from .porting_policy import summarize

        run['scenarios'] = initial_rows(run['manifest'])
        rows = {row['id']: row for row in run['scenarios']}
        for scenario, payload in db.execute(
            'SELECT scenario,payload FROM porting_updates WHERE run_id=? ORDER BY sequence',
            (run['run_id'],),
        ):
            apply_update(rows[scenario], json.loads(payload))
        run['behavior'] = summarize(run['scenarios'])
        return run

    def load_run(self, run_id):
        uses_checkpoint_storage = False
        try:
            with closing(self.connect()) as db, db:
                db.execute('BEGIN')
                record = db.execute(f'SELECT {_LATEST_RUN_UPDATE},payload FROM runs WHERE run_id=?',
                                    (run_id,)).fetchone()
                if record:
                    updated, payload = record
                    run = json.loads(payload)
                    uses_checkpoint_storage = run.get(_PORTING_STORAGE) == 1
                    if uses_checkpoint_storage:
                        return self._restore_porting(db, run, updated)
        except sqlite3.Error as db_error:
            if uses_checkpoint_storage:
                raise
            try:
                return self._load_legacy_run(run_id)
            except ValueError:
                raise db_error from None
        return self._load_legacy_run(run_id)

    def _load_legacy_run(self, run_id):
        path = self.run_dir(run_id) / 'result.json'
        try:
            run = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(run, dict) or run.get('run_id') != run_id:
                raise ValueError('Result identity mismatch')
            return run
        except (OSError, ValueError):
            raise ValueError(
                '저장된 분석 결과를 읽을 수 없습니다. 기존 이력과 파일은 보존했습니다. '
                '이 분석을 다시 실행하거나 보관된 보고서를 확인하세요.'
            ) from None

    def list_run_summaries(self):
        return self.list_runs(include_porting_results=False)

    def list_runs(self, *, include_porting_results=True):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN')
            runs = []
            for updated, payload in db.execute(
                f'SELECT {_LATEST_RUN_UPDATE} AS recent,payload FROM runs ORDER BY recent DESC LIMIT 100'
            ):
                run = json.loads(payload)
                if run.get(_PORTING_STORAGE) == 1:
                    runs.append(self._restore_porting(
                        db, run, updated, include_results=include_porting_results,
                    ))
                    continue
                try:
                    self._load_legacy_run(run['run_id'])
                except ValueError as exc:
                    # Preserve the prior JSON and DB copies for manual recovery.
                    run['recorded_status'] = run['status']
                    run['status'] = 'partial'
                    run['storage_error'] = dict(code='result_unreadable', message=str(exc))
                runs.append(run)
        return runs

    def workflow_dir(self, workflow_id):
        if not re.fullmatch(r'workflow_[a-f0-9]{32}', workflow_id):
            raise ValueError('잘못된 workflow_id')
        path = self.root / 'workflows' / workflow_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_workflow(self, record):
        with self.lock:
            record['updated'] = now()
            atomic_json(self.workflow_dir(record['workflow_id']) / 'result.json', record)
            with self.connect() as db:
                db.execute(
                    'INSERT INTO workflows VALUES(?,?,?,?) ON CONFLICT(workflow_id) DO UPDATE SET status=excluded.status,updated=excluded.updated,payload=excluded.payload',
                    (
                        record['workflow_id'],
                        record['status'],
                        record['updated'],
                        json.dumps(record, ensure_ascii=False, default=str),
                    ),
                )

    def load_workflow(self, workflow_id):
        return json.loads(
            (self.workflow_dir(workflow_id) / 'result.json').read_text(encoding='utf-8')
        )

    def list_workflows(self):
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    'SELECT payload FROM workflows ORDER BY updated DESC LIMIT 100'
                )
            ]

    def event(self, run, kind, **payload):
        event = dict(
            event_id=new_id('event'),
            time=now(),
            run_id=run['run_id'],
            attempt_id=run['attempt_id'],
            kind=kind,
            **payload,
        )
        with self.lock:
            run.setdefault('events', []).append(event)
            path = self.run_dir(run['run_id'])
            with (path / 'events.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event, ensure_ascii=False, default=str) + '\n')
            with (path / 'recheck.log').open('a', encoding='utf-8') as stream:
                stream.write(
                    f"{event['time']} [{event['event_id']}] {kind} {json.dumps(payload, ensure_ascii=False, default=str)}\n"
                )
        return event

    def evidence(self, run, kind, target, data, *, tool='Recheck', source='', sha256=''):
        record = dict(
            evidence_id=new_id('ev'),
            run_id=run['run_id'],
            attempt_id=run['attempt_id'],
            kind=kind,
            target=target,
            tool=tool,
            sha256=sha256,
            source=source,
            created=now(),
            data=data,
        )
        run.setdefault('evidence', []).append(record)
        return record['evidence_id']

    @staticmethod
    def _mark_interrupted(run, reason):
        run['status'] = 'partial'
        for stage in run.get('stages', {}).values():
            if stage.get('status') == 'running':
                stage.update(status='failed', reason=reason)

    def interrupt_porting(self, run_id, reason, *, expected_updated=None):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            record = db.execute(
                'SELECT updated,payload FROM runs WHERE run_id=? AND status=?',
                (run_id, 'running'),
            ).fetchone()
            if not record or (expected_updated is not None and record[0] != expected_updated):
                return False
            run = json.loads(record[1])
            if run.get(_PORTING_STORAGE) != 1:
                return False
            self._mark_interrupted(run, reason)
            run['updated'] = now()
            run['recovery'] = dict(reason=reason)
            db.execute('UPDATE runs SET status=?,updated=?,payload=? WHERE run_id=?',
                       (run['status'], run['updated'], json.dumps(run, ensure_ascii=False), run_id))
        return True

    def interrupted(self):
        recovered = []
        reason = '이전 프로세스가 중단되었습니다. 재시도와 소유 자원 점검이 필요합니다'
        with closing(self.connect()) as db:
            candidates = db.execute(
                'SELECT updated,payload FROM runs WHERE status=?', ('running',)
            ).fetchall()
        for updated, payload in candidates:
            run = json.loads(payload)
            owner = run.get('owner_process', {})
            try:
                if owner.get('pid') and abs(
                    psutil.Process(owner['pid']).create_time() - owner.get('create_time', 0)
                ) < 0.01:
                    continue
            except psutil.AccessDenied:
                # A live owner may be running under a different account.
                continue
            except (psutil.Error, ValueError, TypeError):
                pass
            self._mark_interrupted(run, reason)
            if run.get(_PORTING_STORAGE) == 1:
                if not self.interrupt_porting(run['run_id'], reason, expected_updated=updated):
                    continue
            else:
                try:
                    self._load_legacy_run(run['run_id'])
                except ValueError:
                    continue
                self.event(
                    run, 'interrupted_recovery', reason='startup audit; no result reused as success'
                )
                self.save(run)
            recovered.append(run['run_id'])
        return recovered

    def diagnostic_zip(self, run_id, settings):
        run = self.load_run(run_id)
        # Target stdout, stdin, decompiled code and auxiliary data may contain arbitrary secrets;
        # share metadata/status only by default, not opaque content.
        # Scrub decoded values. Regex replacement on serialized JSON can corrupt
        # delimiters/escape sequences in real model metadata and nested logs.
        if run.get('mode') == 'porting':
            # The checkpoint data can contain arbitrary runtime values, arguments
            # and worker configuration. Only share the observation counts.
            fields = ('run_id', 'attempt_id', 'schema_version', 'mode', 'created',
                      'updated', 'status', 'stages', 'behavior')
            run = {key: run[key] for key in fields if key in run}
        masked = dict(run=run, settings=settings)
        sensitive = {
            'stdout',
            'stderr',
            'stdout_b64',
            'stderr_b64',
            'stdin',
            'env',
            'decompile',
            'decompiled',
            'decompiled_code',
            'args',
            'content',
            'prompt',
            'messages',
            'raw_response',
            'claim',
            'rationale',
            'summary',
            'description',
            'question',
            'answer',
            'error',
            'message',
            'reason',
            'validated_text',
            'normalized_text',
        }

        def scrub(value):
            if isinstance(value, dict):
                return {
                    k: (
                        '<MASKED>'
                        if re.search(
                            r'(?i)(api[_-]?key|password|secret|authorization|access[_-]?token|refresh[_-]?token)',
                            k,
                        )
                        or k.lower() == 'token'
                        else '<CONTENT OMITTED>' if k in sensitive else scrub(v)
                    )
                    for k, v in value.items()
                    if k
                    not in (
                        'functions',
                        'data',
                        'events',
                        'history',
                        'logs',
                        'process',
                        'response',
                        'responses',
                        'context',
                        'interpretation',
                        'function_candidates',
                        'findings',
                        'suggested_tests',
                        'model_metadata',
                        'metadata',
                        'license',
                        'template',
                        'parameters',
                        'citation_completions',
                    )
                }
            if isinstance(value, list):
                return [scrub(x) for x in value]
            if isinstance(value, str):
                value = re.sub(r'(?i)([A-Z]:[\\/]+Users[\\/]+)[^\\/"\s]+', r'\1<USER>', value)
                return re.sub(
                    r'(?i)\b(api[_-]?key|password|token|secret)(\s*[:=]\s*)[^\s,;]+',
                    r'\1\2<MASKED>',
                    value,
                )
            return value

        path = self.run_dir(run_id) / 'diagnostics-masked.zip'
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                'diagnostics.json', json.dumps(scrub(masked), ensure_ascii=False, indent=2)
            )
            event_fields = {'event_id', 'time', 'kind', 'stage', 'status', 'target', 'code'}
            archive.writestr(
                'events-summary.jsonl',
                '\n'.join(
                    json.dumps(
                        scrub({k: v for k, v in event.items() if k in event_fields}),
                        ensure_ascii=False,
                    )
                    for event in run.get('events', [])
                ),
            )
            archive.writestr(
                'README.txt',
                '공유용: 입력/출력/디컴파일/프롬프트/원문 로그를 제외한 상태와 설정입니다. 공유 전 검토하세요.',
            )
        return path
