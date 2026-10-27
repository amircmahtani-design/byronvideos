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
document.getElementById('default-voice-form').addEventListener('submit', event => {
  event.preventDefault(); const input = document.getElementById('default-voice');
  submit('/api/admin/default-voice', {voice_id:input.value.trim()}, input, 'Accessible voice checked and saved as the default for new films.');
});
for (const button of document.querySelectorAll('[data-test]')) button.addEventListener('click', () => submit(`/api/admin/test/${button.dataset.test}`, null, null, 'Read-only connection test passed. No media was generated.'));
document.getElementById('admin-recheck').addEventListener('click', () => { if (loginRequired) location.assign('/auth/login'); else check(); });
window.addEventListener('pagehide', () => { csrf = null; setDeveloperMode(false); for (const input of document.querySelectorAll('input')) input.value = ''; fields.disabled = true; });
check();
