export const MAX_SCRIPT_CHARACTERS = 100000;  // Mirrors byron_video/engine.py; generous on purpose.
export const MAX_SCRIPT_WORDS = 20000;
export const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
export const MAX_IMAGES = 5;
export const MIN_IMAGES = 4;

// Pure state transition: a failed refresh keeps the prior catalog and selection.
export function refreshVoiceState(previous, payload) {
  if (!payload || !Array.isArray(payload.voices)) return {...previous, error: true};
  const voices = payload.voices.map(v => {
    if (!v || !/^[A-Za-z0-9_-]{8,80}$/.test(v.id) || typeof v.name !== 'string' || !v.name.trim()) throw new Error('Invalid voice catalog');
    return {id: v.id, name: v.name};
  });
  const selectedId = previous.selectedId || payload.default_voice_id || '';
  return {voices, selectedId, error: false, removed: !!selectedId && !voices.some(v => v.id === selectedId)};
}

export function voiceLabel(voice, voices) {
  return voices.filter(v => v.name === voice.name).length > 1 ? `${voice.name} (${voice.id})` : voice.name;
}

export function snapshotVoice(state) {
  const voice = state.voices.find(v => v.id === state.selectedId);
  if (!voice || state.removed) throw new Error('Choose an available saved voice.');
  return Object.freeze({...voice});
}

export function countWords(text) {
  return text.trim() ? text.trim().split(/\s+/u).length : 0;
}

// Mirrors byron_video/spoken_text.py: heading lines (title, byline) and web links are never read aloud.
const MARKDOWN_LINK = /(?<!!)\[([^\[\]\r\n]+)\]\((https?:\/\/[^\s()]+)\)/gu;
const WEB_ADDRESS = /(?:https?:\/\/|www\.)\S+/giu;
const DOMAIN = /\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|co|uk|io|info|blog|me|art|eu|es|ae)\b(?:\/\S*)?/giu;
function linkOnly(line) {
  const withoutUrls = line.replace(WEB_ADDRESS, '');
  const count = (line.match(WEB_ADDRESS) || []).length + (withoutUrls.match(DOMAIN) || []).length;
  const rest = withoutUrls.replace(DOMAIN, '');
  const trimmed = rest.replace(/^[\s:\-–—|·]+|[\s:\-–—|·]+$/gu, '');
  const label = trimmed.split(/\s+/u).filter(Boolean).length <= 4 && rest.trimEnd().endsWith(':');
  return [count, count > 0 && (!/\p{L}/u.test(trimmed) || label)];
}
function isHeading(line, following) {
  line = line.trim();
  if (line.startsWith('#') || (/^(?:written\s+|words\s+)?by\s+\S/iu.test(line) && line.split(/\s+/u).length <= 8)) return true;
  return line.split(/\s+/u).length <= 15 && !/[.!?…:;,"'”’)]$/u.test(line) && !following.trim();
}
export function storySkips(script) {
  const lines = script.replace(MARKDOWN_LINK, '$1').replace(/\r\n?/gu, '\n').split('\n');
  const heading = [];
  let seen = 0;
  for (let i = 0; i < lines.length && seen < 4; i++) {
    if (!lines[i].trim()) continue;
    seen++;
    if (linkOnly(lines[i])[1]) continue;
    if (!isHeading(lines[i], lines[i + 1] || '')) break;
    heading.push(lines[i].trim().replace(/^#+/u, '').trim()); lines[i] = '';
  }
  let links = 0;
  for (const line of lines) {
    const [count, only] = linkOnly(line);
    links += only ? count : (line.match(WEB_ADDRESS) || []).length;
  }
  return {heading, links};
}
export function skipNote(script) {
  const {heading, links} = storySkips(script);
  const short = text => `“${text.length > 50 ? text.slice(0, 47) + '…' : text}”`;
  const parts = [];
  if (heading.length) parts.push(`the heading ${heading.map(short).join(', ')}`);
  if (links) parts.push(links === 1 ? 'one web link' : `${links} web links`);
  return parts.length ? `Not read aloud or subtitled: ${parts.join(' and ')}.` : '';
}

export function validateScript(text) {
  if (!text.trim()) return 'Paste the words you want the narrator to read.';
  if (text.length > MAX_SCRIPT_CHARACTERS) return `Your script has ${text.length.toLocaleString()} characters. Please shorten it to ${MAX_SCRIPT_CHARACTERS.toLocaleString()} or fewer; nothing has been removed.`;
  if (countWords(text) > MAX_SCRIPT_WORDS) return `The studio accepts up to ${MAX_SCRIPT_WORDS.toLocaleString()} words per film. Nothing has been removed.`;
  return null;
}

export function validateImages(files, complete = true) {
  if (files.length > MAX_IMAGES) return 'Choose no more than five paintings.';
  if (complete && files.length < MIN_IMAGES) return 'Add four or five paintings to make your film.';
  for (const file of files) {
    if (!/\.(png|jpe?g)$/iu.test(file.name) || !['image/png', 'image/jpeg'].includes(file.type)) return `${file.name}: please choose a PNG or JPEG image.`;
    if (file.size === 0) return `${file.name}: this file is empty. Please choose it again.`;
    if (file.size > MAX_IMAGE_BYTES) return `${file.name}: each painting must be 20 MB or smaller.`;
  }
  return null;
}

export function moveItem(items, index, direction) {
  const destination = index + direction;
  if (!Number.isInteger(index) || ![-1, 1].includes(direction) || index < 0 || index >= items.length || destination < 0 || destination >= items.length) return [...items];
  const result = [...items];
  [result[index], result[destination]] = [result[destination], result[index]];
  return result;
}

export function safeJobId(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{8,80}$/u.test(value) ? value : null;
}

export function safeDownloadURL(value, origin, jobId) {
  try {
    const url = new URL(value, origin);
    if (url.origin !== origin || url.username || url.password || url.search || url.hash || url.pathname !== `/api/jobs/${jobId}/video`) return null;
    return url.href;
  } catch { return null; }
}

export function displayProgress(value) {
  return typeof value === 'number' && Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : null;
}

export function formatDuration(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0) return null;
  const rounded = Math.round(seconds);
  return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, '0')}`;
}

export function isPrivateDraft(job) {
  return job.status === 'draft_preview_ready' && job.artifact_kind === 'PRIVATE_DRAFT_PREVIEW' && job.draft === true;
}

export function completedFilmNote(job) {
  if (isPrivateDraft(job)) return [formatDuration(job.duration_seconds) ? `Duration ${formatDuration(job.duration_seconds)}.` : '',
    'Private watermarked DRAFT preview. Watch the whole film to assess its style and content. It is not approved for publication; no approval or publishing action is available here.'].filter(Boolean).join(' ');
  const duration = formatDuration(job.duration_seconds);
  const advisory = typeof job.duration_advisory === 'string' ? job.duration_advisory.slice(0, 500) : '';
  return [duration ? `Duration ${duration}.` : '', advisory,
    'Download your MP4 and watch it before publishing. This prototype does not publish your film.'].filter(Boolean).join(' ');
}
