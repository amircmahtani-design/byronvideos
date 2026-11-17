// Explicit owner actions only; importing this module never sends a request.
import {safeJobId} from './core.mjs';
const ID=/^[A-Za-z0-9_-]{8,128}$/;
const KEY=/^[A-Za-z0-9_-]{16,128}$/;
export function exactCheckpoint(value) {
  if (!value || !safeJobId(value.job_id) || !ID.test(value.artifact_id || '') || !KEY.test(value.key || '') ||
      !['approve','renew'].includes(value.action) || !['challenge','approval'].includes(value.phase)) throw new Error('Invalid private film action reference.');
  return Object.freeze({job_id:value.job_id,artifact_id:value.artifact_id,key:value.key,action:value.action,phase:value.phase});
}
export function persistExact(storage,value) {
  const checked=exactCheckpoint(value),encoded=JSON.stringify(checked);
  storage.setItem('byron-exact-film-action',encoded);
  if(storage.getItem('byron-exact-film-action')!==encoded) throw new Error('Private film action reference was not retained; no request was sent.');
  return checked;
}
export async function exactFilmAction({jobId,artifactId,action,key,csrf},send,checkpoint) {
  if(typeof csrf!=='string' || !/^[\x21-\x7e]{32,512}$/.test(csrf)) throw new Error('Reconnect to your private studio first.');
  const pending=exactCheckpoint({job_id:jobId,artifact_id:artifactId,action,key,phase:'challenge'});
  await checkpoint(pending);
  const route=`/api/jobs/${jobId}/${action==='approve'?'film-approval':'preview-view-renewal'}`;
  const field=action==='approve'?'preview_id':'artifact_id';
  const options=body=>({method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,'Idempotency-Key':key},body:JSON.stringify(body)});
  const challengeResponse=await send(route+'/challenge',options({[field]:artifactId}));
  if(!challengeResponse.ok) throw new Error('The private film challenge could not be confirmed. Check this film status; nothing will retry automatically.');
  const challenge=await challengeResponse.json();
  if(!ID.test(challenge?.challenge_id || '') || !/^[0-9a-f]{64}$/.test(challenge?.challenge_sha256 || '')) throw new Error('The exact film challenge could not be verified.');
  await checkpoint(exactCheckpoint({...pending,phase:'approval'}));
  const response=await send(route,options({[field]:artifactId,challenge_id:challenge.challenge_id,challenge_sha256:challenge.challenge_sha256}));
  if(!response.ok) throw new Error('The private film action result is uncertain. Check this same film; do not submit again.');
  const result=await response.json();
  if(result?.id!==jobId || result?.reference!==key || result?.action!==action || result?.recorded!==true) throw new Error('The private film action receipt could not be verified.');
  return result;
}
export function exactActionResolved(pending,status) {
  return !!pending && status?.id===pending.job_id && status?.reference===pending.key && status?.action_recorded===true;
}
