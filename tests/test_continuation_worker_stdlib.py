"""Offline continuation chain provenance tests. Synthetic bytes; decode mocked."""
import copy,hashlib,io,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_motion_continuation_stdlib import inputs
from byron_video.elevenlabs_adapter import fingerprint
from byron_video.motion_continuation import plan_continuation,EXTENSION_ENDPOINT,IMAGE_ENDPOINT
from byron_video.continuation_worker import stage_committed_continuation,validate_continuation_context,_operation,FirestorePreparedContinuation

class ContinuationWorkerChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'staged'
        data=inputs((12,12,12,12));self.audio=b'synthetic audio'
        data['narration_report']['audio_sha256']=hashlib.sha256(self.audio).hexdigest()
        data['timing']['audio_sha256']=data['narration_report']['audio_sha256']
        # Rebind the already-reviewed motion timing identity to the synthetic audio.
        from byron_video.motion_planning import plan_motion
        source=[]
        for image in data['images']:
            source.append(dict(id=image['id'],position=image['position'],source_sha256=image['sha256'],duration_seconds=12,
                source_review=dict(provenance='local_human_source_review',source_sha256=image['sha256'],reference_id='fixture_review',capabilities=['water','safe_camera_framing'])))
        data['motion_plan']=plan_motion(source,hashlib.sha256(data['narration_report']['exact_text'].encode()).hexdigest(),timing_plan=data['timing'])
        data['approved_prompts']=[dict(image_id=s['image_id'],source_sha256=s['source_sha256'],scene_sha256=fingerprint(s),prompt=s['prompt'],reference='fixture_prompt',approved=True) for s in data['motion_plan']['scenes']]
        self.profile={'fixture':'private-selected-profile'}
        data['narration_report']['profile_sha256']=fingerprint(self.profile)
        data['expected_narration_report_sha256']=fingerprint(data['narration_report'])
        self.report=data['narration_report'];self.report_sha=fingerprint(self.report)
        self.plan=plan_continuation(**data);self.sha=fingerprint(self.plan)
        frozen=dict(images=data['images'],input_order=[i['id'] for i in data['images']],narrated_text=self.report['exact_text'],processing_plan={'narration_profile':self.profile})
        self.job=SimpleNamespace(owner_id=data['owner_id'],id=data['job_id'],snapshot=frozen,snapshot_sha256=fingerprint(frozen))
        self.prefix=f"owners/{hashlib.sha256(self.job.owner_id.encode()).hexdigest()}/jobs/{self.job.id}/"
        self.objects={};self.records={};self.entries={}
        def ref(name,body,mime):
            self.objects[self.prefix+name]=body
            return dict(bucket='fixture-private-bucket',object=self.prefix+name,generation='1',sha256=hashlib.sha256(body).hexdigest(),bytes=len(body),content_type=mime)
        self.audio_ref=ref('narration.wav',self.audio,'audio/wav')
        for scene in self.plan['scenes']:
            previous=None
            for seq in range(len(scene['segments'])):
                rid=f"fixture_{scene['position']}_{seq}";url='https://queue.fal.run/minimax/h3-max-turbo/requests/'+rid
                handle=dict(endpoint=EXTENSION_ENDPOINT if seq else IMAGE_ENDPOINT,request_id=rid,response_url=url,status_url=url+'/status')
                video=ref(f"segments/{scene['image_id']}/{seq}.mp4",rid.encode(),'video/mp4')
                record=dict(schema_version=1,owner_id=self.job.owner_id,job_id=self.job.id,snapshot_sha256=self.job.snapshot_sha256,
                    continuation_plan_sha256=self.sha,image_id=scene['image_id'],source_sha256=scene['source_sha256'],sequence=seq,
                    operation_id=_operation(self.job.owner_id,self.job.id,scene['image_id'],seq),journal_binding_sha256='b'*64,
                    provider_handle=handle,provider_url='https://fal.media/files/'+rid+'.mp4',video=video,duration_seconds=10+seq*5,presentation_crop=None,
                    prior_record_sha256=previous[1] if previous else None,prior_clip_sha256=previous[0]['video']['sha256'] if previous else None,
                    review=dict(reference='fixture_review',clip_sha256=video['sha256'],source_sha256=scene['source_sha256'],prompt_sha256=hashlib.sha256(scene['approved_prompt'].encode()).hexdigest(),no_invented_action_reviewed=True),synthetic=True,full_decode_verified=True)
                self.records[(scene['image_id'],seq)]=(record,fingerprint(record));previous=(record,fingerprint(record))
                self.entries[record['operation_id']]=dict(state='received',plan_sha256='b'*64,receipt=handle)
        self.reads=[]
    def journal(self):
        b=self.plan['budget'];return SimpleNamespace(get=lambda op:self.entries.get(op),owner_id=self.job.owner_id,job_id=self.job.id,scope_hash=hashlib.sha256(b['scope'].encode()).hexdigest(),total_cap=b['approved_total_cap_cents'],prior_spend=b['prior_spend_cents'])
    def tearDown(self):self.tmp.cleanup()
    def reader(self,ref):
        self.reads.append(ref['object']);return dict(generation=ref['generation'],content_type=ref['content_type'],stream=io.BytesIO(self.objects[ref['object']]))
    def stage(self,**overrides):
        args=dict(job=self.job,plan=self.plan,expected_plan_sha256=self.sha,narration_report=self.report,expected_report_sha256=self.report_sha,
            narration_ref=self.audio_ref,segment_loader=lambda owner,job,image,seq:self.records[(image,seq)],reader=self.reader,
            spend_journal=self.journal(),root=self.root,expected_bucket='fixture-private-bucket',enabled=True)
        args.update(overrides)
        with patch('byron_video.portrait_composition.probe',side_effect=lambda path,**kw:dict(duration=15 if path.name.startswith('clip-') else 10)):
            return stage_committed_continuation(**args)
    def validate(self,result):
        return validate_continuation_context(result['continuation_context'],result['continuation_context_sha256'],staged=result['staged'],frozen=self.job.snapshot,clip_receipts=result['clip_receipts'],expected_report_sha256=self.report_sha)
    def mutate(self,key,change):
        record=self.records[key][0];change(record);self.records[key]=(record,fingerprint(record))
    def test_all_segments_independently_read_and_final_extension_truthful(self):
        result=self.stage();self.assertEqual(len(self.reads),9);self.assertEqual(result['provider_requests'],0)
        self.assertEqual(self.validate(result),[EXTENSION_ENDPOINT]*4)
        self.assertFalse(list(self.root.glob('chain-*')));self.assertEqual(len(list(self.root.glob('clip-*'))),4)
    def test_wrong_journal_scope_rejected(self):
        journal=self.journal();journal.owner_id='other'
        with self.assertRaises(ValueError):self.stage(spend_journal=journal)
        self.assertEqual(self.reads,[])
    def test_disabled_has_no_reads(self):
        with self.assertRaises(ValueError):self.stage(enabled=False)
        self.assertEqual(self.reads,[])
    def test_owner_source_and_semantic_review_fail_before_reads(self):
        key=next(iter(self.records));original=copy.deepcopy(self.records[key])
        for change in (lambda r:r.update(owner_id='other'),lambda r:r.update(source_sha256='f'*64),lambda r:r['review'].update(no_invented_action_reviewed=False)):
            self.records[key]=copy.deepcopy(original);self.mutate(key,change)
            with self.assertRaises(ValueError):self.stage()
            self.assertEqual(self.reads,[])
    def test_unknown_paid_outcome_never_accepted(self):
        next(iter(self.entries.values()))['state']='may_have_submitted'
        with self.assertRaises(ValueError):self.stage()
        self.assertEqual(self.reads,[])
    def test_wrong_prior_link_rejected(self):
        key=(self.plan['input_order'][0],1);self.mutate(key,lambda r:r.update(prior_clip_sha256='f'*64))
        with self.assertRaises(ValueError):self.stage()
    def test_changed_stored_bytes_rejected(self):
        self.objects[next(iter(self.objects))]=b'changed'
        with self.assertRaises(ValueError):self.stage()
    def test_wrong_generation_rejected(self):
        with self.assertRaises(ValueError):self.stage(reader=lambda ref:dict(generation='2',content_type=ref['content_type'],stream=io.BytesIO(self.objects[ref['object']])))
    def test_missing_intermediate_rejected(self):
        del self.records[(self.plan['input_order'][0],0)]
        with self.assertRaises(KeyError):self.stage()
    def test_context_tamper_and_relabel_rejected(self):
        result=self.stage();result['clip_receipts'][0]['model']=IMAGE_ENDPOINT
        with self.assertRaises(ValueError):self.validate(result)
    def test_complete_context_independent_digest_required(self):
        result=self.stage();result['continuation_context']['chains'][0]['segments'].pop(0)
        result['continuation_context_sha256']=fingerprint(result['continuation_context'])
        with self.assertRaises(ValueError):self.validate(result)
    def test_changed_trim_rejected(self):
        result=self.stage();path=result['staged']['files']['composition.json'];comp=json.loads(path.read_text());comp['clips'][0]['trim_length']=11;path.write_text(json.dumps(comp))
        with self.assertRaises(ValueError):self.validate(result)
    def test_persistence_binds_inputs_evidence_then_readonly_handoff(self):
        from test_cloud_storage_stdlib import FakeBucket
        from test_cloud_journal_stdlib import Client
        from test_worker_binding_stdlib import fixture
        from byron_video.continuation_worker import CloudPreparedInputStore,persist_prepared_continuation,PreparedContinuationHandoffLoader
        target=fixture()[0];target['bucket']='fixture-private-bucket'
        bucket=FakeBucket();store=CloudPreparedInputStore(SimpleNamespace(bucket=lambda name:bucket),bucket=target['bucket'])
        client=Client();registry=FirestorePreparedContinuation(client,project_id='synthetic-project',namespace='fixture_namespace',transaction_runner=client.run)
        events=[]
        evidence=SimpleNamespace(commit=lambda *args,**kw:events.append(kw))
        result=persist_prepared_continuation(job=self.job,prepared=self.stage(),target=target,input_store=store,evidence_store=evidence,handoff_registry=registry,enabled=True)
        self.assertEqual(len(bucket.writes),14);self.assertEqual(len(events),1)
        self.assertFalse(result['dispatched']);self.assertEqual(result['provider_requests'],0)
        loader=PreparedContinuationHandoffLoader(registry,target=target)
        self.assertEqual(loader(self.job),(result['manifest'],result['reference']))
        writes=len(bucket.writes)
        self.assertEqual(store.write(self.job.owner_id,self.job.id,'narration.wav',self.audio),result['manifest']['narration'])
        self.assertEqual(len(bucket.writes),writes)
        with self.assertRaises(ValueError):store.write(self.job.owner_id,self.job.id,'narration.wav',b'changed')
    def test_actual_full_decode_of_all_synthetic_chain_segments(self):
        import subprocess
        from byron_video import portrait_composition
        original=portrait_composition.probe;calls=[]
        # Native 24fps 9:16 fixtures, unique colour for every segment.
        for index,(key,item) in enumerate(self.records.items()):
            record=item[0];path=Path(self.tmp.name)/f'fixture-{index}.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',f'color=c=0x{index+1:06x}:s=72x128:r=24:d={record["duration_seconds"]}',
                '-c:v','libx264','-pix_fmt','yuv420p',str(path)],check=True,capture_output=True)
            body=path.read_bytes();ref=record['video'];self.objects[ref['object']]=body;ref['bytes']=len(body);ref['sha256']=hashlib.sha256(body).hexdigest()
            record['review']['clip_sha256']=ref['sha256']
            if key[1]:
                prev=self.records[(key[0],key[1]-1)];record['prior_record_sha256']=prev[1];record['prior_clip_sha256']=prev[0]['video']['sha256']
            self.records[key]=(record,fingerprint(record))
        args=dict(job=self.job,plan=self.plan,expected_plan_sha256=self.sha,narration_report=self.report,expected_report_sha256=self.report_sha,
            narration_ref=self.audio_ref,segment_loader=lambda owner,job,image,seq:self.records[(image,seq)],reader=self.reader,
            spend_journal=self.journal(),root=self.root,expected_bucket='fixture-private-bucket',enabled=True)
        def decode(*args,**kwargs):calls.append(args[0]);return original(*args,**kwargs)
        with patch.object(portrait_composition,'probe',side_effect=decode):result=stage_committed_continuation(**args)
        self.assertEqual(len(calls),8);self.assertEqual(self.validate(result),[EXTENSION_ENDPOINT]*4)
    def test_registry_immutable_owner_scoped(self):
        from test_cloud_journal_stdlib import Client
        client=Client();registry=FirestorePreparedContinuation(client,project_id='synthetic-project',namespace='fixture_namespace',transaction_runner=client.run)
        self.assertIsNone(registry.get(self.job))
        self.assertTrue(registry.commit(self.job,{'fixture':1},{'fixture':2},'c'*64))
        self.assertEqual(registry.get(self.job),({'fixture':1},{'fixture':2}))
        self.assertFalse(registry.commit(self.job,{'fixture':1},{'fixture':2},'c'*64))
        self.assertEqual(registry.load(self.job),({'fixture':1},{'fixture':2}))
        with self.assertRaises(ValueError):registry.commit(self.job,{'fixture':2},{'fixture':2},'c'*64)
        with self.assertRaises(ValueError):registry.load(SimpleNamespace(owner_id='other',id=self.job.id,snapshot_sha256=self.job.snapshot_sha256))


    def test_prepared_unknown_media_write_recovers_readonly_exact_bytes(self):
        from test_cloud_storage_stdlib import FakeBucket
        from byron_video.continuation_worker import CloudPreparedInputStore
        bucket=FakeBucket();store=CloudPreparedInputStore(SimpleNamespace(bucket=lambda name:bucket),bucket='fixture-private-bucket')
        bucket.fail_after=lambda name,match:name.endswith('/narration.wav')
        ref=store.write(self.job.owner_id,self.job.id,'narration.wav',self.audio)
        writes=len(bucket.writes);self.assertEqual(writes,2)
        self.assertEqual(store.write(self.job.owner_id,self.job.id,'narration.wav',self.audio),ref)
        self.assertEqual(len(bucket.writes),writes)

    def test_prepared_unknown_claim_never_reuploads_missing_media(self):
        from test_cloud_storage_stdlib import FakeBucket
        from byron_video.continuation_worker import CloudPreparedInputStore
        from byron_video.provider_execution import ProviderOutcomeUnknown
        bucket=FakeBucket();store=CloudPreparedInputStore(SimpleNamespace(bucket=lambda name:bucket),bucket='fixture-private-bucket')
        bucket.fail_after=lambda name,match:name.endswith('.claim.json')
        with self.assertRaises(ProviderOutcomeUnknown):store.write(self.job.owner_id,self.job.id,'narration.wav',self.audio)
        bucket.fail_after=None
        with self.assertRaises(ProviderOutcomeUnknown):store.write(self.job.owner_id,self.job.id,'narration.wav',self.audio)
        self.assertEqual(len(bucket.writes),1)

if __name__=='__main__':unittest.main()
