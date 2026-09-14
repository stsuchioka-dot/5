import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
from pathlib import Path
from app import LendingService,Rejected,JST

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'test.db'
        self.s=LendingService(self.path);self.s.initialize();self.s.seed_demo()
        self.due=(datetime.now(JST).date()+timedelta(days=7)).isoformat()
    def tearDown(self):self.tmp.cleanup()
    def prep(self,actor=1,device=1):return self.s.prepare_lend(actor,device,self.due,'業務用')[0]
    def query(self,sql):
        db=self.s.connect()
        try:return [tuple(r) for r in db.execute(sql)]
        finally:db.close()
    def race(self,items):
        def run(item):
            try:return LendingService(self.path).confirm(*item)
            except Rejected:return None
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(run,items))
    def test_same_device_two_employees(self):
        a,b=self.prep(),self.prep(2)
        self.assertEqual(sum(x is not None for x in self.race([(1,a),(2,b)])),1)
        self.assertEqual(self.query('SELECT count(*) FROM lendings'),[(1,)])
    def test_same_employee_two_devices(self):
        a,b=self.prep(),self.prep(1,2)
        self.assertEqual(sum(x is not None for x in self.race([(1,a),(1,b)])),1)
    def test_double_submit_and_result_recovery(self):
        t=self.prep();results=self.race([(1,t),(1,t)])
        self.assertEqual(results[0],results[1]);self.assertEqual(self.s.result(1,t),results[0])
        self.assertEqual(self.query('SELECT count(*) FROM lendings'),[(1,)])
    def test_lend_rollback(self):
        t=self.prep()
        def fail(stage):
            if stage=='after_lending_insert':raise RuntimeError('injected')
        with self.assertRaises(RuntimeError):LendingService(self.path,fail).confirm(1,t)
        self.assertEqual(self.query('SELECT count(*) FROM lendings'),[(0,)])
        self.assertEqual(self.query('SELECT status FROM devices WHERE id=1'),[('AVAILABLE',)])
        self.assertIsNone(self.s.result(1,t));self.s.confirm(1,t)
    def test_return_rollback(self):
        l=self.s.confirm(1,self.prep())['lending_id'];t=self.s.prepare_return(1,l)[0]
        def fail(stage):
            if stage=='after_return_update':raise RuntimeError('injected')
        with self.assertRaises(RuntimeError):LendingService(self.path,fail).confirm(1,t)
        self.assertEqual(self.query('SELECT returned_at FROM lendings'),[(None,)])
        self.assertEqual(self.query('SELECT status FROM devices WHERE id=1'),[('LENT',)])
    def test_failure_before_commit_rolls_back_result_too(self):
        t=self.prep()
        def fail(stage):
            if stage=='before_commit':raise RuntimeError('injected')
        with self.assertRaises(RuntimeError):LendingService(self.path,fail).confirm(1,t)
        self.assertIsNone(self.s.result(1,t));self.assertEqual(self.query('SELECT count(*) FROM lendings'),[(0,)])
    def test_old_return_does_not_affect_new_loan(self):
        l=self.s.confirm(1,self.prep())['lending_id']
        a,b=self.s.prepare_return(1,l)[0],self.s.prepare_return(1,l)[0]
        self.s.confirm(1,a);new=self.s.confirm(2,self.prep(2))
        self.s.confirm(1,b);self.s.confirm(1,a)
        self.assertEqual(self.query('SELECT status FROM devices WHERE id=1'),[('LENT',)])
        self.assertEqual(self.query('SELECT user_id FROM lendings WHERE returned_at IS NULL'),[(2,)])
    def test_other_employee_cannot_confirm_or_read(self):
        t=self.prep()
        with self.assertRaises(Rejected):self.s.confirm(2,t)
        with self.assertRaises(Rejected):self.s.result(2,t)
        l=self.s.confirm(1,t)['lending_id']
        with self.assertRaises(Rejected):self.s.prepare_return(2,l)
    def test_employee_change_after_confirmation(self):
        t=self.prep()
        with self.s.transaction() as db:db.execute("UPDATE employees SET employment_status='LEAVE' WHERE id=1")
        with self.assertRaises(Rejected):self.s.confirm(1,t)
    def test_inconsistent_device(self):
        with self.s.transaction() as db:db.execute("UPDATE devices SET status='LENT' WHERE id=1")
        with self.assertRaises(Rejected):self.prep()
    def test_input_boundaries(self):
        for purpose in ['', '　 ', 'a'*101,'x\ny']:
            with self.assertRaises(Rejected):self.s.prepare_lend(1,1,self.due,purpose)
        self.s.prepare_lend(1,1,self.due,'😀'*100)
        for due in ['2026-02-30',datetime.now(JST).date().isoformat(),(datetime.now(JST).date()+timedelta(days=91)).isoformat()]:
            with self.assertRaises(Rejected):self.s.prepare_lend(1,1,due,'会議')
    def test_db_unique_constraint(self):
        self.s.confirm(1,self.prep())
        with self.assertRaises(sqlite3.IntegrityError):
            with self.s.transaction() as db:db.execute("INSERT INTO lendings(device_id,user_id,lent_at,due_date,purpose) VALUES(1,2,'2026-09-14','2026-09-20','test')")

if __name__=='__main__':unittest.main(verbosity=2)
