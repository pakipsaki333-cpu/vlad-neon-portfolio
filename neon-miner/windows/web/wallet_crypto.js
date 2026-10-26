/* NeonMiner Wallet — криптография Monero на чистом JS (без зависимостей, без сети).
 *
 * Реализовано по исходникам monero-project/monero:
 *   src/crypto/crypto.cpp           generate_keys(): sc_reduce32 + ge_scalarmult_base
 *   src/cryptonote_basic/account.cpp generate(): viewkey = sc_reduce32(keccak(spendkey))
 *   src/mnemonics/electrum-words.cpp bytes_to_words(): 4 байта -> 3 слова + CRC32-слово
 *   src/mnemonics/english.h         wordlist из 1626 слов, unique_prefix_length = 3
 *
 * Всё считается на BigInt. Ни один вызов не делает сетевых запросов.
 */
(function (global) {
  'use strict';

  /* ------------------------------ Keccak-256 ------------------------------ */
  const MASK = (1n << 64n) - 1n;
  const RC = [
    0x0000000000000001n, 0x0000000000008082n, 0x800000000000808an, 0x8000000080008000n,
    0x000000000000808bn, 0x0000000080000001n, 0x8000000080008081n, 0x8000000000008009n,
    0x000000000000008an, 0x0000000000000088n, 0x0000000080008009n, 0x000000008000000an,
    0x000000008000808bn, 0x800000000000008bn, 0x8000000000008089n, 0x8000000000008003n,
    0x8000000000008002n, 0x8000000000000080n, 0x000000000000800an, 0x800000008000000an,
    0x8000000080008081n, 0x8000000000008080n, 0x0000000080000001n, 0x8000000080008008n,
  ];
  const ROT = [
    [0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56], [27, 20, 39, 8, 14],
  ];
  const rol = (v, n) => {
    n %= 64;
    if (!n) return v & MASK;
    return ((v << BigInt(n)) | (v >> BigInt(64 - n))) & MASK;
  };
  function keccakF(a) {
    for (let rnd = 0; rnd < 24; rnd++) {
      const c = [0n, 0n, 0n, 0n, 0n], d = [0n, 0n, 0n, 0n, 0n];
      for (let x = 0; x < 5; x++) c[x] = a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4];
      for (let x = 0; x < 5; x++) d[x] = c[(x + 4) % 5] ^ rol(c[(x + 1) % 5], 1);
      for (let x = 0; x < 5; x++) for (let y = 0; y < 5; y++) a[x][y] ^= d[x];
      const b = [[0n,0n,0n,0n,0n],[0n,0n,0n,0n,0n],[0n,0n,0n,0n,0n],[0n,0n,0n,0n,0n],[0n,0n,0n,0n,0n]];
      for (let x = 0; x < 5; x++) for (let y = 0; y < 5; y++) b[y][(2 * x + 3 * y) % 5] = rol(a[x][y], ROT[x][y]);
      for (let x = 0; x < 5; x++) for (let y = 0; y < 5; y++) a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y] & MASK) & b[(x + 2) % 5][y]);
      a[0][0] ^= RC[rnd];
    }
    return a;
  }
  function keccak256(bytes) {
    const rate = 136;
    const padded = new Uint8Array(Math.ceil((bytes.length + 1) / rate) * rate);
    padded.set(bytes);
    padded[bytes.length] = 0x01;
    padded[padded.length - 1] |= 0x80;
    const a = [[0n,0n,0n,0n,0n],[0n,0n,0n,0n,0n],[0n,0n,0n,0n,0n],[0n,0n,0n,0n,0n],[0n,0n,0n,0n,0n]];
    for (let off = 0; off < padded.length; off += rate) {
      for (let i = 0; i < rate / 8; i++) {
        let lane = 0n;
        for (let j = 0; j < 8; j++) lane |= BigInt(padded[off + i * 8 + j]) << BigInt(8 * j);
        a[i % 5][Math.floor(i / 5)] ^= lane;
      }
      keccakF(a);
    }
    const out = new Uint8Array(32);
    for (let i = 0; i < 4; i++) {
      const lane = a[i % 5][Math.floor(i / 5)];
      for (let j = 0; j < 8; j++) out[i * 8 + j] = Number((lane >> BigInt(8 * j)) & 0xffn);
    }
    return out;
  }

  /* -------------------------- поле ed25519 / точка -------------------------- */
  const P = (1n << 255n) - 19n;
  const L = (1n << 252n) + 27742317777372353535851937790883648493n;
  const mod = (v) => { const r = v % P; return r < 0n ? r + P : r; };
  function powMod(base, exp) {
    let r = 1n, a = mod(base);
    while (exp > 0n) { if (exp & 1n) r = mod(r * a); a = mod(a * a); exp >>= 1n; }
    return r;
  }
  const inv = (v) => powMod(v, P - 2n);
  const D = mod(-121665n * inv(121666n));
  const BX = 15112221349535400772501151409588531511454012693041857206046113283949847762202n;
  const BY = 46316835694926478169428394003475163141307993866256225615783033603165251855960n;
  const ID = [0n, 1n, 1n, 0n];                 // [X,Y,Z,T]
  const BASE = [BX, BY, 1n, mod(BX * BY)];

  function pointAdd(p1, p2) {
    const [X1, Y1, Z1, T1] = p1, [X2, Y2, Z2, T2] = p2;
    const A = mod((Y1 - X1) * (Y2 - X2));
    const B = mod((Y1 + X1) * (Y2 + X2));
    const C = mod(2n * D * T1 * T2);
    const Dd = mod(2n * Z1 * Z2);
    const E = mod(B - A), F = mod(Dd - C), G = mod(Dd + C), H = mod(B + A);
    return [mod(E * F), mod(G * H), mod(F * G), mod(E * H)];
  }
  function scalarMultBase(scalar /* Uint8Array 32, little endian */) {
    let n = 0n;
    for (let i = 31; i >= 0; i--) n = (n << 8n) | BigInt(scalar[i]);
    let r = ID, q = BASE;
    for (let i = 0; i < 256; i++) {
      if ((n >> BigInt(i)) & 1n) r = pointAdd(r, q);
      q = pointAdd(q, q);
    }
    return r;
  }
  function encodePoint(p) {
    const [X, Y, Z] = p, zi = inv(Z);
    const x = mod(X * zi), y = mod(Y * zi);
    const out = new Uint8Array(32);
    for (let j = 0; j < 32; j++) out[j] = Number((y >> BigInt(8 * j)) & 0xffn);
    out[31] |= Number(x & 1n) << 7;             // sign bit = parity of x
    return out;
  }
  function scReduce32(bytes /* Uint8Array 32 LE */) {
    let n = 0n;
    for (let i = 31; i >= 0; i--) n = (n << 8n) | BigInt(bytes[i]);
    const r = n % L, out = new Uint8Array(32);
    for (let i = 0; i < 32; i++) out[i] = Number((r >> BigInt(8 * i)) & 0xffn);
    return out;
  }

  /* ------------------------------ Base58 Monero ------------------------------ */
  const B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  const ENC = [0, 2, 3, 5, 6, 7, 9, 10, 11];
  function b58encode(bytes) {
    let s = '';
    for (let pos = 0; pos < bytes.length;) {
      const rem = bytes.length - pos, dec = rem >= 8 ? 8 : rem;
      const enc = dec === 8 ? 11 : ENC[dec];
      let num = 0n;
      for (let i = 0; i < dec; i++) num = (num << 8n) | BigInt(bytes[pos + i]);
      const tmp = new Array(enc).fill(B58[0]);
      for (let i = enc - 1; i >= 0; i--) { tmp[i] = B58[Number(num % 58n)]; num /= 58n; }
      s += tmp.join('');
      pos += dec;
    }
    return s;
  }
  function b58decode(s) {
    let out = [];
    for (let pos = 0; pos < s.length;) {
      const rem = s.length - pos, enc = rem >= 11 ? 11 : rem;
      let dec = enc === 11 ? 8 : ENC.indexOf(enc);
      if (dec < 0) throw new Error('bad base58 block');
      let num = 0n;
      for (let i = 0; i < enc; i++) { const v = B58.indexOf(s[pos + i]); if (v < 0) throw new Error('bad base58 char'); num = num * 58n + BigInt(v); }
      for (let i = 0; i < dec; i++) out.push(Number((num >> BigInt(8 * (dec - 1 - i))) & 0xffn));
      pos += enc;
    }
    return Uint8Array.from(out);
  }
  function addressFromPubkeys(spendPub, viewPub) {
    const data = new Uint8Array(65);
    data[0] = 0x12;                              // mainnet primary address
    data.set(spendPub, 1);
    data.set(viewPub, 33);
    const h = keccak256(data);
    const full = new Uint8Array(69);
    full.set(data); full.set(h.slice(0, 4), 65);
    return b58encode(full);
  }
  function verifyAddressChecksum(addr) {
    if (addr.length !== 95 && addr.length !== 106) return false;
    let raw;
    try { raw = b58decode(addr); } catch (e) { return false; }
    if (raw.length !== 69 && raw.length !== 77) return false;
    const h = keccak256(raw.slice(0, raw.length - 4));
    for (let i = 0; i < 4; i++) if (h[i] !== raw[raw.length - 4 + i]) return false;
    return raw[0] === 0x12 || raw[0] === 0x2a || raw[0] === 0x13;
  }

  /* ------------------------------ мнемоника ------------------------------ */
  function crc32(str) {
    const b = new TextEncoder().encode(str);
    let c = 0xffffffff;
    for (let i = 0; i < b.length; i++) {
      c ^= b[i];
      for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xedb88320 & -(c & 1));
    }
    return (c ^ 0xffffffff) >>> 0;
  }
  function bytesToWords(key, WORDS) {
    const N = WORDS.length, out = [];
    for (let i = 0; i < 8; i++) {
      const v = (key[i * 4] | (key[i * 4 + 1] << 8) | (key[i * 4 + 2] << 16) | (key[i * 4 + 3] << 24)) >>> 0;
      const w1 = v % N;
      const w2 = (Math.floor(v / N) + w1) % N;
      const w3 = (Math.floor(Math.floor(v / N) / N) + w2) % N;
      out.push(WORDS[w1], WORDS[w2], WORDS[w3]);
    }
    let trimmed = '';
    for (const w of out) trimmed += w.slice(0, 3);   // English unique_prefix_length = 3
    out.push(out[crc32(trimmed) % 24]);
    return out;
  }
  function wordsToBytes(words, WORDS) {
    if (words.length !== 25) throw new Error('Нужно ровно 25 слов');
    const N = WORDS.length, map = new Map(WORDS.map((w, i) => [w, i]));
    const first = words.slice(0, 24);
    let trimmed = '';
    for (const w of first) trimmed += w.slice(0, 3);
    if (words[24] !== first[crc32(trimmed) % 24]) throw new Error('Контрольное слово не совпадает — опечатка в сид-фразе');
    const out = new Uint8Array(32);
    for (let i = 0; i < 8; i++) {
      const a = map.get(first[i * 3]), b = map.get(first[i * 3 + 1]), c = map.get(first[i * 3 + 2]);
      if (a === undefined || b === undefined || c === undefined) throw new Error('Неизвестное слово в сид-фразе');
      const d1 = ((b - a) % N + N) % N, d2 = ((c - b) % N + N) % N;
      let v = a + N * d1 + N * N * d2;
      for (let j = 0; j < 4; j++) out[i * 4 + j] = Math.floor(v / Math.pow(2, 8 * j)) & 0xff;
    }
    return out;
  }

  /* ------------------------------ сборка кошелька ------------------------------ */
  const hex = (b) => Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
  const fromHex = (s) => Uint8Array.from(s.match(/../g).map(x => parseInt(x, 16)));

  function walletFromSpend(spendScalar, WORDS) {
    const spend = scReduce32(spendScalar);
    const view = scReduce32(keccak256(spend));
    const spendPub = encodePoint(scalarMultBase(spend));
    const viewPub = encodePoint(scalarMultBase(view));
    const address = addressFromPubkeys(spendPub, viewPub);
    const mnemonic = bytesToWords(spend, WORDS);
    return {
      address,
      mnemonic: mnemonic.join(' '),
      mnemonicWords: mnemonic,
      spendKey: hex(spend),
      viewKey: hex(view),
      spendPub: hex(spendPub),
      viewPub: hex(viewPub),
    };
  }
  function walletFromMnemonic(phrase, WORDS) {
    const words = phrase.trim().toLowerCase().split(/\s+/);
    const spend = scReduce32(wordsToBytes(words, WORDS));
    return walletFromSpend(spend, WORDS);
  }

  global.NMWalletCrypto = {
    keccak256, scReduce32, scalarMultBase, encodePoint, pointAdd,
    b58encode, b58decode, addressFromPubkeys, verifyAddressChecksum,
    crc32, bytesToWords, wordsToBytes, walletFromSpend, walletFromMnemonic,
    hex, fromHex, L, P, D,
  };
})(typeof window !== 'undefined' ? window : globalThis);
