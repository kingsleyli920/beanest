"""Exact owned stdio errors stay failed, with no prompt replay or private text."""
import json
import unittest
from tests import test_kimi_acp as fixture


class KimiAuthDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.KimiACPTests.asyncSetUp
    asyncTearDown = fixture.KimiACPTests.asyncTearDown
    scenario = fixture.KimiACPTests.scenario
    trace = fixture.KimiACPTests.trace
    invoke = fixture.KimiACPTests.invoke
    wire_path = fixture.KimiACPTests.wire_path

    def inject(self, target, *, compaction=False, status=403, context=True):
        script = self.executable.read_text()
        old = '    if method == "initialize":'
        message = ('Your current plan supports 256K context. 1M is available on higher plans; upgrade.'
                   if context else fixture.PRIVATE)
        records = []
        if compaction:
            records = [{'type':'turn.prompt'}, {'type':'full_compaction.begin'},
                {'type':'turn.ended','turnId':23,'reason':'failed','error':{
                    'code':'provider.auth_error','name':'APIStatusError','message':message,
                    'details':{'statusCode':status,'requestId':fixture.PRIVATE}}}]
        injected = '    if method == '+repr(target)+':\n'
        for record in records:
            injected += '        append_wire('+repr(record)+')\n'
        injected += ('        send({"id":row["id"],"error":{"code":-32000,"message":'
                     +repr(fixture.PRIVATE)+'}})\n        continue\n'+old)
        self.executable.write_text(script.replace(old, injected))

    async def test_compaction_context_403_stays_failed_without_retry_or_private_error(self):
        self.scenario();self.inject('session/prompt', compaction=True)
        result=await self.invoke(permission='full_access')
        self.assertEqual(result['failure_code'],'native_provider_error')
        diagnostic=result['native_execution']['failure_diagnostic']
        self.assertEqual(diagnostic, {'rpc_method':'session/prompt','rpc_code':-32000,'prompt_sent':True,
            'phase':'compaction','reason':'provider_context_limit','http_status':403})
        self.assertEqual(sum(row.get('method')=='session/prompt' for row in self.trace()),1)
        self.assertTrue(result['cleanup_confirmed'])
        self.assertNotIn(fixture.PRIVATE,json.dumps(result))

    async def test_authenticate_failure_remains_auth_unconfirmed_and_never_sends_prompt(self):
        self.scenario();self.inject('authenticate')
        result=await self.invoke()
        self.assertEqual(result['failure_code'],'native_auth_unconfirmed')
        d=result['native_execution']['failure_diagnostic']
        self.assertEqual((d['phase'],d['rpc_method'],d['prompt_sent']),('handshake','authenticate',False))
        self.assertFalse(any(row.get('method')=='session/prompt' for row in self.trace()))
        self.assertTrue(result['cleanup_confirmed'])

    async def test_session_error_is_not_login_error_and_old_failed_wire_is_not_current_evidence(self):
        self.scenario();self.inject('session/resume')
        result=await self.invoke(session_id=fixture.SID)
        self.assertEqual(result['failure_code'],'native_provider_error')
        self.assertEqual(result['native_execution']['failure_diagnostic']['phase'],'session')
        self.assertFalse(result['native_execution']['failure_diagnostic']['prompt_sent'])
        self.assertTrue(result['cleanup_confirmed'])

    async def test_real_provider_auth_failure_does_not_become_context_limit_from_status_alone(self):
        self.scenario();self.inject('session/prompt',compaction=True,status=401,context=False)
        result=await self.invoke()
        self.assertEqual(result['native_execution']['failure_diagnostic']['reason'],'provider_auth_error')
        self.assertEqual(result['native_execution']['failure_diagnostic']['http_status'],401)
        self.assertNotIn(fixture.PRIVATE,json.dumps(result))


class FailedTurnEvidenceTests(unittest.TestCase):
    def test_only_new_exact_turn_is_diagnosed_and_successful_compaction_does_not_label_later_loop(self):
        import tempfile
        from pathlib import Path
        from local_agent_coordinator import kimi_acp
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'wire.jsonl'
            old={'type':'turn.ended','reason':'failed','error':{'code':'provider.auth_error',
                'message':'Your current plan supports 256K context; upgrade for higher plans.', 'details':{'statusCode':403}}}
            path.write_text(json.dumps(old)+'\n')
            before=kimi_acp.wire_offset(path)
            rows=[{'type':'turn.prompt'}, {'type':'full_compaction.begin'},
                  {'type':'full_compaction.complete'}, {'type':'llm.request','kind':'loop'},
                  {'type':'turn.ended','reason':'failed','error':{'code':'provider.error','message':fixture.PRIVATE,
                                                            'details':{'statusCode':503}}}]
            with path.open('a') as stream:
                for row in rows:stream.write(json.dumps(row)+'\n')
            self.assertEqual(kimi_acp.failed_turn_diagnostic(path,before),
                             {'phase':'prompt','reason':'provider_error','http_status':503})
            # A replaced file cannot be used as this turn's native evidence.
            path.rename(path.with_suffix('.old'))
            path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
            self.assertIsNone(kimi_acp.failed_turn_diagnostic(path,before))

    def test_store_persists_only_whitelisted_failure_metadata(self):
        import tempfile
        from pathlib import Path
        from uuid import uuid4
        from local_agent_coordinator.room_store import RoomStore
        with tempfile.TemporaryDirectory() as directory:
            store=RoomStore(Path(directory)/'room.sqlite3')
            try:
                room=store.create_room('Fictional metadata test','claude',str(uuid4()),collaboration=True)[0]
                job=store.claim(room,'claude')
                diagnostic={'phase':'compaction','reason':'provider_context_limit','http_status':403,
                    'rpc_method':'session/prompt','rpc_code':-32000,'prompt_sent':True}
                store.record_native_execution(job['id'],{'agent':'kimi','failure_diagnostic':{
                    **diagnostic,'message':fixture.PRIVATE,'requestId':fixture.PRIVATE}},'failed')
                with store._connection() as db:
                    value=json.loads(db.execute('SELECT metadata_json FROM native_executions WHERE job_id=?',
                                                (job['id'],)).fetchone()[0])
                self.assertEqual(value['failure_diagnostic'],diagnostic)
                self.assertNotIn(fixture.PRIVATE,json.dumps(value))
            finally:store.close()

    def test_context_handoff_requires_bound_prompt_and_current_native_tail(self):
        import tempfile
        from pathlib import Path
        from local_agent_coordinator import kimi_acp
        rows=[{'type':'turn.prompt','time':1100,'input':[{'type':'text','text':'bound host context'}]},
              {'type':'full_compaction.begin','time':1200},
              {'type':'llm.request','time':1300,'kind':'compaction','modelAlias':fixture.MODEL},
              {'type':'turn.ended','time':1400,'turnId':23,'reason':'failed','error':{
                  'code':'provider.auth_error','message':'Your current plan supports 256K context; upgrade for higher plans.',
                  'details':{'statusCode':403}}}]
        with tempfile.TemporaryDirectory() as directory:
            cwd=Path(directory).resolve();path=cwd/'wire.jsonl'
            from unittest.mock import patch
            with patch.object(kimi_acp,'session_wire',return_value=path):
                def proof(validator=lambda prompt:prompt=='bound host context'):
                    return kimi_acp.context_limit_recovery_proof(cwd,fixture.SID,claimed_at=1,finished_at=2,
                        model=fixture.MODEL,validate_prompt=validator)
                path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
                self.assertIsNotNone(proof())
                self.assertIsNone(proof(lambda _:False))
                for tail in ({'type':'config.update'}, {'type':'turn.started','time':1500,'turnId':24},
                             {'type':'unknown','time':float('nan')}):
                    path.write_text(''.join(json.dumps(row)+'\n' for row in rows+[tail]))
                    self.assertIsNone(proof())
