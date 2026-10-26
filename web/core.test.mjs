import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {countWords,validateScript,validateImages,moveItem,safeJobId,safeDownloadURL,displayProgress,formatDuration,completedFilmNote} from './core.mjs';
const image = (name='painting.png', type='image/png', size=100) => ({name,type,size});
test('word count handles whitespace without editing source', () => { assert.equal(countWords('  A\nnew   essay. '),3); assert.equal(countWords('  '),0); });
test('scripts are validated, never truncated', () => { const source='a'.repeat(4001); assert.match(validateScript(source),/nothing has been removed/); assert.equal(source.length,4001); assert.ok(validateScript('  ')); assert.equal(validateScript('This exact text.'),null); assert.equal(validateScript('Short.'),null); assert.match(validateScript('a '.repeat(451)),/technical limit of 450 words/); });
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
