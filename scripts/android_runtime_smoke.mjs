import assert from 'node:assert/strict';
import { brotliCompressSync } from 'node:zlib';

function defineMethod(target, name, implementation) {
  if (typeof target[name] === 'function') return;
  Object.defineProperty(target, name, {
    value: implementation,
    writable: true,
    configurable: true,
    enumerable: false,
  });
}

defineMethod(Array.prototype, 'toReversed', function toReversed() {
  return Array.from(this).reverse();
});
defineMethod(Array.prototype, 'toSorted', function toSorted(compareFn) {
  return Array.from(this).sort(compareFn);
});

const iteratorPrototype = Object.getPrototypeOf(
  Object.getPrototypeOf([][Symbol.iterator]()),
);
defineMethod(iteratorPrototype, 'map', function map(mapper) {
  const source = this;
  return (function* mappedIterator() {
    let index = 0;
    for (const value of source) yield mapper(value, index++);
  })();
});
defineMethod(iteratorPrototype, 'filter', function filter(predicate) {
  const source = this;
  return (function* filteredIterator() {
    let index = 0;
    for (const value of source) {
      if (predicate(value, index++)) yield value;
    }
  })();
});
defineMethod(iteratorPrototype, 'reduce', function reduce(reducer, initialValue) {
  let accumulator = initialValue;
  let hasAccumulator = arguments.length > 1;
  let index = 0;
  for (const value of this) {
    if (!hasAccumulator) {
      accumulator = value;
      hasAccumulator = true;
    } else {
      accumulator = reducer(accumulator, value, index);
    }
    index += 1;
  }
  if (!hasAccumulator) throw new TypeError('Reduce of empty iterator with no initial value');
  return accumulator;
});
defineMethod(iteratorPrototype, 'some', function some(predicate) {
  let index = 0;
  for (const value of this) {
    if (predicate(value, index++)) return true;
  }
  return false;
});
defineMethod(iteratorPrototype, 'toArray', function toArray() {
  return Array.from(this);
});
defineMethod(Map.prototype, 'getOrInsert', function getOrInsert(key, defaultValue) {
  if (this.has(key)) return this.get(key);
  this.set(key, defaultValue);
  return defaultValue;
});
defineMethod(Map.prototype, 'getOrInsertComputed', function getOrInsertComputed(key, callback) {
  if (this.has(key)) return this.get(key);
  const value = callback(key);
  this.set(key, value);
  return value;
});

const adapters = await import('@dan-uni/dan-any/adapters');
const pureCore = await import('@dan-uni/dan-any/core/main/pure');
const { ConverterFactory } = await import('opencc-js/core');
const { default: simplifiedToTraditionalCharacters } =
  await import('opencc-js/dict/STCharacters');
const { default: toSimplifiedChinese } = await import('opencc-js/to/cn');
const { default: toTraditionalChinese } = await import('opencc-js/to/tw');
const { default: brotliDecompress } = await import('brotli/decompress.js');
const pako = await import('pako');
const { default: fetch } = await import('node-fetch');
const { HttpsProxyAgent } = await import('https-proxy-agent');

for (const exportName of [
  'ArtplayerMetadata',
  'BiliXmlMetadata',
  'DanuniJsonMetadata',
  'DdplayMetadata',
  'VodMetadata',
]) {
  assert.equal(typeof adapters[exportName]?.type, 'string');
}

const parsedBili = adapters.BiliCommonParser(
  { $UniDB: { DMIDGenerator: () => 'smoke-dmid' } },
  {
    id: 1n,
    idStr: '1',
    oid: 1n,
    progress: 1000,
    mode: 1,
    fontsize: 25,
    color: 0xffffff,
    midHash: '0123456789abcdef',
    content: 'smoke',
    ctime: 1n,
    weight: 1,
    pool: 0,
    attr: 0,
  },
);
assert.equal(parsedBili.mode, 'Normal');

const db = new pureCore.UniDB().init();
const chunk = db.makeChunk({});
chunk.upsertDanmakus([
  {
    SOID: 'smoke-source',
    progress: 1000,
    mode: 'Normal',
    fontsize: 25,
    color: 0xffffff,
    senderID: 'smoke-sender',
    content: 'smoke-content',
    ctime: new Date(0),
    weight: 1,
    pool: 'Def',
    attr: ['Protect'],
    platform: 'smoke',
    extra: null,
  },
], true);
assert.equal(chunk.$count, 1);

const toSimplified = ConverterFactory(toSimplifiedChinese);
const toTraditional = ConverterFactory(
  [simplifiedToTraditionalCharacters],
  toTraditionalChinese,
);
assert.equal(toSimplified('漢語'), '汉语');
assert.equal(toTraditional('汉语'), '漢語');

const payload = Buffer.from('danmu-api-android-runtime-smoke', 'utf8');
const compressed = brotliCompressSync(payload);
assert.equal(Buffer.from(brotliDecompress(compressed)).toString('utf8'), payload.toString('utf8'));
assert.equal(Buffer.from(pako.inflate(pako.deflate(payload))).toString('utf8'), payload.toString('utf8'));
assert.equal(typeof fetch, 'function');
assert.equal(typeof HttpsProxyAgent, 'function');

console.log('Android runtime feature smoke: OK');
