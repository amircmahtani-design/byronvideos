import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {countWords,validateScript,validateImages,moveItem,safeJobId,safeDownloadURL,displayProgress,formatDuration,completedFilmNote} from './core.mjs';
const image = (name='painting.png', type='image/png', size=100) => ({name,type,size});
test('long full paste and five photos fit without discarding headers or words', () => {
  const source = 'Synthetic title\nBy Example Writer\nwww.example.com\n\n' + 'Every exact source word remains. '.repeat(180);
  assert.ok(source.length > 4000 && countWords(source) > 450);
  assert.equal(validateScript(source), null);
  assert.equal(validateScript('word '.repeat(2000)), null);
  assert.equal(validateScript('abcdefg '.repeat(2000)), null);
  const photos = Array.from({length:5}, (_, i) => image(`${i}.jpg`, 'image/jpeg'));
  assert.equal(validateImages(photos), null);
  assert.deepEqual(photos.map(photo=>photo.name), ['0.jpg','1.jpg','2.jpg','3.jpg','4.jpg']);
});
test('word count handles whitespace without editing source', () => { assert.equal(countWords('  A\nnew   essay. '),3); assert.equal(countWords('  '),0); });
test('scripts are validated, never truncated', () => { const source='a'.repeat(16001); assert.match(validateScript(source),/nothing has been removed/); assert.equal(source.length,16001); assert.ok(validateScript('  ')); assert.equal(validateScript('This exact text.'),null); assert.equal(validateScript('Short.'),null); assert.match(validateScript('a '.repeat(2001)),/technical limit of 2,000 words/); });
test('four or five PNG/JPEG inputs only', () => { assert.equal(validateImages(Array(4).fill(image())),null); assert.equal(validateImages(Array(5).fill(image('p.jpg','image/jpeg'))),null); assert.ok(validateImages(Array(3).fill(image()))); assert.ok(validateImages(Array(6).fill(image()))); assert.ok(validateImages([image('p.svg','image/svg+xml')],false)); assert.ok(validateImages([image('p.png','image/jpeg',0)],false)); assert.ok(validateImages([image('p.png','image/png',20*1024*1024+1)],false)); });
test('reordering preserves original objects and bounds', () => { const a=image('a.png'),b=image('b.png'),c=image('c.png'); const original=[a,b,c]; assert.deepEqual(moveItem(original,1,-1),[b,a,c]); assert.deepEqual(original,[a,b,c]); assert.deepEqual(moveItem(original,0,-1),original); assert.deepEqual(moveItem(original,2,1),original); });
test('job identifiers cannot escape local paths', () => { assert.equal(safeJobId('abc12345-1234'),'abc12345-1234'); for(const value of ['../secret','short','a/bbbbbbb',null,'x'.repeat(81)]) assert.equal(safeJobId(value),null); });
test('download link must match exact same-origin job', () => { const origin='http://127.0.0.1:8000', id='abc12345'; assert.equal(safeDownloadURL(`/api/jobs/${id}/video`,origin,id),`${origin}/api/jobs/${id}/video`); for(const value of ['https://example.com/video','javascript:alert(1)','/api/jobs/otherjob/video','/api/jobs/abc12345/video?send=private','/api/jobs/abc12345/video#hash']) assert.equal(safeDownloadURL(value,origin,id),null); });
test('progress requires a real bounded finite number', () => { assert.equal(displayProgress(undefined),null); assert.equal(displayProgress('50'),null); assert.equal(displayProgress(NaN),null); assert.equal(displayProgress(120),100); assert.equal(displayProgress(-1),0); assert.equal(displayProgress(62),62); });
test('completion duration is informational at every editorial boundary', () => {
  for (const [seconds,label] of [[89,'1:29'],[90,'1:30'],[180,'3:00'],[181,'3:01']]) {
    assert.equal(formatDuration(seconds),label);
    const text=completedFilmNote({duration_seconds:seconds,duration_advisory:seconds===89||seconds===181?'Your complete script and narration speed were preserved.':null});
    assert.ok(text.includes(`Duration ${label}.`));
    assert.ok(text.includes('Download your MP4'));
  }
  for(const value of [NaN,Infinity,-1,0,'89',undefined]) assert.equal(formatDuration(value),null);
});
test('search metadata lists only the verified public landing page', () => {
  const read=name=>readFileSync(new URL(name,import.meta.url),'utf8');
  assert.ok(read('index.html').includes('<link rel="canonical" href="https://byronvideos.netlify.app/">'));
  assert.deepEqual([...read('sitemap.xml').matchAll(/<loc>(.*?)<\/loc>/gu)].map(match=>match[1]),['https://byronvideos.netlify.app/']);
  for(const path of ['/api/','/jobs/','/creator/','/uploads/','/.private/']) assert.ok(read('robots.txt').includes(`Disallow: ${path}`));
});


test('saved voices preserve selection on refresh failure and flag removal without fallback', async () => {
  const {refreshVoiceState, voiceLabel, snapshotVoice} = await import('./core.mjs');
  const initial = refreshVoiceState({voices:[], selectedId:''}, {voices:[{id:'fixture_voice_1',name:'Narrator'},{id:'fixture_voice_2',name:'Narrator'}],default_voice_id:'fixture_voice_1'});
  const frozen = snapshotVoice(initial);
  assert.notEqual(voiceLabel(initial.voices[0], initial.voices), voiceLabel(initial.voices[1], initial.voices));
  const failed = refreshVoiceState(initial, null);
  assert.equal(failed.selectedId, 'fixture_voice_1');
  assert.deepEqual(failed.voices, initial.voices);
  const removed = refreshVoiceState(initial, {voices:[initial.voices[1]],default_voice_id:'fixture_voice_2'});
  assert.equal(removed.removed, true);
  assert.equal(removed.selectedId, 'fixture_voice_1');
  assert.throws(()=>snapshotVoice(removed));
  initial.voices[0].name = 'Renamed'; initial.selectedId='fixture_voice_2';
  assert.deepEqual(frozen, {id:'fixture_voice_1',name:'Narrator'});
  assert.equal(Object.isFrozen(frozen), true);
});

const hostedAPI = await import('./hosted.mjs');
const submitFixture = (snapshot, capability, send, checkpoint = () => {}) => hostedAPI.submitHosted(snapshot, capability, send, checkpoint);
const voices = () => ({voices:[{id:'fixture_voice_1',name:'Narrator'}],selectedId:'fixture_voice_1',removed:false});
const sourceFiles = () => Array.from({length:4}, (_, i) => new File([`synthetic-${i}`],`painting-${i}.png`,{type:'image/png'}));
const uploadReceipt = () => ({upload_id:'fixture_upload_1',images:Array.from({length:4},(_,i)=>({id:`fixture_image_${i}`,position:i+1}))});
const reply = (status, payload) => ({status,ok:status>=200&&status<300,json:async()=>payload});
test('hosted mode requires explicit versioned capability and session CSRF', () => {
  const valid={enabled:true,mode:'hosted',protocol_version:1,live_release:false,synthetic_fixture:true,csrf_token:'x'.repeat(32)};
  assert.ok(hostedAPI.hostedCapability(valid));
  for(const value of [null,{}, {...valid,enabled:false}, {...valid,live_release:true}, {...valid,synthetic_fixture:false}, {...valid,mode:'local'}, {...valid,protocol_version:2}, {...valid,csrf_token:'short'}]) assert.equal(hostedAPI.hostedCapability(value),null);
});
test('hosted submission freezes voice and original ordered files without rewriting text', () => {
  const state=voices(), files=sourceFiles();
  const snapshot=hostedAPI.freezeSubmission('  Exact words.\n',files,state,'fixture_request_0001');
  state.selectedId='fixture_voice_2'; state.voices[0].name='Changed'; files.reverse();
  assert.equal(snapshot.script,'  Exact words.\n');
  assert.equal(snapshot.voice.id,'fixture_voice_1'); assert.equal(snapshot.voice.name,'Narrator');
  assert.equal(snapshot.files[0].name,'painting-0.png');
  assert.ok(Object.isFrozen(snapshot) && Object.isFrozen(snapshot.files));
  assert.throws(()=>hostedAPI.freezeSubmission('Words.',sourceFiles(),{...voices(),removed:true},'fixture_request_0001'));
});
test('private upload receipt requires exact count, unique IDs and numbered order', () => {
  assert.equal(hostedAPI.uploadedImages(uploadReceipt(),4).length,4);
  for(const change of [p=>p.images.reverse(),p=>p.images.pop(),p=>p.images[0].id='../bad/path',p=>p.images[1].id=p.images[0].id,p=>p.upload_id='']) {
    const p=uploadReceipt(); change(p); assert.throws(()=>hostedAPI.uploadedImages(p,4));
  }
});
test('hosted transport bridges upload and JSON job with one stable key and frozen voice', async () => {
  const snapshot=hostedAPI.freezeSubmission('  Exact words.\n',sourceFiles(),voices(),'fixture_request_0001');
  const calls=[],checkpoints=[];
  const result=await submitFixture(snapshot,{csrf:'x'.repeat(32)},async (url,options)=>{
    calls.push({url,options}); return url==='/api/uploads'?reply(200,uploadReceipt()):reply(202,{id:'fixture_job_1'});
  },value=>checkpoints.push(value));
  assert.deepEqual(result,{id:'fixture_job_1'}); assert.equal(calls.length,2);
  assert.deepEqual(calls[0].options.body.getAll('images').map(f=>f.name),snapshot.files.map(f=>f.name));
  for(const c of calls) {assert.equal(c.options.headers['Idempotency-Key'],snapshot.key);assert.equal(c.options.headers['X-CSRF-Token'],'x'.repeat(32));assert.equal(c.options.credentials,'same-origin');}
  assert.deepEqual(JSON.parse(calls[1].options.body),{script:snapshot.script,images:uploadReceipt().images,voice_id:'fixture_voice_1'});
  assert.deepEqual(checkpoints,[{key:snapshot.key,stage:'upload'},{key:snapshot.key,stage:'job'}]);
});
test('unknown upload outcome never starts a film or retries', async () => {
  let calls=0;
  await assert.rejects(submitFixture(hostedAPI.freezeSubmission('Words.',sourceFiles(),voices(),'fixture_request_0001'),{csrf:'x'.repeat(32)},async()=>{calls++;throw new Error('lost');}),error=>error.uncertain===true&&error.stage==='upload');
  assert.equal(calls,1);
});
test('unknown job outcome and conflicts stay locked without paid retries', async () => {
  for(const response of [()=>{throw new Error('lost');},()=>reply(409,{detail:'conflict'}),()=>reply(202,{id:'bad'})]) {
    let calls=0;
    await assert.rejects(submitFixture(hostedAPI.freezeSubmission('Words.',sourceFiles(),voices(),'fixture_request_0001'),{csrf:'x'.repeat(32)},async()=>++calls===1?reply(200,uploadReceipt()):response()),error=>error.uncertain===true&&error.stage==='job');
    assert.equal(calls,2);
  }
});
test('definite validation rejection is reported without an automatic retry', async () => {
  let calls=0;
  await assert.rejects(submitFixture(hostedAPI.freezeSubmission('Words.',sourceFiles(),voices(),'fixture_request_0001'),{csrf:'x'.repeat(32)},async()=>{calls++;return reply(422,{detail:'Invalid image bytes'});}),error=>error.uncertain===false&&error.stage==='upload');
  assert.equal(calls,1);
});

test('initial recovery checkpoint failure prevents every hosted POST', async () => {
  let requests=0;
  await assert.rejects(submitFixture(hostedAPI.freezeSubmission('Words.',sourceFiles(),voices(),'fixture_request_0001'),{csrf:'x'.repeat(32)},async()=>{requests++;return reply(200,uploadReceipt());},()=>{throw new Error('quota');}),error=>error.checkpointFailed===true&&error.uncertain===false&&error.stage==='upload'&&error.message.includes('Nothing was uploaded'));
  assert.equal(requests,0);
});
test('job recovery checkpoint failure after upload prevents job POST', async () => {
  let requests=0, checkpoints=0;
  await assert.rejects(submitFixture(hostedAPI.freezeSubmission('Words.',sourceFiles(),voices(),'fixture_request_0001'),{csrf:'x'.repeat(32)},async()=>{requests++;return reply(200,uploadReceipt());},()=>{if(++checkpoints===2) throw new Error('quota');}),error=>error.checkpointFailed===true&&error.uncertain===true&&error.stage==='job'&&error.message.includes('No film request was sent'));
  assert.equal(requests,1);
});
test('pending and active hosted references require storage read-back without clearing pending', () => {
  const values=new Map();
  const storage={setItem:(k,v)=>values.set(k,v),getItem:k=>values.get(k)};
  const checkpoint={key:'fixture_request_0001',stage:'job'};
  hostedAPI.persistPending(storage,checkpoint);
  hostedAPI.persistHostedJob(storage,'fixture_job_1');
  assert.equal(values.get('byron-pending-submission'),JSON.stringify(checkpoint));
  assert.equal(values.get('byron-active-job-mode'),'hosted');
  for(const broken of [{setItem:()=>{throw new Error('quota');},getItem:()=>null},{setItem:()=>{},getItem:()=>null}]) {
    assert.throws(()=>hostedAPI.persistPending(broken,checkpoint));
    assert.throws(()=>hostedAPI.persistHostedJob(broken,'fixture_job_1'));
  }
});
test('upload receipt IDs may use the backend full 128 character bound', () => {
  const receipt=uploadReceipt();receipt.images[0].id='a'.repeat(128);
  assert.equal(hostedAPI.uploadedImages(receipt,4)[0].id.length,128);
  receipt.images[0].id='a'.repeat(129);assert.throws(()=>hostedAPI.uploadedImages(receipt,4));
});
test('lost job receipt recovery only looks up the original opaque reference', async () => {
  const calls=[];
  const result=await hostedAPI.recoverSubmission('fixture_request_0001',async(url,options)=>{calls.push({url,options});return reply(200,{id:'fixture_job_1',status:'HOLD'});});
  assert.deepEqual(result,{id:'fixture_job_1'});
  assert.deepEqual(calls,[{url:'/api/submissions/fixture_request_0001',options:{method:'GET',credentials:'same-origin',cache:'no-store'}}]);
});
test('missing or malformed recovery result stays unresolved and never posts', async () => {
  for(const response of [reply(404,{}),reply(503,{}),reply(200,{id:'../secret'})]) {
    const methods=[];
    await assert.rejects(hostedAPI.recoverSubmission('fixture_request_0001',async(url,options)=>{methods.push(options.method);return response;}));
    assert.deepEqual(methods,['GET']);
  }
});
test('accepted runtime capability requires explicit live mode and bound release fingerprint', () => {
  const value={enabled:true,mode:'hosted',protocol_version:1,live_release:true,synthetic_fixture:false,
    runtime_release_sha256:'a'.repeat(64),csrf_token:'x'.repeat(32)};
  assert.equal(hostedAPI.hostedCapability(value).live,true);
  for(const invalid of [{...value,runtime_release_sha256:undefined},{...value,synthetic_fixture:true},
      {...value,runtime_release_sha256:'unreviewed'}, {...value,enabled:false}]) assert.equal(hostedAPI.hostedCapability(invalid),null);
});

test('resume checkpoint contains only bounded opaque job/key/attempt fields', () => {
  assert.deepEqual(hostedAPI.resumeCheckpoint('fixture_job_1','fixture_resume_key_0001',1),{job_id:'fixture_job_1',key:'fixture_resume_key_0001',attempt:1});
  for(const attempt of [0,4,1.5,'1']) assert.throws(()=>hostedAPI.resumeCheckpoint('fixture_job_1','fixture_resume_key_0001',attempt));
  assert.throws(()=>hostedAPI.resumeCheckpoint('../other','fixture_resume_key_0001',1));
});
test('resume storage must round-trip before permission to send', () => {
  const checkpoint=hostedAPI.resumeCheckpoint('fixture_job_1','fixture_resume_key_0001',1);
  const map=new Map(), storage={setItem:(k,v)=>map.set(k,v),getItem:k=>map.get(k)};
  hostedAPI.persistResume(storage,checkpoint);
  assert.deepEqual(JSON.parse(map.get('byron-worker-resume')),checkpoint);
  assert.throws(()=>hostedAPI.persistResume({setItem:()=>{},getItem:()=>null},checkpoint));
});
test('failed resume checkpoint prevents every resume POST', async () => {
  let requests=0;
  await assert.rejects(hostedAPI.resumeHosted('fixture_job_1',{live:true,csrf:'x'.repeat(32)},'fixture_resume_key_0001',1,
    async()=>{requests++;},()=>{throw new Error('storage unavailable');}));
  assert.equal(requests,0);
});
test('explicit resume uses one stable key and never replays an uncertain POST', async () => {
  const calls=[],markers=[];
  await assert.rejects(hostedAPI.resumeHosted('fixture_job_1',{live:true,csrf:'x'.repeat(32)},'fixture_resume_key_0001',1,
    async(url,options)=>{calls.push({url,options});throw new Error('lost response');},value=>markers.push(value)));
  assert.equal(calls.length,1);assert.equal(markers.length,1);
  assert.equal(calls[0].url,'/api/jobs/fixture_job_1/resume');assert.equal(calls[0].options.method,'POST');
  assert.equal(calls[0].options.headers['Idempotency-Key'],markers[0].key);
  assert.equal(calls[0].options.body,undefined);
});
test('resume marker clears only for matching server-confirmed attempt outcome', () => {
  const marker=hostedAPI.resumeCheckpoint('fixture_job_1','fixture_resume_key_0001',1);
  const known={id:'fixture_job_1',resume_attempt:1,resume_outcome_known:true};
  assert.equal(hostedAPI.resumeResolved(marker,known),true);
  for(const value of [{...known,id:'fixture_job_2'},{...known,resume_attempt:2},{...known,resume_outcome_known:false},{id:'fixture_job_1',status:'HOLD'}]) assert.equal(hostedAPI.resumeResolved(marker,value),false);
});
test('fixture-only capability cannot resume and malformed receipt cannot confirm it', async () => {
  let calls=0;
  await assert.rejects(hostedAPI.resumeHosted('fixture_job_1',{live:false,csrf:'x'.repeat(32)},'fixture_resume_key_0001',1,async()=>{calls++;},()=>{}));
  assert.equal(calls,0);
  await assert.rejects(hostedAPI.resumeHosted('fixture_job_1',{live:true,csrf:'x'.repeat(32)},'fixture_resume_key_0001',1,
    async()=>reply(202,{id:'fixture_job_2',resume_attempt:1}),()=>{}));
});

import {isPrivateDraft} from './core.mjs';
test('private draft requires exact state, kind and flag; never inferred from generic completion', () => {
  const draft={status:'draft_preview_ready',artifact_kind:'PRIVATE_DRAFT_PREVIEW',draft:true,duration_seconds:32};
  assert.equal(isPrivateDraft(draft),true);
  for(const change of [{status:'complete'},{artifact_kind:'rendered_video'},{draft:false},{draft:'true'}]) assert.equal(isPrivateDraft({...draft,...change}),false);
  const note=completedFilmNote(draft);
  assert.match(note,/watermarked DRAFT/);assert.match(note,/not approved for publication/);
  assert.match(note,/whole film/);assert.match(note,/no approval or publishing action/);
});

test('explicit private preview capability permits private workflow without live release claim', () => {
  const value={enabled:true,mode:'hosted_private_preview',protocol_version:1,csrf_token:'c'.repeat(32),live_release:false,private_preview:true,synthetic_fixture:false,runtime_release_sha256:'a'.repeat(64)};
  assert.deepEqual(hostedAPI.hostedCapability(value),{csrf:'c'.repeat(32),live:false,preview:true});
  for(const changed of [{private_preview:false},{live_release:true},{synthetic_fixture:true},{runtime_release_sha256:null},{enabled:false}]) assert.equal(hostedAPI.hostedCapability({...value,...changed}),null);
});

const exactAPI=await import('./exact-film.mjs');
const exactArgs={jobId:'a'.repeat(32),artifactId:'preview_artifact_001',action:'approve',key:'exact_request_00000001',csrf:'c'.repeat(32)};
test('exact-film checkpoint persists only opaque action identity and must round trip',()=>{
  const value=exactAPI.exactCheckpoint({job_id:exactArgs.jobId,artifact_id:exactArgs.artifactId,key:exactArgs.key,action:'approve',phase:'approval',script:'not retained'});
  assert.deepEqual(Object.keys(value),['job_id','artifact_id','key','action','phase']);
  assert.throws(()=>exactAPI.persistExact({setItem(){},getItem(){return null;}},value));
});
test('exact-film checkpoint failure prevents all POST requests',async()=>{
  let count=0;await assert.rejects(exactAPI.exactFilmAction(exactArgs,async()=>{count++;},()=>{throw Error('storage');}));assert.equal(count,0);
});
test('exact-film action sends challenge then exact body only, preserving key',async()=>{
  const calls=[],points=[];
  const result=await exactAPI.exactFilmAction(exactArgs,async(url,options)=>{
    calls.push([url,options]);return {ok:true,json:async()=>calls.length===1?{challenge_id:'challenge_001',challenge_sha256:'d'.repeat(64)}:{id:exactArgs.jobId,reference:exactArgs.key,action:'approve',recorded:true}};
  },value=>points.push(value));
  assert.equal(calls.length,2);assert.equal(points[1].phase,'approval');assert.equal(result.recorded,true);
  assert.deepEqual(JSON.parse(calls[1][1].body),{preview_id:exactArgs.artifactId,challenge_id:'challenge_001',challenge_sha256:'d'.repeat(64)});
  assert.equal(calls[1][1].headers['Idempotency-Key'],exactArgs.key);
});
test('exact-film approval checkpoint failure stops after challenge; unknown POST never retries',async()=>{
  let count=0;await assert.rejects(exactAPI.exactFilmAction(exactArgs,async()=>{count++;return {ok:true,json:async()=>({challenge_id:'challenge_001',challenge_sha256:'d'.repeat(64)})};},value=>{if(value.phase==='approval')throw Error('storage');}));assert.equal(count,1);
  count=0;await assert.rejects(exactAPI.exactFilmAction(exactArgs,async()=>{count++;if(count===2)throw Error('unknown');return {ok:true,json:async()=>({challenge_id:'challenge_001',challenge_sha256:'d'.repeat(64)})};},()=>{}));assert.equal(count,2);
});
test('view renewal is distinct from approval and recovery needs exact recorded reference',async()=>{
  const calls=[];await exactAPI.exactFilmAction({...exactArgs,action:'renew'},async(url,options)=>{
    calls.push([url,options]);return {ok:true,json:async()=>calls.length===1?{challenge_id:'challenge_001',challenge_sha256:'d'.repeat(64)}:{id:exactArgs.jobId,reference:exactArgs.key,action:'renew',recorded:true}};
  },()=>{});
  assert.match(calls[1][0],/preview-view-renewal$/);assert.equal(JSON.parse(calls[1][1].body).artifact_id,exactArgs.artifactId);
  const pending={job_id:exactArgs.jobId,key:exactArgs.key};
  assert.equal(exactAPI.exactActionResolved(pending,{id:exactArgs.jobId,reference:exactArgs.key,action_recorded:true}),true);
  assert.equal(exactAPI.exactActionResolved(pending,{id:exactArgs.jobId,reference:'wrong',action_recorded:true}),false);
});

test('optional Gemini admin field stays hidden unless authenticated selected mode; clears on interruption', async () => {
  const {readFile}=await import('node:fs/promises'); const {runInNewContext}=await import('node:vm');
  const source=await readFile(new URL('./admin.mjs',import.meta.url),'utf8');
  for(const [mode,failSave] of [['vertex_eu',false],['gemini_developer',false],['gemini_developer',true],['fallback',false]]) {
    const elements=new Map(); const handlers={}; const calls=[];
    const el=id=>{if(!elements.has(id))elements.set(id,{value:'',hidden:true,disabled:true,textContent:'',addEventListener:(event,fn)=>handlers[id+':'+event]=fn});return elements.get(id);};
    const page={};
    runInNewContext(source,{document:{getElementById:el,querySelectorAll:selector=>selector==='input'?[el('gemini-key')]:[]},window:{addEventListener:(event,fn)=>page[event]=fn},location:{assign:()=>{}},AbortSignal:{timeout:()=>undefined},fetch:async(path,options)=>{calls.push([path,options]);return {ok:!(failSave && path==='/api/admin/keys/gemini'),status:200,json:async()=>({enabled:true,csrf_token:'x'.repeat(40),native_api_mode:mode})};}});
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(el('gemini-section').hidden,mode!=='gemini_developer');
    assert.equal(el('gemini-key').disabled,mode!=='gemini_developer');
    el('gemini-key').value='synthetic-key-never-live';handlers['gemini-form:submit']({preventDefault(){}});
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(calls.filter(([path])=>path==='/api/admin/keys/gemini').length,mode==='gemini_developer'?1:0);
    if(mode==='gemini_developer')assert.equal(el('gemini-key').value,'');
    page.pagehide();assert.equal(el('gemini-key').value,'');assert.equal(el('gemini-section').hidden,true);assert.equal(el('admin-fields').disabled,true);
  }
});
