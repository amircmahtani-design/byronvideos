import {exactCheckpoint,persistExact,exactFilmAction,exactActionResolved} from './exact-film.mjs';
import {MAX_SCRIPT_CHARACTERS, countWords, validateScript, validateImages, moveItem, safeJobId, safeDownloadURL, displayProgress, isPrivateDraft, completedFilmNote, refreshVoiceState, voiceLabel, skipNote, storyPreview} from './core.mjs';

import {hostedCapability, freezeSubmission, submitHosted, persistPending, persistHostedJob, recoverSubmission, resumeCheckpoint, persistResume, resumeResolved, resumeHosted} from './hosted.mjs';

const $ = id => document.getElementById(id);
const ui = Object.fromEntries(['exact-actions','exact-note','approve-film','renew-preview','renew-clean','clean-download','check-exact','voice','refresh-voices','voice-status','creator','inputs','script','script-count','images','paintings','empty-images','convert','connection','reconnect','error','job','job-title','job-message','job-note','progress','download','new-film','resume','resume-film','prototype-title','prototype-copy','studio-status','retry-upload','script-skips','title-seconds','stop-film','check','check-heading','check-title','check-start','check-end','check-meta','check-go','check-edit'].map(id => [id, $(id)]));
let paintings = [];
let checked = false; // The owner confirmed the "check this is the right story" panel for exactly these inputs.
let ready = false;
let activeJob = null;
let busy = false;
let timer = null;
let pollController = null;
let checking = false;
let voiceState = {voices: [], selectedId: '', removed: false};
let refreshingVoices = false;
let localRenderer = false;
let hosted = null;
let loginRequired = false;
let hostedJob = false;
let pendingSubmission = null;
let resumePending = null;
let exactPending=null, exactWorking=false, exactStatus=null, exactTimer=null;
let resumeWorking = false;
let eligibleResumeAttempt = null;
function showVoices() {
  ui.voice.replaceChildren();
  const empty = new Option(voiceState.removed ? 'Selected voice is no longer available' : 'Choose a saved voice', '');
  ui.voice.add(empty);
  for (const voice of voiceState.voices) ui.voice.add(new Option(voiceLabel(voice, voiceState.voices), voice.id));
  ui.voice.value = voiceState.removed ? '' : voiceState.selectedId;
  ui.voice.disabled = busy || localRenderer || !voiceState.voices.length;
}
async function refreshVoices() {
  if (busy || refreshingVoices) return;
  refreshingVoices = true; ui['refresh-voices'].disabled = true;
  try {
    const response = await timedFetch('/api/voices', {cache: 'no-store'});
    if (!response.ok) throw new Error();
    voiceState = refreshVoiceState(voiceState, await response.json());
    if (voiceState.error) throw new Error();
    showVoices();
    ui['voice-status'].textContent = localRenderer ? 'The local prototype uses its fixed narrator. Saved ElevenLabs voices are for hosted films once connected.' : voiceState.removed ? 'Your previous voice was removed. Choose another saved voice; no substitute was selected.' : voiceState.voices.length ? 'Choose a narrator by name. Your choice is fixed when a film starts.' : 'No narrator voices saved yet. They are added on the administrator page.';
  } catch {
    voiceState = refreshVoiceState(voiceState, null);
    ui['voice-status'].textContent = voiceState.voices.length ? 'Could not refresh voices. Your previous selection is kept. Try again shortly.' : 'ElevenLabs is not connected yet. Your saved voices will appear here after setup.';
  } finally { refreshingVoices = false; ui['refresh-voices'].disabled = busy; }
}
ui.voice.addEventListener('change', () => { if (!busy) voiceState = {...voiceState, selectedId: ui.voice.value, removed: false}; });
ui['refresh-voices'].addEventListener('click', refreshVoices);
refreshVoices();

async function timedFetch(url, options = {}, timeout = 15000) {
  const controller = new AbortController();
  const upstream = options.signal;
  const abort = () => controller.abort();
  if (upstream?.aborted) controller.abort();
  else upstream?.addEventListener('abort', abort, {once:true});
  const deadline = setTimeout(abort, timeout);
  try { return await fetch(url, {...options, signal:controller.signal}); }
  finally { clearTimeout(deadline); upstream?.removeEventListener('abort', abort); }
}


function showError(message = '') {
  ui.error.textContent = message;
  ui.error.hidden = !message;
}
function updateConvert() { ui.convert.disabled = !ready || busy; }
function updateCount() {
  const words = countWords(ui.script.value);
  ui['script-count'].textContent = `${words.toLocaleString()} ${words === 1 ? 'word' : 'words'} · ${ui.script.value.length.toLocaleString()} / ${MAX_SCRIPT_CHARACTERS.toLocaleString()} characters`;
  ui['script-skips'].textContent = skipNote(ui.script.value);
}
function showPaintings() {
  hideCheck(); // Order or count changed: confirm again.
  ui.paintings.replaceChildren();
  ui['empty-images'].hidden = paintings.length > 0;
  paintings.forEach((item, index) => {
    const row = document.createElement('li'); row.className = 'painting';
    const position = document.createElement('span'); position.className = 'painting-number'; position.textContent = `${index + 1}`;
    const image = document.createElement('img'); image.src = item.url; image.alt = `Painting ${index + 1}: ${item.file.name}`;
    const name = document.createElement('span'); name.className = 'painting-name'; name.textContent = item.file.name;
    const meta = document.createElement('span'); meta.className = 'painting-meta'; meta.textContent = `${(item.file.size / 1024 / 1024).toFixed(1)} MB · Original image`;
    name.append(meta);
    const controls = document.createElement('div'); controls.className = 'painting-controls';
    for (const [symbol, label, direction] of [['↑', 'Move earlier', -1], ['↓', 'Move later', 1], ['×', 'Remove', 0]]) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = symbol; button.setAttribute('aria-label', `${label}: ${item.file.name}`);
      button.disabled = busy || (direction === -1 && index === 0) || (direction === 1 && index === paintings.length - 1);
      button.addEventListener('click', () => {
        if (busy) return;
        if (direction) paintings = moveItem(paintings, index, direction);
        else { URL.revokeObjectURL(item.url); paintings.splice(index, 1); }
        showPaintings();
        const focusIndex = direction ? index + direction : Math.min(index, paintings.length - 1);
        ui.paintings.children[focusIndex]?.querySelector('button:not(:disabled)')?.focus();
        showError();
      });
      controls.append(button);
    }
    row.append(position, image, name, controls); ui.paintings.append(row);
  });
}
async function checkRenderer() {
  if (checking || busy) return;
  checking = true; ready = false; localRenderer = false; hosted = null; loginRequired = false; ui.reconnect.textContent = 'Check again'; showVoices(); updateConvert(); ui.reconnect.disabled = true;
  ui.connection.textContent = 'Checking the renderer connection…';
  try {
    const response = await timedFetch('/api/health', {cache: 'no-store'}, 10000);
    if (!response.ok) throw new Error();
    const health = await response.json();
    if (health.ready !== true || health.mode !== 'local') {
      const capabilities = await timedFetch('/api/creator-capabilities', {cache:'no-store', credentials:'same-origin'});
      if (capabilities.status === 401) loginRequired = true;
      if (!capabilities.ok) throw new Error();
      hosted = hostedCapability(await capabilities.json());
      if (!hosted) throw new Error();
      ready = true; showVoices(); await refreshVoices();
      ui['prototype-title'].textContent = hosted.preview ? 'Private first-film DRAFT preview.' : hosted.live ? 'Private studio — creator connected.' : 'Hosted test workspace connected — no live generation.';
      ui['prototype-copy'].textContent = hosted.preview ? 'Generate a private watermarked draft to watch and assess as a whole. It is not approved for publication. Your originals and narrator stay fixed.' : hosted.live ? 'Your original paintings are uploaded privately in the order shown. Your narrator is fixed when you start. Nothing is published automatically.' : 'This fixture workspace tests private upload order and your chosen narrator. It does not call paid providers or generate a live film. Nothing is published.';
      ui.connection.textContent = hosted.preview ? 'Private DRAFT workspace connected. Choose your narrator and make a preview.' : hosted.live ? 'Private studio connected. Choose your narrator and make your film.' : 'Test workspace connected. Choose a narrator to test the private submission workflow.';
      ui['studio-status'].hidden = hosted.live; // The live studio shows only the steps; the box returns if anything breaks.
      return;
    }
    ready = true; localRenderer = true; showVoices();
    ui['prototype-title'].textContent = 'Local prototype — renderer connected on this computer.';
    ui['prototype-copy'].textContent = 'Convert sends your files only to this local renderer. Nothing is published, and your paintings are not repainted.';
    ui.connection.textContent = 'Local renderer connected. Ready to make your film.';
  } catch {
    ui['prototype-title'].textContent = 'Studio preview — video generation API not connected yet.';
    ui['prototype-copy'].textContent = 'Your files stay in this browser; nothing is uploaded or published.';
    ui.connection.textContent = ['localhost','127.0.0.1','[::1]'].includes(location.hostname)
      ? 'Renderer unavailable. Start the local app using the README, then check again.'
      : 'Renderer unavailable. You can prepare your script and image order, but Convert stays disabled until the API is connected.';
    if (loginRequired) {
      ui.connection.textContent = 'Sign in with your permitted Google account to open the private creator.';
      ui.reconnect.textContent = 'Sign in';
    }
  } finally { if (!hosted?.live) ui['studio-status'].hidden = false; checking = false; ui.reconnect.disabled = false; updateConvert(); }
}
function stopPolling() { clearTimeout(timer); timer = null; pollController?.abort(); pollController = null; }
function saveActiveJob(id) { try { if (id) { sessionStorage.setItem('byron-active-job', id); sessionStorage.setItem('byron-active-job-mode', hostedJob ? 'hosted' : 'local'); } else { sessionStorage.removeItem('byron-active-job'); sessionStorage.removeItem('byron-active-job-mode'); } } catch { /* Private browsing may disable storage. */ } }
// A finished film stays downloadable from this browser for a day, even after the Google sign-in (1 hour) lapses.
const FINISHED_KEY = 'byron-finished-film', FINISHED_MS = 24 * 3600 * 1000;
let signInNeeded = false;
function saveFinished(id) { try { if (id) localStorage.setItem(FINISHED_KEY, JSON.stringify({id, at: Date.now()})); else localStorage.removeItem(FINISHED_KEY); } catch { /* Optional convenience only. */ } }
function loadFinished() {
  try { const value = JSON.parse(localStorage.getItem(FINISHED_KEY)); const id = safeJobId(value?.id);
    if (id && Number.isFinite(value.at) && Date.now() - value.at < FINISHED_MS) return id; localStorage.removeItem(FINISHED_KEY); } catch { /* Optional convenience only. */ }
  return null;
}
function askToSignIn(id) {
  stopPolling(); signInNeeded = true; activeJob = id;
  ui['job-title'].textContent = 'Please sign in again'; ui.progress.hidden = true; ui.download.hidden = true;
  ui['job-message'].textContent = 'Your Google sign-in timed out (it lasts an hour). Your film is safe.';
  ui['job-note'].textContent = 'Press Sign in, choose your Google account, and this film will be here again to download.';
  ui.resume.textContent = 'Sign in'; ui.resume.hidden = false;
}
function savePending(value) {
  if (value) persistPending(sessionStorage, value); // Must round-trip before each hosted POST.
  else { try { sessionStorage.removeItem('byron-pending-submission'); } catch { /* Keep any stale checkpoint safely locked. */ } }
}
function showUncertain(stage, reference, checkpointFailed = false) {
  ui.job.hidden = false; ui.download.hidden = true; ui['new-film'].hidden = true; ui.progress.hidden = true;
  if (stage === 'upload') {
    // The film request is only sent after the 'job' checkpoint, so nothing paid can have started: safe to try again.
    pendingSubmission = null; savePending(null); setBusy(false); ui.resume.hidden = true; ui['retry-upload'].hidden = false;
    ui['job-title'].textContent = 'Your paintings did not upload';
    ui['job-message'].textContent = 'Nothing was made and nothing was charged. Your story and paintings are still here.';
    ui['job-note'].textContent = 'Press Try again. If it happens again, please let Amir know.';
    ui.convert.textContent = 'Convert to film →';
    return;
  }
  pendingSubmission = {key:reference,stage};
  setBusy(true); ui.resume.hidden = false; ui['retry-upload'].hidden = true;
  ui['job-title'].textContent = 'Checking your film';
  ui['job-message'].textContent = checkpointFailed ? 'Your paintings were saved, but this browser could not keep its place. No film was started.' : 'Your film may already have started. It will never be made or charged twice.';
  ui['job-note'].textContent = 'Please do not press Convert again. Press Check film status in a minute. If it still shows this, please let Amir know (reference ' + reference.slice(0, 8) + ').';
  ui.convert.textContent = 'Please wait…';
}
function setBusy(value) { busy = value; showVoices(); ui['refresh-voices'].disabled = value || refreshingVoices; ui.inputs.disabled = value; ui.reconnect.disabled = value; updateConvert(); }
function showJob(id) {
  pendingSubmission = null; ui['retry-upload'].hidden = true;
  activeJob = id; setBusy(true);
  if (hostedJob) {
    try { persistHostedJob(sessionStorage, id); savePending(null); }
    catch { /* Known job remains usable in memory; retained pending checkpoint blocks reload. */ }
  } else saveActiveJob(id); ui.job.hidden = false; ui.download.hidden = true; ui['new-film'].hidden = true; ui.resume.hidden = true;
  ui['job-title'].textContent = 'Preparing your film'; ui['job-message'].textContent = 'Waiting for the renderer…'; ui.progress.removeAttribute('value');
}
function failedJob(message) {
  // Stay locked (and remembered across reloads) until the owner deliberately chooses to try again.
  stopPolling(); activeJob = null; setBusy(true);
  ui['job-title'].textContent = 'Your film stopped'; ui['job-message'].textContent = message;
  ui.progress.hidden = true;
  ui['job-note'].textContent = 'Convert is locked so nothing runs twice. Fix the problem named above, then press “I’ve fixed it — try again”. ' +
    (ui.script.value || paintings.length ? 'Your story and paintings are still here.' : 'You will need to paste your story and choose your paintings again.');
  ui['new-film'].textContent = 'I’ve fixed it — try again'; ui['new-film'].hidden = false;
  ui.convert.textContent = 'Locked — see below';
  if (message.startsWith('You stopped this film')) {
    ui['job-title'].textContent = 'Film stopped'; ui['new-film'].textContent = 'Make another film';
    ui['job-note'].textContent = 'Your story and paintings are still here. Correct them, then press Make another film.';
  }
}
async function pollJob() {
  if (!activeJob) return;
  const id = activeJob;
  eligibleResumeAttempt=null; ui['resume-film'].hidden=true;
  pollController = new AbortController();
  ui.resume.hidden = true; ui['stop-film'].hidden = true;
  try {
    const response = await timedFetch(`/api/jobs/${id}`, {cache: 'no-store', signal: pollController.signal});
    if (hostedJob && [401, 403].includes(response.status)) { askToSignIn(id); return; }
    if (response.status === 404 && hostedJob) throw new Error('This private film could not be found for your session. Ask the administrator to check it; no replacement will be submitted.');
    if (response.status === 404) { failedJob('This job is no longer available. The local server may have restarted.'); return; }
    if (!response.ok) throw new Error('The local renderer is not responding.');
    const job = await response.json();
    if (activeJob !== id) return;
    if (resumeResolved(resumePending, job)) {
      try { sessionStorage.removeItem('byron-worker-resume'); } catch { /* A stale marker stays safely blocked after reload. */ }
      resumePending = null;
    }
    eligibleResumeAttempt = job.status === 'HOLD' && job.can_resume === true && Number.isInteger(job.resume_attempt) && job.resume_attempt < 3 ? job.resume_attempt + 1 : null;
    ui['resume-film'].hidden = !hostedJob || eligibleResumeAttempt === null || !!resumePending;
    ui['resume-film'].disabled = resumeWorking;
    const progress = displayProgress(job.progress);
    ui.progress.hidden = false;
    if (progress === null) ui.progress.removeAttribute('value'); else ui.progress.value = progress;
    ui['job-message'].textContent = typeof job.message === 'string' ? job.message : 'The renderer is working on your film.';
    if (job.status === 'HOLD') {
      stopPolling(); ui['job-title'].textContent = 'Your film is paused'; ui.progress.hidden = true;
      ui['job-note'].textContent = 'This film is on hold. Ask the studio administrator to review it. Check film status only checks this same film; it never submits or pays again.';
      if (eligibleResumeAttempt !== null && !resumePending) ui['job-note'].textContent = 'The previous worker has stopped. Resume this film continues the same saved inputs and plan; it does not repeat completed paid requests.';
      if (resumePending) ui['job-note'].textContent = 'Resume reference: ' + resumePending.key + '. Its result needs confirmation. Check film status only; do not request another resume.';
      ui.resume.hidden = false;
    } else if (['complete','completed','done','succeeded'].includes(job.status) || isPrivateDraft(job)) {
      const download = safeDownloadURL(job.download_url, location.origin, id);
      if (!download) throw new Error('The renderer finished but did not provide a valid local download. Check film status again.');
      stopPolling(); ui['job-title'].textContent = isPrivateDraft(job) ? 'Your private DRAFT preview is ready' : 'Your film is ready'; ui.download.innerHTML = isPrivateDraft(job) ? 'Download DRAFT preview <span aria-hidden="true">↓</span>' : 'Download MP4 <span aria-hidden="true">↓</span>'; ui.download.download = isPrivateDraft(job) ? 'byron-DRAFT-' + id + '.mp4' : 'byron-film.mp4'; ui.progress.value = 100; ui.download.href = download; ui.download.hidden = false; ui['new-film'].textContent = 'Make another film'; ui['new-film'].hidden = false;
      ui['job-note'].textContent = completedFilmNote(job);
      // Keep the form locked until the operator chooses to make another film.
      if (isPrivateDraft(job)) await refreshExactFilm(id);
      else { saveActiveJob(null); activeJob = null; if (hostedJob) saveFinished(id); }
    } else if (['failed','error','cancelled'].includes(job.status)) failedJob(typeof job.message === 'string' ? job.message : 'The renderer reported an error.');
    else if (['queued','running','rendering','processing'].includes(job.status)) {
      ui['job-title'].textContent = job.status === 'queued' ? 'Your film is in the queue' : 'Making your film';
      ui['stop-film'].hidden = !hostedJob;
      timer = setTimeout(pollJob, 2000);
    } else throw new Error('The renderer returned an unfamiliar status. Check film status again; no new job will be started.');
  } catch (error) {
    if (error.name === 'AbortError' && pollController?.signal.aborted) return;
    ui['job-title'].textContent = 'Waiting to reconnect'; ui['job-message'].textContent = error.message || 'Connection interrupted. Your film may still be rendering.';
    ui['job-note'].textContent = 'Do not submit again. Use Check film status to reconnect to this same job.'; ui.resume.hidden = false;
  }
}
function showCheck() {
  const {title, start, end, words} = storyPreview(ui.script.value);
  const seconds = Number(ui['title-seconds'].value);
  ui['check-title'].textContent = !seconds ? 'No title card' : title ? `“${title}” with Lord Byron, ${seconds} second${seconds === 1 ? '' : 's'}` : 'No title line found, so no title card';
  ui['check-start'].textContent = start; ui['check-end'].textContent = end;
  ui['check-meta'].textContent = `${words} words read aloud · ${paintings.length} paintings · ${ui.voice.selectedOptions[0]?.textContent || 'narrator'}`;
  ui.check.hidden = false; ui['check-heading'].focus(); ui.check.scrollIntoView({block:'nearest'});
}
function hideCheck() { checked = false; ui.check.hidden = true; }
ui['check-go'].addEventListener('click', () => { checked = true; ui.creator.requestSubmit(); });
ui['check-edit'].addEventListener('click', () => { hideCheck(); ui.script.focus(); });
ui.inputs.addEventListener('input', hideCheck); ui.inputs.addEventListener('change', hideCheck);
ui['stop-film'].addEventListener('click', async () => {
  const id = activeJob;
  if (!id || !hosted || !confirm('Stop this film now? Anything already recorded or animated has been paid for; nothing more will be started.')) return;
  ui['stop-film'].disabled = true;
  try {
    const response = await timedFetch(`/api/jobs/${id}/stop`, {method:'POST', credentials:'same-origin', headers:{'X-CSRF-Token':hosted.csrf}}, 30000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The film could not be stopped just now. Try again in a moment.');
    stopPolling(); await pollJob();
  } catch (error) { showError(error.message); }
  finally { ui['stop-film'].disabled = false; }
});
ui.script.addEventListener('input', () => { updateCount(); showError(); });
ui.images.addEventListener('change', () => {
  if (busy) return;
  const files = [...ui.images.files]; ui.images.value = '';
  const problem = validateImages([...paintings.map(item => item.file), ...files], false);
  if (problem) { showError(problem); return; }
  paintings.push(...files.map(file => ({file, url: URL.createObjectURL(file)}))); showPaintings(); showError();
});
ui.reconnect.addEventListener('click', () => { if (loginRequired) location.assign('/auth/login'); else checkRenderer(); });
ui.download.addEventListener('click', async event => {
  // The link needs a live sign-in; check first so an expired one leads to Sign in, not a failed download.
  const id = loadFinished();
  if (!id || !ui.download.href.endsWith(`/api/jobs/${id}/video`)) return;
  event.preventDefault();
  try {
    const response = await timedFetch(`/api/jobs/${id}`, {cache: 'no-store'}, 15000);
    if ([401, 403].includes(response.status)) { askToSignIn(id); return; }
  } catch { /* Let the download itself report a network problem. */ }
  location.assign(ui.download.href);
});
ui.resume.addEventListener('click', async () => {
  if (signInNeeded) { if (activeJob) saveActiveJob(activeJob); location.assign('/auth/login'); return; }
  if (!pendingSubmission) return pollJob();
  if (pendingSubmission.stage !== 'job' || ui.resume.disabled) return;
  const reference = pendingSubmission.key; ui.resume.disabled = true;
  try {
    const {id} = await recoverSubmission(reference, (url, options) => timedFetch(url, options));
    hostedJob = true; showJob(id); await pollJob();
  } catch (error) {
    ui['job-message'].textContent = error.message || 'The outcome is still unresolved. No replacement was submitted.';
    ui['job-note'].textContent = 'Submission reference: ' + reference + '. Ask the administrator to review it. Check film status only looks up this reference; it never resubmits.';
  } finally { ui.resume.disabled = false; }
});
async function refreshExactFilm(id=activeJob) {
  if (!id || !hostedJob) return;
  clearTimeout(exactTimer);
  try {
    const query=exactPending?.job_id===id ? '?reference='+encodeURIComponent(exactPending.key) : '';
    const response=await timedFetch('/api/jobs/'+id+'/exact-film'+query,{cache:'no-store',credentials:'same-origin'});
    if (!response.ok) { ui['exact-actions'].hidden=true; return; }
    const status=await response.json();
    if(activeJob!==id)return;
    if (status.id!==id || status.enabled!==true || status.private_only!==true) throw Error('Invalid private film status.');
    exactStatus=status;
    if(exactActionResolved(exactPending,status)) {
      try { sessionStorage.removeItem('byron-exact-film-action'); } catch { /* Reload remains conservatively locked. */ }
      exactPending=null;
    }
    ui['exact-actions'].hidden=false;
    ui['job-note'].textContent='Private watermarked DRAFT preview. Watch the whole film before approving this specific film for a clean copy. Nothing is published.';
    ui['new-film'].hidden=!!exactPending || ['queued','HOLD'].includes(status.status);
    ui['approve-film'].hidden=status.can_approve!==true || !!exactPending;
    ui['renew-preview'].hidden=status.preview_view_available===true || !!exactPending;
    ui['renew-clean'].hidden=!status.clean_artifact_id || status.clean_view_available===true || !!exactPending;
    ui.download.hidden=status.preview_view_available!==true;
    ui['clean-download'].hidden=true;
    if(status.status==='ready' && status.clean_view_available===true && status.artifact_kind==='OWNER_APPROVED_EXACT_FILM' && status.download_url==='/api/jobs/'+id+'/approved-video') {
      ui['clean-download'].href=status.download_url;ui['clean-download'].download='byron-THIS-FILM-'+id+'.mp4';ui['clean-download'].hidden=false;
    }
    ui['exact-note'].textContent=exactPending ? 'Private action reference: '+exactPending.key+'. Check status only; this action will not retry automatically.' : status.status==='ready' ? 'Your approved clean copy of THIS film is ready. The original DRAFT remains available. This does not approve future style or publish anything.' : status.status==='queued' ? 'Preparing the clean copy from this film’s retained media. No paintings, narration or motion are regenerated.' : status.status==='HOLD' ? 'The clean-copy outcome needs reconciliation. Check this same film; do not approve or generate it again.' : 'After watching the whole DRAFT, you may approve THIS film for a private clean copy. This does not approve future films or publish anything. Renew access only reopens this retained artifact; it never regenerates it.';
    if(status.status==='queued') exactTimer=setTimeout(()=>refreshExactFilm(id),3000);
  } catch(error) { showError(error.message || 'Private film status could not be confirmed.'); }
}
async function takeExactAction(action,artifactId) {
  if(!activeJob || exactWorking || exactPending || !exactStatus || !artifactId) return;
  exactWorking=true;
  for(const name of ['approve-film','renew-preview','renew-clean']) ui[name].disabled=true;
  try {
    const response=await timedFetch('/api/creator-capabilities',{cache:'no-store',credentials:'same-origin'});
    if(!response.ok)throw Error('Sign in again before this private film action.');
    const capability=hostedCapability(await response.json());
    if(!capability || !(capability.live || capability.preview))throw Error('Reconnect to the accepted private studio.');
    await exactFilmAction({jobId:activeJob,artifactId,action,key:crypto.randomUUID(),csrf:capability.csrf},
      (url,options)=>timedFetch(url,options,120000),value=>{persistExact(sessionStorage,value);exactPending=value;});
    await refreshExactFilm();
  } catch(error) {
    showError((error.message || 'Private film action could not be confirmed.')+(exactPending ? ' Reference: '+exactPending.key+'. Check status only.' : ''));
    await refreshExactFilm();
  } finally {
    exactWorking=false;
    for(const name of ['approve-film','renew-preview','renew-clean']) ui[name].disabled=false;
  }
}
ui['approve-film'].addEventListener('click',()=>takeExactAction('approve',exactStatus?.preview_id));
ui['renew-preview'].addEventListener('click',()=>takeExactAction('renew',exactStatus?.preview_id));
ui['renew-clean'].addEventListener('click',()=>takeExactAction('renew',exactStatus?.clean_artifact_id));
ui['check-exact'].addEventListener('click',()=>refreshExactFilm());
ui['resume-film'].addEventListener('click', async () => {
  if (!activeJob || !hostedJob || resumeWorking || resumePending || eligibleResumeAttempt === null) return;
  const id = activeJob, attempt = eligibleResumeAttempt;
  resumeWorking = true; ui['resume-film'].disabled = true;
  try {
    const response = await timedFetch('/api/creator-capabilities', {cache:'no-store', credentials:'same-origin'});
    if (!response.ok) throw new Error('Sign in again before resuming this film.');
    const capability = hostedCapability(await response.json());
    const key = crypto.randomUUID();
    await resumeHosted(id, capability, key, attempt, (url, options) => timedFetch(url, options, 120000), value => {
      persistResume(sessionStorage,value); resumePending=value; ui['resume-film'].hidden=true;
    });
    await pollJob();
  } catch (error) {
    showError((error.message || 'Resume could not be confirmed. Check this same film without resubmitting.') + (resumePending ? ' Resume reference: ' + resumePending.key + '. Check film status only.' : ''));
    ui.resume.hidden=false;
  } finally { resumeWorking=false; ui['resume-film'].disabled=false; }
});
ui.creator.addEventListener('submit', async event => {
  event.preventDefault(); if (!ready || busy) return;
  const error = validateScript(ui.script.value) || validateImages(paintings.map(item => item.file));
  if (error) { showError(error); ui.error.scrollIntoView({block:'nearest'}); return; }
  if (hosted && !checked) { showCheck(); return; } // Nothing is uploaded or paid for until the owner confirms.
  checked = false; ui.check.hidden = true;
  if (hosted) {
    let snapshot;
    try { snapshot = freezeSubmission(ui.script.value, paintings.map(item => item.file), voiceState, crypto.randomUUID(), Number(ui['title-seconds'].value)); }
    catch (error) { showError(error.message); return; }
    showError(); setBusy(true); ui.convert.textContent = 'Uploading privately…';
    try {
      const {id} = await submitHosted(snapshot, hosted, (url, options) => timedFetch(url, options, 120000), savePending);
      hostedJob = true; showJob(id); ui.job.scrollIntoView({block:'start'}); await pollJob();
    } catch (error) {
      if (error.uncertain) showUncertain(error.stage, snapshot.key, error.checkpointFailed);
      else { savePending(null); setBusy(false); showError(error.message); }
    } finally { if (activeJob || !ui.download.hidden || !busy) ui.convert.textContent = 'Convert to film →'; }
    return;
  }
  showError(); setBusy(true); ui.convert.textContent = 'Sending to local renderer…';
  const form = new FormData(); form.append('script', ui.script.value);
  for (const item of paintings) form.append('images', item.file, item.file.name);
  let rejected = false;
  try {
    const response = await timedFetch('/api/jobs', {method:'POST', body:form}, 120000);
    // A definite validation rejection is retryable. An accepted/malformed or
    // interrupted response may already own a job, so never silently re-submit.
    rejected = response.status >= 400 && response.status < 500;
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The renderer could not accept these files. Check the script and images.');
    const id = safeJobId(data.id); if (!id) throw new Error('The renderer did not return a valid job reference. Please inspect the local server before trying again.');
    hostedJob = false; showJob(id); ui.job.scrollIntoView({block:'start'}); await pollJob();
  } catch (error) {
    if (!rejected) {
      // An interrupted POST may have been accepted. Never automatically resend it.
      showError('Connection interrupted while sending. The renderer may already have accepted this film. Check the local server before reloading or trying again; no second job was sent.');
      ui.convert.textContent = 'Check the local renderer';
    } else { setBusy(false); showError(error.message || 'The local renderer could not accept this film.'); }
  } finally { if (rejected || activeJob || !ui.download.hidden) ui.convert.textContent = 'Convert to film →'; }
});
ui['retry-upload'].addEventListener('click', () => {
  if (busy) return;
  ui['retry-upload'].hidden = true; ui.job.hidden = true; ui.creator.requestSubmit();
});
ui['new-film'].addEventListener('click', () => {
  if(exactPending || exactWorking)return;
  clearTimeout(exactTimer);exactStatus=null;ui['exact-actions'].hidden=true;
  stopPolling(); activeJob = null; saveActiveJob(null); saveFinished(null); signInNeeded = false; ui.resume.textContent = 'Check film status';
  setBusy(false); ui.job.hidden = true; ui.download.removeAttribute('href'); showError(); ui.script.focus(); checkRenderer();
  ui['new-film'].textContent = 'Make another film'; ui.convert.textContent = 'Convert to film →';
});
window.addEventListener('pagehide', () => {stopPolling();clearTimeout(exactTimer);});
window.addEventListener('pageshow', event => { if (event.persisted && activeJob) pollJob(); });
updateCount();
let restored = null;
let pending = null;
try { const value=JSON.parse(sessionStorage.getItem('byron-exact-film-action')); if(value) exactPending=exactCheckpoint(value); } catch { /* Invalid data cannot authorize a request. */ }
try {
  const value=JSON.parse(sessionStorage.getItem('byron-worker-resume'));
  if(value) resumePending=resumeCheckpoint(value.job_id,value.key,value.attempt);
} catch { /* Malformed recovery data cannot authorize a POST. */ }
try {
  const value = JSON.parse(sessionStorage.getItem('byron-pending-submission'));
  if (value && /^[A-Za-z0-9_-]{16,128}$/.test(value.key) && ['upload','job'].includes(value.stage)) pending = value;
} catch { /* Optional recovery only. */ }
try { restored = safeJobId(sessionStorage.getItem('byron-active-job')); hostedJob = sessionStorage.getItem('byron-active-job-mode') === 'hosted'; } catch { /* Optional recovery only. */ }
if(exactPending && !restored) { restored=exactPending.job_id;hostedJob=true; }
if (resumePending && !restored) { restored=resumePending.job_id; hostedJob=true; }
const finished = loadFinished();
if (finished && !restored) { restored = finished; hostedJob = true; }
if (pending?.stage === 'upload') { savePending(null); pending = null; } // Upload-only: no film was requested, nothing to recover.
if (pending) showUncertain(pending.stage, pending.key);
else if (restored) { showJob(restored); ui['job-note'].textContent = 'Reconnecting to your previous film. Your script and files are not saved in browser storage.'; pollJob(); }
else checkRenderer();
