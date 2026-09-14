"""Additional checklist audit, using isolated temporary databases."""
import sqlite3
import unittest
from datetime import datetime,timedelta
from unittest.mock import patch
import test_app
from app import LendingService,Rejected,JST

class ChecklistTests(unittest.TestCase):
    def test_out_of_range_ids_are_rejected_without_update(self):
        for value in [0,-1,9223372036854775808,10**100,True,1.5,'1']:
            with self.subTest(value=value):
                with self.assertRaises(Rejected):self.s.prepare_lend(1,value,self.due,'用途')
                with self.assertRaises(Rejected):self.s.prepare_return(1,value)
                with self.assertRaises(Rejected):self.s.lists(value)
        self.assertEqual(self.query('SELECT count(*) FROM lendings'),[(0,)])

    setUp=test_app.Tests.setUp
    tearDown=test_app.Tests.tearDown
    prep=test_app.Tests.prep
    query=test_app.Tests.query

    def test_unavailable_and_wrong_type(self):
        for state,kind in [('REPAIR','LAPTOP'),('DISPOSED','LAPTOP'),('AVAILABLE','TABLET'),('AVAILABLE','MONITOR')]:
            with self.subTest(state=state,kind=kind):
                with self.s.transaction() as db:db.execute('UPDATE devices SET status=?, device_type=? WHERE id=1',(state,kind))
                with self.assertRaises(Rejected):self.prep()

    def test_missing_device_and_employee(self):
        for actor,device in [(1,999),(999,1),(1,-1),(1,0)]:
            with self.subTest(actor=actor,device=device):
                with self.assertRaises(Rejected):self.prep(actor,device)

    def test_zero_inventory(self):
        with self.s.transaction() as db:db.execute("UPDATE devices SET status='REPAIR'")
        self.assertEqual(self.s.lists(1)[0],[])

    def test_retired_and_leave(self):
        for state in ['RETIRED','LEAVE']:
            with self.s.transaction() as db:db.execute('UPDATE employees SET employment_status=? WHERE id=1',(state,))
            with self.assertRaises(Rejected):self.prep()

    def test_exact_boundaries_and_special_characters(self):
        today=datetime.now(JST).date()
        for days in [1,90]:
            for purpose in ['a','あ'*100,'😀','髙①','<script>alert(1)</script>']:
                with self.subTest(days=days,purpose=purpose):
                    token,_=self.s.prepare_lend(1,1,(today+timedelta(days=days)).isoformat(),purpose)
                    self.s.confirm(1,token)
                    self.assertEqual(self.query('SELECT purpose FROM lendings ORDER BY id DESC LIMIT 1'),[(purpose,)])
                    loan=self.query('SELECT id FROM lendings ORDER BY id DESC LIMIT 1')[0][0]
                    self.s.confirm(1,self.s.prepare_return(1,loan)[0])

    def test_unborrowed_return_and_invalid_confirmation(self):
        with self.assertRaises(Rejected):self.s.prepare_return(1,999)
        with self.assertRaises(Rejected):self.s.confirm(1,'invented-token')

    def test_payload_changed_outside_service_does_not_change_saved_content(self):
        token,payload=self.s.prepare_lend(1,1,self.due,'会議')
        payload['device_id']=2;payload['purpose']='変更'
        self.s.confirm(1,token)
        self.assertEqual(self.query('SELECT device_id,purpose FROM lendings'),[(1,'会議')])

    def test_overdue_rejects_new_but_allows_return(self):
        loan=self.s.confirm(1,self.prep())['lending_id']
        with self.s.transaction() as db:db.execute('UPDATE lendings SET due_date=?',((datetime.now(JST).date()-timedelta(days=1)).isoformat(),))
        with self.assertRaisesRegex(Rejected,'返却期限'):self.prep(1,2)
        self.s.confirm(1,self.s.prepare_return(1,loan)[0])
        self.assertEqual(self.query('SELECT status FROM devices WHERE id=1'),[('AVAILABLE',)])

    def test_midnight_rechecks_confirmation(self):
        today=datetime.now(JST)
        token,_=self.s.prepare_lend(1,1,(today.date()+timedelta(days=1)).isoformat(),'会議')
        with patch('app.datetime') as clock:
            clock.now.return_value=today+timedelta(days=1)
            clock.strptime.side_effect=datetime.strptime
            with self.assertRaisesRegex(Rejected,'90日'):self.s.confirm(1,token)
        self.assertEqual(self.query('SELECT count(*) FROM lendings'),[(0,)])

    def test_real_sqlite_lock_timeout_leaves_no_updates(self):
        token=self.prep()
        original=self.s.connect
        def impatient():
            db=original();db.execute('PRAGMA busy_timeout=20');return db
        with self.s.transaction():
            with patch.object(self.s,'connect',side_effect=impatient):
                with self.assertRaises(sqlite3.OperationalError):self.s.confirm(1,token)
        self.assertEqual(self.query('SELECT count(*) FROM lendings'),[(0,)])
        self.assertIsNone(self.s.result(1,token))
        self.s.confirm(1,token)

if __name__=='__main__':
    unittest.main(verbosity=2)
