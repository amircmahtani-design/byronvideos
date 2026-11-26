"""Fresh narration stage through real native FFmpeg and fake private cloud state."""
import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from byron_video.narration_stage import NarrationStage,CloudNarrationAudioStore,FirestoreNarrationRecords
from byron_video.cloud_journal import FirestoreSpendJournal
from byron_video.provider_execution import FileResponseStore,SpendApproval,WireResponse,ProviderOutcomeUnknown
from byron_video.elevenlabs_adapter import fingerprint,plan_requests
from test_provider_execution_stdlib import profile
from test_cloud_journal_stdlib import Client,PROJECT
from test_cloud_storage_stdlib import FakeBucket


class NarrationStageChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();p=Path(cls.temp.name)/'tone.mp3'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=44100:duration=5.9',
            '-c:a','libmp3lame','-b:a','128k',str(p)],check=True,capture_output=True)
        cls.audio=p.read_bytes();cls.version=subprocess.check_output(['ffmpeg','-version']).decode().splitlines()[0].split()[2]
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.db=Client();self.bucket=FakeBucket()
        self.text='Synthetic words stay exactly unchanged.';snapshot={'narrated_text':self.text,'processing_plan':{'narration_profile':profile()}}
        self.job=SimpleNamespace(id='a'*32,owner_id='fixture_owner',snapshot=snapshot,snapshot_sha256=fingerprint(snapshot))
        self.journal=FirestoreSpendJournal(self.db,project_id=PROJECT,namespace='synthetic_stage',owner_id=self.job.owner_id,job_id=self.job.id,
            budget_scope='synthetic_budget',approved_total_cap_cents=100,prior_spend_cents=0,transaction_runner=self.db.run)
        self.records=FirestoreNarrationRecords(self.db,project_id=PROJECT,namespace='synthetic_stage',transaction_runner=self.db.run)
        self.store=CloudNarrationAudioStore(SimpleNamespace(bucket=lambda name:self.bucket),bucket='synthetic-private-bucket')
        self.archive=FileResponseStore(self.root/'responses');self.calls=[];self.active=True
        self.approval=SpendApproval('synthetic_approval',fingerprint(plan_requests(self.text,profile())),(2,),50,2)
        def transport(request):
            self.calls.append(request);text=request['body']['text'];n=len(text)
            payload=json.dumps({'audio_base64':base64.b64encode(self.audio).decode(),'alignment':{'characters':list(text),
                'character_start_times_seconds':[i*5/n for i in range(n)],'character_end_times_seconds':[(i+1)*5/n for i in range(n)]}}).encode()
            return WireResponse('synthetic_request_1',payload)
        self.stage=NarrationStage(journal_factory=lambda job:self.journal,response_store_factory=lambda job:self.archive,transport=transport,
            audio_store=self.store,records=self.records,expected_ffmpeg_version=self.version,activation_check=lambda:self.active,
            approval_loader=lambda job:self.approval,source_kind='synthetic_test',enabled=True)
    def tearDown(self):self.tmp.cleanup()
    def run_stage(self):return self.stage(self.job,self.root/'work')
    def test_fresh_generation_native_decode_publication_and_readonly_replay(self):
        first=self.run_stage();writes=len(self.bucket.writes)
        self.assertEqual(first['report']['exact_text'],self.text);self.assertEqual(first['report']['duration_seconds'],5.9)
        self.assertEqual(first['report']['profile_sha256'],fingerprint(profile()));self.assertTrue(first['report']['synthetic'])
        self.assertEqual(self.run_stage(),first);self.assertEqual(len(self.calls),1);self.assertEqual(len(self.bucket.writes),writes)
    def test_disabled_or_bad_workspace_runtime_before_provider(self):
        self.stage.enabled=False
        with self.assertRaises(ValueError):self.run_stage()
        self.stage.enabled=True;self.stage.version='incorrect'
        with self.assertRaises(ValueError):self.run_stage()
        self.assertEqual(self.calls,[]);self.assertEqual(self.db.data,{})
    def test_existing_campaign_spend_blocks_before_narration(self):
        self.journal.arm('synthetic_prior_operation',{'prior':True},49,50)
        with self.assertRaises(ValueError):self.run_stage()
        self.assertEqual(self.calls,[])
    def test_unknown_provider_never_resubmits(self):
        calls=[]
        def unknown(request):calls.append(request);raise TimeoutError()
        self.stage.transport=unknown
        for _ in range(2):
            with self.assertRaises(ProviderOutcomeUnknown):self.run_stage()
        self.assertEqual(len(calls),1)
    def test_unknown_audio_write_recovers_only_by_readback(self):
        original=self.store.publish
        def unknown(job,data):original(job,data);raise TimeoutError()
        self.store.publish=unknown
        with self.assertRaises(ProviderOutcomeUnknown):self.run_stage()
        writes=len(self.bucket.writes);result=self.run_stage()
        self.assertEqual(result['report']['exact_text'],self.text);self.assertEqual(len(self.calls),1);self.assertEqual(len(self.bucket.writes),writes)
    def test_missing_unknown_audio_stays_hold_without_reupload(self):
        self.store.publish=lambda job,data:(_ for _ in ()).throw(TimeoutError())
        for _ in range(2):
            with self.assertRaises(ProviderOutcomeUnknown):self.run_stage()
        self.assertEqual(len(self.calls),1);self.assertEqual(self.bucket.writes,[])
    def test_changed_approval_or_snapshot_cannot_reuse_report(self):
        self.run_stage();self.approval=replace(self.approval,reference='different_approval')
        with self.assertRaises(ValueError):self.run_stage()
        self.assertEqual(len(self.calls),1)
    def test_expired_quote_allows_ready_and_unknown_publication_readonly_recovery(self):
        self.stage.submission_check=lambda job,approval:True
        result=self.run_stage();writes=len(self.bucket.writes)
        self.stage.submission_check=lambda job,approval:False
        self.assertEqual(self.run_stage(),result);self.assertEqual(len(self.calls),1);self.assertEqual(len(self.bucket.writes),writes)
        # A second fresh job writes audio but loses its receipt; expiry must not prevent readback.
        self.tmp.cleanup();self.setUp()
        original=self.store.publish
        def unknown(job,data):original(job,data);raise TimeoutError()
        self.store.publish=unknown
        with self.assertRaises(ProviderOutcomeUnknown):self.run_stage()
        self.stage.submission_check=lambda job,approval:False
        self.assertEqual(self.run_stage()['report']['exact_text'],self.text);self.assertEqual(len(self.calls),1)

    def test_expired_quote_blocks_fresh_narration_before_reservation(self):
        self.stage.submission_check=lambda job,approval:False
        with self.assertRaises(ValueError):self.run_stage()
        self.assertEqual(self.calls,[]);self.assertEqual(self.db.data,{})

    def test_other_owner_spend_journal_rejected(self):
        self.journal.owner_id='different_owner'
        with self.assertRaises(ValueError):self.run_stage()
        self.assertEqual(self.calls,[])
    def test_expired_activation_between_reservation_and_submit_retains_hold(self):
        count=[]
        def active():count.append(1);return len(count)<4
        self.stage.activation_check=active
        with self.assertRaises(ProviderOutcomeUnknown):self.run_stage()
        self.assertEqual(self.calls,[])
        self.stage.activation_check=lambda:True
        with self.assertRaises(ProviderOutcomeUnknown):self.run_stage()
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
