// WP2 scaling math tests. Extracts the marked pure-function block from the
// built page and asserts behavior. Run: node tests/test_scale_math.mjs
import { readFileSync } from 'node:fs';
import { execSync } from 'node:child_process';
import assert from 'node:assert/strict';

execSync('python3 generate.py', { stdio: 'pipe' });
const html = readFileSync('output/index.html', 'utf8');
const m = html.match(/\/\* ── Fraction helpers.*?\*\/([\s\S]*?)\/\* ── End fraction helpers/);
assert.ok(m, 'fraction helper markers found in built page');
const api = new Function(`${m[1]}; return {frMul, frLimitDen, frFmt, parseHave, ingLineText};`)();

// frMul: exact multiplication, reduced
assert.deepEqual(api.frMul([1, 2], [2, 3]), [1, 3]);
assert.deepEqual(api.frMul([4, 1], [1, 1]), [4, 1]);

// frLimitDen: nearest with denominator <= 8, tie -> smaller denominator
assert.deepEqual(api.frLimitDen([1, 3], 8), [1, 3]);
assert.deepEqual(api.frLimitDen([3, 8], 8), [3, 8]);
assert.deepEqual(api.frLimitDen([7, 16], 8), [1, 2]);
assert.deepEqual(api.frLimitDen([9, 2], 8), [9, 2]);
assert.deepEqual(api.frLimitDen([1, 7], 8), [1, 8]);  // cooking denominators: nearest is 1/8
// tiny values clamp to 1/8, never 0
assert.deepEqual(api.frLimitDen([1, 40], 8), [1, 8]);

// frFmt: mixed numbers, ASCII fractions
assert.equal(api.frFmt([1, 2]), '1/2');
assert.equal(api.frFmt([5, 2]), '2 1/2');
assert.equal(api.frFmt([4, 1]), '4');
assert.equal(api.frFmt([0, 1]), '0');
assert.equal(api.frFmt([-1, 2]), '-1/2');
assert.equal(api.frFmt([-5, 2]), '-2 1/2');

// parseHave: accepts int, decimal, fraction; rejects junk
assert.deepEqual(api.parseHave('2'), [2, 1]);
assert.deepEqual(api.parseHave('1.5'), [3, 2]);
assert.deepEqual(api.parseHave('1,5'), [3, 2]);
assert.deepEqual(api.parseHave('1/2'), [1, 2]);
assert.deepEqual(api.parseHave('2.05'), [41, 20]);  // exact, no float garbage
assert.deepEqual(api.parseHave('0.25'), [1, 4]);
assert.equal(api.parseHave(''), null);
assert.equal(api.parseHave('abc'), null);
assert.equal(api.parseHave('0'), null);

// ingLineText: display pipeline
const unit = { q: [1, 1], q2: null, u1: 'taza', un: 'tazas', r1: 'arroz', rn: 'arroz', pre: '' };
// factor 1 -> original byte-identical
assert.equal(api.ingLineText(unit, '1 taza arroz', [1, 1]), '1 taza arroz');
// 1 taza x 9/2 = 4 1/2 tazas
assert.equal(api.ingLineText(unit, '1 taza arroz', [9, 2]), '4 1/2 tazas arroz');
// plural -> singular when displayed value is 1
const cnt = { q: [2, 1], q2: null, u1: '', un: '', r1: 'tomate medianos', rn: 'tomates medianos', pre: '' };
assert.equal(api.ingLineText(cnt, '2 tomates medianos', [1, 2]), '1 tomate medianos');
// range scales both ends
const rng = { q: [8, 1], q2: [10, 1], u1: '', un: '', r1: 'hoja de curry', rn: 'hojas de curry', pre: '' };
assert.equal(api.ingLineText(rng, '8–10 hojas de curry', [2, 1]), '16–20 hojas de curry');
// dientes joins with " de "
const di = { q: [4, 1], q2: null, u1: 'diente', un: 'dientes', r1: 'ajo picados', rn: 'ajo picados', pre: '' };
assert.equal(api.ingLineText(di, '4 dientes de ajo picados', [1, 4]), '1 diente de ajo picados');
// mid-line prefix rejoined
const mid = { q: [1, 2], q2: null, u1: '', un: '', r1: 'lima', rn: 'limas', pre: 'Zumo de ' };
assert.equal(api.ingLineText(mid, 'Zumo de 1/2 lima', [2, 1]), 'Zumo de 1 lima');
// prose unscaled
assert.equal(api.ingLineText({ q: null, q2: null, u1: null, un: null, r1: 'Sal al gusto', rn: 'Sal al gusto', pre: '' }, 'Sal al gusto', [9, 2]), 'Sal al gusto');
// quantized -> ≈ prefix (1/3 x 1/2 = 1/6 stays; 1/6 x 7 = 7/6 -> 1 1/8 ≈)
const qq = { q: [1, 6], q2: null, u1: 'cdta', un: 'cdtas', r1: 'sal', rn: 'sal', pre: '' };
assert.equal(api.ingLineText(qq, '1/6 cdta sal', [7, 1]), '1 1/6 cdtas sal');  // 7/6 exact with den 6

console.log('test_scale_math: all assertions passed');
