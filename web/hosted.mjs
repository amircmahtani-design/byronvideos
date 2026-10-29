// Explicit hosted protocol. No network request occurs merely by importing this module.
import {validateScript, validateImages, snapshotVoice, safeJobId} from './core.mjs';

export function hostedCapability(value) {
  const fixture = value?.live_release === false && value.synthetic_fixture === true;
  const accepted = value?.live_release === true && value.synthetic_fixture === false &&
    typeof value.runtime_release_sha256 === 'string' && /^[0-9a-f]{64}$/.test(value.runtime_release_sha256);
  const preview = value?.mode === 'hosted_private_preview' && value.private_preview === true && value.live_release === false && value.synthetic_fixture === false && typeof value.runtime_release_sha256 === 'string' && /^[0-9a-f]{64}$/.test(value.runtime_release_sha256);
  return value?.enabled === true && (value.mode === 'hosted' || preview) && value.protocol_version === 1 &&
    (fixture || accepted || preview) && typeof value.csrf_token === 'string' && /^[\x21-\x7e]{32,512}$/.test(value.csrf_token)
    ? Object.freeze({csrf: value.csrf_token, live: accepted, ...(preview ? {preview:true} : {})}) : null;
}

export function freezeSubmission(script, files, voices, key) {
  const error = validateScript(script) || validateImages(files);
  if (error) throw new Error(error);
  if (typeof key !== 'string' || !/^[A-Za-z0-9_-]{16,128}$/.test(key)) throw new Error('A secure submission reference is required.');
  return Object.freeze({script, files:Object.freeze([...files]), voice:snapshotVoice(voices), key});
}

export function uploadedImages(payload, count) {
  if (!payload || typeof payload.upload_id !== 'string' || !/^[A-Za-z0-9_-]{8,128}$/.test(payload.upload_id) ||
      !Array.isArray(payload.images) || payload.images.length !== count) throw new Error('The private upload receipt could not be verified.');
  const seen = new Set();
  return Object.freeze(payload.images.map((item, index) => {
    if (!item || typeof item.id !== 'string' || !/^[A-Za-z0-9_-]{8,128}$/.test(item.id) || item.position !== index + 1 || seen.has(item.id)) {
      throw new Error('The private upload order could not be verified.');
    }
    seen.add(item.id);
    return Object.freeze({id:item.id, position:item.position});
  }));
}


export function persistPending(storage, value) {
  const encoded = JSON.stringify(value);
  storage.setItem('byron-pending-submission', encoded);
  if (storage.getItem('byron-pending-submission') !== encoded) throw new Error('Submission reference was not retained.');
}

export function persistHostedJob(storage, id) {
  // Write mode first: a partial write must never label a hosted job as local.
  storage.setItem('byron-active-job-mode', 'hosted');
  storage.setItem('byron-active-job', id);
  if (storage.getItem('byron-active-job') !== id || storage.getItem('byron-active-job-mode') !== 'hosted') {
    throw new Error('Film recovery reference was not retained.');
  }
}

// A deliberately non-retrying pipeline: callbacks persist only opaque reference/stage.
export async function submitHosted(snapshot, capability, send, checkpoint) {
  if (!capability || typeof capability.csrf !== 'string') throw new Error('Reconnect to the private studio before submitting.');
  let stage = 'upload';
  let definiteRejection = false;
  let checkpointFailed = false;
  const headers = {'Idempotency-Key':snapshot.key, 'X-CSRF-Token':capability.csrf};
  try {
    try { checkpoint({key:snapshot.key, stage}); }
    catch { checkpointFailed = true; throw new Error('Browser recovery storage is unavailable. Nothing was uploaded or submitted. Enable session storage before trying again.'); }
    const form = new FormData();
    for (const file of snapshot.files) form.append('images', file, file.name);
    let response = await send('/api/uploads', {method:'POST', headers, body:form, credentials:'same-origin'});
    // Conflict can represent an existing operation; never unlock it as a new request.
    definiteRejection = [400,401,403,413,415,422,423].includes(response.status);
    let data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Private upload was not accepted.');
    definiteRejection = false;
    const images = uploadedImages(data, snapshot.files.length);
    stage = 'job';
    try { checkpoint({key:snapshot.key, stage}); }
    catch { checkpointFailed = true; throw new Error('Your upload was accepted, but its recovery checkpoint could not be saved. No film request was sent. Ask the administrator to review this submission reference.'); }
    response = await send('/api/jobs', {method:'POST', credentials:'same-origin',
      headers:{...headers, 'Content-Type':'application/json'},
      body:JSON.stringify({script:snapshot.script, images, voice_id:snapshot.voice.id})});
    // 402: over the signed budget; 423: another film is still being made. Both refuse before anything starts.
    definiteRejection = [400,401,402,403,413,415,422,423].includes(response.status);
    data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Film submission could not be confirmed.');
    definiteRejection = false;
    const id = safeJobId(data.id);
    if (!id) throw new Error('The film receipt could not be verified.');
    return {id};
  } catch (error) {
    const result = new Error(error.message || 'The private studio could not confirm this action.');
    result.uncertain = !definiteRejection && (!checkpointFailed || stage === 'job'); result.stage = stage;
    result.checkpointFailed = checkpointFailed;
    throw result;
  }
}

export async function recoverSubmission(key, send) {
  if (typeof key !== 'string' || !/^[A-Za-z0-9_-]{16,128}$/.test(key)) throw new Error('The submission reference is invalid.');
  const response = await send(`/api/submissions/${key}`, {method:'GET', credentials:'same-origin', cache:'no-store'});
  if (!response.ok) throw new Error('This submission is still unresolved. Ask the administrator to review the saved reference; do not submit again.');
  const payload = await response.json();
  const id = safeJobId(payload.id);
  if (!id) throw new Error('The recovered film reference could not be verified. Do not submit again.');
  return {id};
}

export function resumeCheckpoint(jobId, key, attempt) {
  if (!safeJobId(jobId) || typeof key !== 'string' || !/^[A-Za-z0-9_-]{16,128}$/.test(key) ||
      !Number.isInteger(attempt) || attempt < 1 || attempt > 3) throw new Error('The resume reference is invalid.');
  return Object.freeze({job_id:jobId,key,attempt});
}
export function persistResume(storage, value) {
  const checked=resumeCheckpoint(value.job_id,value.key,value.attempt);
  const encoded=JSON.stringify(checked);
  storage.setItem('byron-worker-resume',encoded);
  if(storage.getItem('byron-worker-resume')!==encoded) throw new Error('Resume recovery storage is unavailable. No request was sent.');
}
export function resumeResolved(checkpoint, job) {
  return !!checkpoint && job?.id===checkpoint.job_id && job.resume_attempt===checkpoint.attempt && job.resume_outcome_known===true;
}
export async function resumeHosted(jobId, capability, key, attempt, send, checkpoint) {
  const value=resumeCheckpoint(jobId,key,attempt);
  if(!(capability?.live || capability?.preview) || typeof capability.csrf!=='string') throw new Error('Reconnect to the accepted private studio before resuming.');
  checkpoint(value); // Must persist/read back BEFORE the single explicit POST.
  const response=await send(`/api/jobs/${jobId}/resume`,{method:'POST',credentials:'same-origin',
    headers:{'Idempotency-Key':key,'X-CSRF-Token':capability.csrf}});
  if(!response.ok) throw new Error('Resume could not be confirmed. Check this same film; do not submit a replacement.');
  const data=await response.json();
  if(data.id!==jobId || data.resume_attempt!==attempt) throw new Error('The resume receipt could not be verified. Check film status without resubmitting.');
  return data;
}
