import test from 'node:test';
import assert from 'node:assert/strict';
import * as m from './model.mjs';
test('workflow invariants and boundary cases',()=>{
assert.equal(m.retrieve('hardware auction risk',m.defaults.notes)[0].id,'hardware');assert.equal(m.retrieve('unrelated dinosaurs',m.defaults.notes).length,0);assert.deepEqual(m.sentences('One sentence. Two sentences!'),['One sentence.','Two sentences!']);assert.equal(m.run({...m.defaults,question:'unrelated dinosaurs'}).artifact.parts.length,0);
});
