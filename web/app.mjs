import {MAX_SCRIPT_CHARACTERS, countWords, validateScript, validateImages, moveItem, safeJobId, safeDownloadURL, displayProgress, completedFilmNote} from './core.mjs';

const $ = id => document.getElementById(id);
const ui = Object.fromEntries(['creator','inputs','script','script-count','images','paintings','empty-images','convert','connection','reconnect','error','job','job-title','job-message','job-note','progress','download','new-film','resume','prototype-title','prototype-copy'].map(id => [id, $(id)]));
let paintings = [];
let ready = false;
let activeJob = null;
let busy = false;
let timer = null;
let pollController = null;
let checking = false;

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
}
function showPaintings() {
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
  checking = true; ready = false; updateConvert(); ui.reconnect.disabled = true;
  ui.connection.textContent = 'Checking the renderer connection…';
  try {
    const response = await timedFetch('/api/health', {cache: 'no-store'}, 10000);
    if (!response.ok) throw new Error();
    const health = await response.json();
    if (health.ready !== true || health.mode !== 'local') throw new Error();
    ready = true;
    ui['prototype-title'].textContent = 'Local prototype — renderer connected on this computer.';
    ui['prototype-copy'].textContent = 'Convert sends your files only to this local renderer. Nothing is published, and your paintings are not repainted.';
    ui.connection.textContent = 'Local renderer connected. Ready to make your film.';
  } catch {
    ui['prototype-title'].textContent = 'Studio preview — video generation API not connected yet.';
    ui['prototype-copy'].textContent = 'Your files stay in this browser; nothing is uploaded or published.';
    ui.connection.textContent = ['localhost','127.0.0.1','[::1]'].includes(location.hostname)
      ? 'Renderer unavailable. Start the local app using the README, then check again.'
      : 'Renderer unavailable. You can prepare your script and image order, but Convert stays disabled until the API is connected.';
  } finally { checking = false; ui.reconnect.disabled = false; updateConvert(); }
}
function stopPolling() { clearTimeout(timer); timer = null; pollController?.abort(); pollController = null; }
function saveActiveJob(id) { try { if (id) sessionStorage.setItem('byron-active-job', id); else sessionStorage.removeItem('byron-active-job'); } catch { /* Private browsing may disable storage. */ } }
function setBusy(value) { busy = value; ui.inputs.disabled = value; ui.reconnect.disabled = value; updateConvert(); }
function showJob(id) {
  activeJob = id; setBusy(true); saveActiveJob(id); ui.job.hidden = false; ui.download.hidden = true; ui['new-film'].hidden = true; ui.resume.hidden = true;
  ui['job-title'].textContent = 'Preparing your film'; ui['job-message'].textContent = 'Waiting for the renderer…'; ui.progress.removeAttribute('value');
}
function failedJob(message) {
  stopPolling(); setBusy(false); saveActiveJob(null); activeJob = null;
  ui['job-title'].textContent = 'Your film could not be finished'; ui['job-message'].textContent = message;
  ui.progress.hidden = true; ui['job-note'].textContent = ui.script.value || paintings.length ? 'Your script and chosen paintings are still here. Correct the problem, then press Convert again.' : 'Paste your script and choose your paintings again, then press Convert.';
}
async function pollJob() {
  if (!activeJob) return;
  const id = activeJob;
  pollController = new AbortController();
  ui.resume.hidden = true;
  try {
    const response = await timedFetch(`/api/jobs/${id}`, {cache: 'no-store', signal: pollController.signal});
    if (response.status === 404) { failedJob('This job is no longer available. The local server may have restarted.'); return; }
    if (!response.ok) throw new Error('The local renderer is not responding.');
    const job = await response.json();
    if (activeJob !== id) return;
    const progress = displayProgress(job.progress);
    ui.progress.hidden = false;
    if (progress === null) ui.progress.removeAttribute('value'); else ui.progress.value = progress;
    ui['job-message'].textContent = typeof job.message === 'string' ? job.message : 'The renderer is working on your film.';
    if (['complete','completed','done','succeeded'].includes(job.status)) {
      const download = safeDownloadURL(job.download_url, location.origin, id);
      if (!download) throw new Error('The renderer finished but did not provide a valid local download. Check film status again.');
      stopPolling(); ui['job-title'].textContent = 'Your film is ready'; ui.progress.value = 100; ui.download.href = download; ui.download.hidden = false; ui['new-film'].hidden = false;
      ui['job-note'].textContent = completedFilmNote(job);
      // Keep the form locked until the operator chooses to make another film.
      saveActiveJob(null); activeJob = null;
    } else if (['failed','error','cancelled'].includes(job.status)) failedJob(typeof job.message === 'string' ? job.message : 'The renderer reported an error.');
    else if (['queued','running','rendering','processing'].includes(job.status)) {
      ui['job-title'].textContent = job.status === 'queued' ? 'Your film is in the queue' : 'Making your film';
      timer = setTimeout(pollJob, 2000);
    } else throw new Error('The renderer returned an unfamiliar status. Check film status again; no new job will be started.');
  } catch (error) {
    if (error.name === 'AbortError' && pollController?.signal.aborted) return;
    ui['job-title'].textContent = 'Waiting to reconnect'; ui['job-message'].textContent = error.message || 'Connection interrupted. Your film may still be rendering.';
    ui['job-note'].textContent = 'Do not submit again. Use Check film status to reconnect to this same job.'; ui.resume.hidden = false;
  }
}
ui.script.addEventListener('input', () => { updateCount(); showError(); });
ui.images.addEventListener('change', () => {
  if (busy) return;
  const files = [...ui.images.files]; ui.images.value = '';
  const problem = validateImages([...paintings.map(item => item.file), ...files], false);
  if (problem) { showError(problem); return; }
  paintings.push(...files.map(file => ({file, url: URL.createObjectURL(file)}))); showPaintings(); showError();
});
ui.reconnect.addEventListener('click', checkRenderer);
ui.resume.addEventListener('click', pollJob);
ui.creator.addEventListener('submit', async event => {
  event.preventDefault(); if (!ready || busy) return;
  const error = validateScript(ui.script.value) || validateImages(paintings.map(item => item.file));
  if (error) { showError(error); ui.error.scrollIntoView({block:'nearest'}); return; }
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
    showJob(id); ui.job.scrollIntoView({block:'start'}); await pollJob();
  } catch (error) {
    if (!rejected) {
      // An interrupted POST may have been accepted. Never automatically resend it.
      showError('Connection interrupted while sending. The renderer may already have accepted this film. Check the local server before reloading or trying again; no second job was sent.');
      ui.convert.textContent = 'Check the local renderer';
    } else { setBusy(false); showError(error.message || 'The local renderer could not accept this film.'); }
  } finally { if (rejected || activeJob || !ui.download.hidden) ui.convert.textContent = 'Convert to film →'; }
});
ui['new-film'].addEventListener('click', () => {
  stopPolling(); activeJob = null; saveActiveJob(null); setBusy(false); ui.job.hidden = true; ui.download.removeAttribute('href'); showError(); ui.script.focus(); checkRenderer();
});
window.addEventListener('pagehide', stopPolling);
window.addEventListener('pageshow', event => { if (event.persisted && activeJob) pollJob(); });
updateCount();
let restored = null;
try { restored = safeJobId(sessionStorage.getItem('byron-active-job')); } catch { /* Optional recovery only. */ }
if (restored) { showJob(restored); ui['job-note'].textContent = 'Reconnecting to your previous local job. Your script and files are not saved in browser storage.'; pollJob(); }
else checkRenderer();
