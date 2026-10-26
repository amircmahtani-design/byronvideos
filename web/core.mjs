export const MAX_SCRIPT_CHARACTERS = 4000;
export const MAX_SCRIPT_WORDS = 450;
export const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
export const MAX_IMAGES = 5;
export const MIN_IMAGES = 4;

export function countWords(text) {
  return text.trim() ? text.trim().split(/\s+/u).length : 0;
}

export function validateScript(text) {
  if (!text.trim()) return 'Paste the words you want the narrator to read.';
  if (text.length > MAX_SCRIPT_CHARACTERS) return `Your script has ${text.length.toLocaleString()} characters. Please shorten it to ${MAX_SCRIPT_CHARACTERS.toLocaleString()} or fewer; nothing has been removed.`;
  if (countWords(text) > MAX_SCRIPT_WORDS) return 'This prototype has a technical limit of 450 words per film. Nothing has been removed.';
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

export function completedFilmNote(job) {
  const duration = formatDuration(job.duration_seconds);
  const advisory = typeof job.duration_advisory === 'string' ? job.duration_advisory.slice(0, 500) : '';
  return [duration ? `Duration ${duration}.` : '', advisory,
    'Download your MP4 and watch it before publishing. This prototype does not publish your film.'].filter(Boolean).join(' ');
}
