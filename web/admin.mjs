const fields = document.getElementById('admin-fields');
const status = document.getElementById('admin-status');
const result = document.getElementById('admin-result');
let csrf = null;
let working = false;
let loginRequired = false;
let developerMode = false;
function setDeveloperMode(enabled) {
  developerMode = enabled === true;
  document.getElementById("gemini-section").hidden = !developerMode;
  const input = document.getElementById("gemini-key");
  input.disabled = !developerMode; input.value = "";
}
async function request(path, options = {}) {
  const response = await fetch(path, {...options, credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(15000)});
  if (response.status === 403 && path === '/api/admin/status') loginRequired = true;
  if (!response.ok) throw new Error('The action could not be confirmed. Check the connection and administrator access.');
  return response.json();
}
async function check() {
  if (working) return;
  fields.disabled = true; csrf = null; loginRequired = false; setDeveloperMode(false); document.getElementById('admin-recheck').textContent = 'Check connection';
  try {
    const data = await request('/api/admin/status');
    if (data.enabled !== true || typeof data.csrf_token !== 'string' || data.csrf_token.length < 32) throw new Error();
    if (!["vertex_eu", "gemini_developer"].includes(data.native_api_mode ?? "vertex_eu")) throw new Error();
    setDeveloperMode(data.native_api_mode === "gemini_developer");
    csrf = data.csrf_token; fields.disabled = false;
    status.textContent = 'Administrator connected. Film generation is not activated.';
    loadVoices();
  } catch {
    status.textContent = loginRequired ? 'Sign in with the permitted administrator account. Do not paste keys until connected.' : 'Administrator setup is not connected yet. Do not paste keys here.';
    if (loginRequired) document.getElementById('admin-recheck').textContent = 'Sign in';
  }
}
async function submit(path, body, input, success) {
  if (!csrf || working) return;
  working = true; fields.disabled = true; result.textContent = 'Saving or checking privately…';
  const encoded = body ? JSON.stringify(body) : undefined;
  if (input) input.value = '';
  try {
    await request(path, {method: 'POST', headers: {'Content-Type':'application/json', 'X-CSRF-Token':csrf}, body:encoded});
    result.textContent = success + ' Film generation is still not activated.';
  } catch { result.textContent = 'Could not confirm this action. Check administrator access, connection and key permissions, then try again. No media was generated.'; }
  finally { working = false; fields.disabled = !csrf; }
}
for (const provider of ['elevenlabs','fal']) {
  document.getElementById(`${provider}-form`).addEventListener('submit', event => {
    event.preventDefault(); const input = document.getElementById(`${provider}-key`);
    submit(`/api/admin/keys/${provider}`, {key:input.value.trim()}, input, 'Key saved privately. Use Test saved connection to verify it.');
  });
}
document.getElementById('gemini-form').addEventListener('submit', event => {
  event.preventDefault();
  if (!developerMode) return;
  const input = document.getElementById('gemini-key');
  submit('/api/admin/keys/gemini', {key:input.value.trim()}, input, 'Gemini Developer API key saved privately. No connection or paid request was made.');
});
function showVoiceList(data) {
  const list = document.getElementById('voice-list');
  if (!data || !Array.isArray(data.voices)) return;
  document.getElementById('voice-name-field').hidden = data.names_from_elevenlabs === true;
  document.getElementById('refresh-names').hidden = data.names_from_elevenlabs !== true || !data.voices.length;
  list.replaceChildren();
  if (!data.voices.length) { const empty = document.createElement('li'); empty.className = 'muted'; empty.textContent = 'No voices saved yet.'; list.append(empty); return; }
  for (const voice of data.voices) {
    const item = document.createElement('li'), label = document.createElement('span'), actions = document.createElement('span');
    label.textContent = voice.name + (voice.id === data.default_voice_id ? ' — default' : '');
    actions.className = 'admin-actions';
    const button = (text, path) => { const b = document.createElement('button'); b.type = 'button'; b.className = 'quiet small'; b.textContent = text;
      b.addEventListener('click', () => voiceChange(path, {voice_id: voice.id}, null, text === 'Remove' ? 'Voice removed from the list.' : 'Default narrator updated.')); return b; };
    if (voice.id !== data.default_voice_id) actions.append(button('Make default', '/api/admin/voices/default'));
    actions.append(button('Remove', '/api/admin/voices/remove'));
    item.append(label, actions); list.append(item);
  }
}
async function loadVoices() {
  try { showVoiceList(await request('/api/admin/voices')); } catch { /* list stays as last shown */ }
}
async function voiceChange(path, body, inputs, success) {
  if (!csrf || working) return;
  working = true; fields.disabled = true; result.textContent = 'Saving…';
  try {
    const response = await fetch(path, {method: 'POST', credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(20000),
      headers: {'Content-Type':'application/json', 'X-CSRF-Token':csrf}, body: body ? JSON.stringify(body) : undefined});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not save this change. Please try again.');
    for (const input of inputs || []) input.value = '';
    showVoiceList(data); result.textContent = success;
  } catch (error) { result.textContent = error.message || 'Could not save this change. Please try again.'; }
  finally { working = false; fields.disabled = !csrf; }
}
document.getElementById('voice-form').addEventListener('submit', event => {
  event.preventDefault();
  const id = document.getElementById('voice-id'), name = document.getElementById('voice-name');
  const body = {voice_id: id.value.trim()}; if (!document.getElementById('voice-name-field').hidden) body.name = name.value.trim();
  voiceChange('/api/admin/voices', body, [id, name], 'Voice saved. It now appears in the narrator list on the film page.');
});
document.getElementById('refresh-names').addEventListener('click', () => voiceChange('/api/admin/voices/refresh-names', null, null, 'Names updated from ElevenLabs.'));
for (const button of document.querySelectorAll('[data-test]')) button.addEventListener('click', () => submit(`/api/admin/test/${button.dataset.test}`, null, null, 'Read-only connection test passed. No media was generated.'));
document.getElementById('admin-recheck').addEventListener('click', () => { if (loginRequired) location.assign('/auth/login'); else check(); });
window.addEventListener('pagehide', () => { csrf = null; setDeveloperMode(false); for (const input of document.querySelectorAll('input')) input.value = ''; fields.disabled = true; });
check();
