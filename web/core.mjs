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
const unemphasise = line => line.trim().replace(/^[*_~]+|[*_~]+$/gu, '').trim();
function isByline(line) {
  line = unemphasise(line);
  return /^(?:written\s+|words\s+)?by\s+\S/iu.test(line) && line.split(/\s+/u).length <= 8;
}
function isHeading(line, following) {
  line = unemphasise(line);
  if (line.startsWith('#') || isByline(line)) return true;
  return line.split(/\s+/u).length <= 15 && !/[.!?…:;,"'”’)]$/u.test(line) && (!following.trim() || isByline(following));
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
    heading.push(unemphasise(lines[i].trim().replace(/^#+/u, ''))); lines[i] = '';
  }
  let links = 0;
  for (const line of lines) {
    const [count, only] = linkOnly(line);
    links += only ? count : (line.match(WEB_ADDRESS) || []).length;
  }
  return {heading, links};
}
// The studio's signed setup lasts up to a year; remind the owner from 60 days before it runs out.
export const SETUP_REMINDER_DAYS = 60, SETUP_URGENT_DAYS = 14;
export function setupReminder(expiresAt, now = Date.now() / 1000) {
  if (!Number.isInteger(expiresAt)) return null;
  const days = Math.max(0, Math.floor((expiresAt - now) / 86400));
  if (days > SETUP_REMINDER_DAYS) return null;
  const date = new Date(expiresAt * 1000).toLocaleDateString('en-GB', {day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC'});
  const when = days === 0 ? 'today' : days === 1 ? 'tomorrow' : `in ${days} days`;
  return {urgent: days <= SETUP_URGENT_DAYS, days,
    title: `Studio setup renewal due: it expires ${when}, on ${date}.`,
    text: 'After that date the studio will stop opening until it is renewed. Renewing takes about five minutes: follow ' +
      '“READ ME – what this is.txt” in your “Byron owner key” folder, or ask Claude to “renew the Byron studio setup”.'};
}
// What the "check before converting" panel shows: the title card line and the narration's first and last words.
export function storyPreview(script, edge = 14) {
  const {heading} = storySkips(script);
  const title = heading.find(line => !/^(?:written\s+|words\s+)?by\s+\S/iu.test(line)) || '';
  const lines = script.replace(MARKDOWN_LINK, '$1').replace(/\r\n?/gu, '\n').split('\n');
  let removed = 0;
  const story = lines.filter(line => {
    if (!line.trim()) return true;
    if (removed < heading.length && unemphasise(line.trim().replace(/^#+/u, '')) === heading[removed]) { removed++; return false; }
    return !linkOnly(line)[1];
  }).join(' ').replace(WEB_ADDRESS, '').split(/\s+/u).filter(Boolean);
  const start = story.slice(0, edge).join(' ') + (story.length > edge ? ' …' : '');
  const end = story.length > edge ? '… ' + story.slice(-edge).join(' ') : story.join(' ');
  return {title, start, end, words: story.length};
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

// "What's left" on the studio page: the studio's own spending limit, ElevenLabs credits and the fal balance.
// Each row stands alone; anything missing or odd reads as not available instead of a wrong number.
const LEFT_TEXT = Object.freeze({
  not_set_up: 'Not set up yet. A billing key is needed to show this.',
  no_permission: 'Not available. The billing key lacks billing permission.',
  unavailable: 'Not available right now. Try again in a moment.',
});
const leftCount = n => Number(n).toLocaleString('en-GB');
const leftMoney = (amount, currency = 'USD') => amount.toLocaleString('en-GB', {style: 'currency', currency, currencyDisplay: 'narrowSymbol'});
const leftDay = seconds => new Date(seconds * 1000).toLocaleDateString('en-GB', {day: 'numeric', month: 'short', year: 'numeric'});
const wholeCents = n => Number.isInteger(n) && n >= 0;
export function describeRemaining(data) {
  const studio = data?.studio, el = data?.elevenlabs, fal = data?.fal;
  const state = info => LEFT_TEXT[info?.state] || LEFT_TEXT.unavailable;
  const studioText = studio?.state === 'available' && wholeCents(studio.left_cents) && wholeCents(studio.total_cents) && studio.left_cents <= studio.total_cents
    ? `${leftMoney(studio.left_cents / 100)} left of ${leftMoney(studio.total_cents / 100)}` : LEFT_TEXT.unavailable;
  const elText = el?.state === 'available' && [el.credits_remaining, el.credits_limit].every(wholeCents)
    ? `${leftCount(el.credits_remaining)} credits left of ${leftCount(el.credits_limit)}`
      + (Number.isInteger(el.resets_at) && el.resets_at > 0 ? `, renews ${leftDay(el.resets_at)}` : '') : state(el);
  const currency = typeof fal?.currency === 'string' && /^[A-Z]{3}$/.test(fal.currency) ? fal.currency : 'USD';
  const falText = fal?.state === 'available' && Number.isFinite(fal.balance) ? `${leftMoney(fal.balance, currency)} left` : state(fal);
  const checked = Number.isInteger(data?.checked_at) && data.checked_at > 0
    ? `Checked ${new Date(data.checked_at * 1000).toLocaleString('en-GB', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'})}.` : '';
  return {studio: studioText, elevenlabs: elText, fal: falText, checked};
}
