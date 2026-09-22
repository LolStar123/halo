import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {chunks,retrieve,sentences,extractCue} from './model.mjs';
const packs=JSON.parse(readFileSync(new URL('./data/meetings.json',import.meta.url))).packs;
test('four populated meeting packs produce source-backed results',()=>{assert.equal(packs.length,4);for(const p of packs){assert.ok(chunks(p.docs).length>=8);const results=retrieve(p.docs,p.question);assert.ok(results.length);assert.ok(results.every(r=>p.docs.some(d=>d.text.includes(r.text))));assert.ok(extractCue(results).length>40);}});
test('new documents change retrieval; unknown questions do not invent answers',()=>{const docs=[{title:'Test',text:'# Orchard\n\nThe orchard review needs seven crates of apples.'}];assert.equal(retrieve(docs,'orchard crates')[0].heading,'Orchard');assert.equal(retrieve(docs,'submarine torque').length,0);assert.equal(extractCue([]),'');});
test('sentence navigation preserves punctuation and all text',()=>{const text='Dr. Smith reviewed the report. The estimate is 3.5 hours. Is that enough?';const parts=sentences(text);assert.equal(parts.join(' '),text);assert.ok(parts.length>=2);assert.ok(parts[0].includes('Dr. Smith'));});
