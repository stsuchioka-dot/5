"""Gate5 local console application. Python 3.11+; standard library only."""
import argparse
import json
import secrets
import sqlite3
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
DEFAULT_DB = Path(__file__).with_name('lending.sqlite3')

class Rejected(Exception):
    pass

class LendingService:
    def __init__(self, path=DEFAULT_DB, fault=None):
        self.path = str(path)
        self.fault = fault or (lambda stage: None)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def initialize(self):
        with closing(self.connect()) as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS employees(
              id INTEGER PRIMARY KEY, name TEXT NOT NULL,
              employment_status TEXT NOT NULL CHECK(employment_status IN ('ACTIVE','LEAVE','RETIRED')));
            CREATE TABLE IF NOT EXISTS devices(
              id INTEGER PRIMARY KEY, asset_no TEXT UNIQUE NOT NULL, model_name TEXT NOT NULL,
              device_type TEXT NOT NULL CHECK(device_type IN ('LAPTOP','TABLET','MONITOR')),
              status TEXT NOT NULL CHECK(status IN ('AVAILABLE','LENT','REPAIR','DISPOSED')));
            CREATE TABLE IF NOT EXISTS lendings(
              id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL REFERENCES devices(id),
              user_id INTEGER NOT NULL REFERENCES employees(id), lent_at TEXT NOT NULL,
              due_date TEXT NOT NULL, returned_at TEXT,
              purpose TEXT NOT NULL CHECK(length(purpose) BETWEEN 1 AND 100),
              CHECK(returned_at IS NULL OR returned_at >= lent_at));
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_device ON lendings(device_id) WHERE returned_at IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_user ON lendings(user_id) WHERE returned_at IS NULL;
            CREATE TABLE IF NOT EXISTS confirmations(
              token TEXT PRIMARY KEY, actor INTEGER NOT NULL REFERENCES employees(id),
              kind TEXT NOT NULL CHECK(kind IN ('lend','return')), payload TEXT NOT NULL,
              created_at TEXT NOT NULL, result TEXT);
            ''')

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            # SQLite serializes all writers, including across independent processes.
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def seed_demo(self):
        with self.transaction() as db:
            if db.execute('SELECT count(*) FROM employees').fetchone()[0]:
                return
            db.executemany('INSERT INTO employees VALUES(?,?,?)', [(1,'社員A','ACTIVE'),(2,'社員B','ACTIVE'),(3,'休職者','LEAVE')])
            db.executemany('INSERT INTO devices VALUES(?,?,?,?,?)',[(1,'PC-0001','共用PC A','LAPTOP','AVAILABLE'),(2,'PC-0002','共用PC B','LAPTOP','AVAILABLE'),(3,'PC-0003','修理PC','LAPTOP','REPAIR')])

    def employee(self, db, actor, active=False):
        e = db.execute('SELECT * FROM employees WHERE id=?',(actor,)).fetchone()
        if not e:
            raise Rejected('社員情報を確認できません。')
        if active and e['employment_status'] != 'ACTIVE':
            raise Rejected('現在の社員状態では新規貸出できません。')
        return e

    def inputs(self, due, purpose):
        if not isinstance(purpose,str):
            raise Rejected('利用目的を入力してください。')
        purpose = purpose.strip()
        if not 1 <= len(purpose) <= 100:
            raise Rejected('利用目的は1〜100文字で入力してください。')
        if any(ord(c)<32 or 127<=ord(c)<160 for c in purpose):
            raise Rejected('利用目的に制御文字は使用できません。')
        try:
            day = datetime.strptime(due,'%Y-%m-%d').date()
            if day.isoformat() != due:
                raise ValueError()
        except (ValueError,TypeError):
            raise Rejected('有効な日付（YYYY-MM-DD）を入力してください。')
        today = datetime.now(JST).date()
        if not today+timedelta(days=1) <= day <= today+timedelta(days=90):
            raise Rejected('返却予定日は明日から90日以内で入力してください。')
        return purpose

    def check_lend(self, db, actor, device_id):
        self.employee(db,actor,True)
        d = db.execute('SELECT * FROM devices WHERE id=?',(device_id,)).fetchone()
        if not d:
            raise Rejected('対象の端末が見つかりません。')
        count = db.execute('SELECT count(*) FROM lendings WHERE device_id=? AND returned_at IS NULL',(device_id,)).fetchone()[0]
        if count != (1 if d['status']=='LENT' else 0):
            raise Rejected('貸出情報と端末状態に不整合があります。管理者にお問い合わせください。')
        if d['status']=='LENT':
            raise Rejected('この端末は現在貸出中です。別の端末を選択してください。')
        if d['status']!='AVAILABLE' or d['device_type']!='LAPTOP':
            raise Rejected('この端末は貸出対象外、修理中または廃棄済みです。')
        own = db.execute('SELECT due_date FROM lendings WHERE user_id=? AND returned_at IS NULL',(actor,)).fetchone()
        if own:
            if own['due_date'] < datetime.now(JST).date().isoformat():
                raise Rejected('返却期限を過ぎています。先に返却してください。')
            raise Rejected('貸出は1人1台までです。現在のPCを返却してください。')
        return d

    def prepare_lend(self, actor, device_id, due, purpose):
        with self.transaction() as db:
            purpose = self.inputs(due,purpose)
            d = self.check_lend(db,actor,device_id)
            payload = dict(device_id=device_id,due_date=due,purpose=purpose,asset_no=d['asset_no'])
            return self.save_confirmation(db,actor,'lend',payload)

    def prepare_return(self, actor, lending_id):
        with self.transaction() as db:
            self.employee(db,actor)
            l = db.execute('SELECT * FROM lendings WHERE id=? AND user_id=?',(lending_id,actor)).fetchone()
            if not l:
                raise Rejected('本人の返却対象が見つかりません。')
            return self.save_confirmation(db,actor,'return',dict(lending_id=lending_id))

    def save_confirmation(self, db, actor, kind, payload):
        token = secrets.token_urlsafe(24)
        db.execute('INSERT INTO confirmations VALUES(?,?,?,?,?,NULL)',(token,actor,kind,json.dumps(payload,ensure_ascii=False),datetime.now(JST).isoformat()))
        return token,payload

    def result(self, actor, token):
        with closing(self.connect()) as db:
            self.employee(db,actor)
            row = db.execute('SELECT result FROM confirmations WHERE token=? AND actor=?',(token,actor)).fetchone()
            if not row:
                raise Rejected('本人の確認情報が見つかりません。')
            return json.loads(row['result']) if row['result'] else None

    def confirm(self, actor, token):
        # No user-supplied payload is accepted here: use persisted confirmed content.
        with self.transaction() as db:
            self.employee(db,actor)
            row = db.execute('SELECT * FROM confirmations WHERE token=? AND actor=?',(token,actor)).fetchone()
            if not row:
                raise Rejected('本人の確認情報が無効です。申請から操作してください。')
            if row['result']:
                return json.loads(row['result'])
            p = json.loads(row['payload'])
            now = datetime.now(JST).isoformat(timespec='microseconds')
            if row['kind']=='lend':
                self.inputs(p['due_date'],p['purpose'])
                d = self.check_lend(db,actor,p['device_id'])
                cur = db.execute('INSERT INTO lendings(device_id,user_id,lent_at,due_date,purpose) VALUES(?,?,?,?,?)',(p['device_id'],actor,now,p['due_date'],p['purpose']))
                self.fault('after_lending_insert')
                db.execute("UPDATE devices SET status='LENT' WHERE id=?",(p['device_id'],))
                result = dict(message=f"{d['asset_no']} を貸し出しました。返却予定日は {p['due_date']} です。",lending_id=cur.lastrowid)
            else:
                l = db.execute('SELECT * FROM lendings WHERE id=? AND user_id=?',(p['lending_id'],actor)).fetchone()
                if not l:
                    raise Rejected('本人の返却対象が見つかりません。')
                if l['returned_at']:
                    result = dict(message='この貸出は返却済みです。',lending_id=l['id'],returned_at=l['returned_at'])
                else:
                    d = db.execute('SELECT * FROM devices WHERE id=?',(l['device_id'],)).fetchone()
                    if not d or d['status']!='LENT':
                        raise Rejected('貸出情報と端末状態に不整合があります。管理者にお問い合わせください。')
                    if now < l['lent_at']:
                        raise Rejected('サーバー時刻に不整合があります。管理者にお問い合わせください。')
                    db.execute('UPDATE lendings SET returned_at=? WHERE id=?',(now,l['id']))
                    self.fault('after_return_update')
                    db.execute("UPDATE devices SET status='AVAILABLE' WHERE id=?",(l['device_id'],))
                    result = dict(message=f"{d['asset_no']} を返却しました。",lending_id=l['id'],returned_at=now)
            db.execute('UPDATE confirmations SET result=? WHERE token=?',(json.dumps(result,ensure_ascii=False),token))
            self.fault('before_commit')
        return result

    def lists(self, actor):
        with closing(self.connect()) as db:
            self.employee(db,actor)
            devices = [dict(r) for r in db.execute("SELECT id,asset_no,model_name FROM devices WHERE status='AVAILABLE' AND device_type='LAPTOP'")]
            own = [dict(r) for r in db.execute('SELECT l.id,d.asset_no,l.due_date FROM lendings l JOIN devices d ON d.id=l.device_id WHERE l.user_id=? AND l.returned_at IS NULL',(actor,))]
            return devices,own

def main():
    parser = argparse.ArgumentParser(description='Gate5 PC貸出：ローカル学習用コンソール')
    parser.add_argument('--db',default=str(DEFAULT_DB)); args=parser.parse_args()
    svc=LendingService(args.db);svc.initialize();svc.seed_demo()
    print('PC貸出管理（学習用）\nデモ社員：1=社員A、2=社員B、3=休職者。実際の認証機能は未実装です。')
    try: actor=int(input('デモ操作社員ID: '))
    except ValueError: return
    print('コマンド：list / lend / return / confirm / result / quit')
    while True:
        token=None
        try:
            cmd=input('> ').strip()
            if cmd=='quit':break
            if cmd=='list':
                devices,own=svc.lists(actor)
                print('貸出可能PC:',json.dumps(devices,ensure_ascii=False,indent=2) if devices else '現在、貸出可能なPCはありません。')
                print('自分の未返却:',json.dumps(own,ensure_ascii=False,indent=2));continue
            if cmd=='lend':
                device=int(input('端末ID: '));due=input('返却予定日 YYYY-MM-DD: ');purpose=input('利用目的: ')
                token,payload=svc.prepare_lend(actor,device,due,purpose)
            elif cmd=='return':token,payload=svc.prepare_return(actor,int(input('貸出ID: ')))
            elif cmd=='confirm':token=input('確認・操作ID: ').strip();print(svc.confirm(actor,token)['message']);continue
            elif cmd=='result':
                token=input('確認・操作ID: ').strip();r=svc.result(actor,token)
                print(r['message'] if r else '保存済み完了結果はありません。同じ操作IDで確定を再試行できます。');continue
            else:print('list / lend / return / confirm / result / quit');continue
            print('確認内容:',json.dumps(payload,ensure_ascii=False));print('確認・操作ID（再送／照会用）:',token)
            if input('確定しますか？ yes: ').strip()=='yes':print(svc.confirm(actor,token)['message'])
            else:print('未確定です。貸出・返却は更新していません。')
        except Rejected as e:print('エラー:',e)
        except ValueError:print('エラー: IDは整数で入力してください。')
        except sqlite3.Error:
            print('DB処理を完了できませんでした。再申請せず、同じ操作IDで結果を照会してください。',token or '')
        except (EOFError,KeyboardInterrupt):print('\n終了します。');break

if __name__=='__main__':main()

