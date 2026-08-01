#!/usr/bin/env node
'use strict';
/*
 * Скільки коштує браузеру прийняти й намалювати кадр.
 *
 * Задача 0018. Замір моста показав, що він устигає віддати в телефон 123 КБ/с
 * при 155 КБ/с потреби, і назвав це «вузьким Wi-Fi». ⚠️ Це був поспішний
 * висновок: міст упирається у **вікно TCP**, а вікно зачиняє той, хто не
 * вичитує сокет. У браузері приймання і малювання живуть в одному потоці, тож
 * повільний клієнт виглядає точно як вузький ефір.
 *
 * Тому цей стенд ганяє **той самий** `webui/proto.js`, що виконується на
 * телефоні, на **справжньому** записаному потоці (`loss_watch.py --dump`) і
 * ділить час за щаблями:
 *
 *   feed    розбір кадрування + CRC по всьому вантажу
 *   decode  decodeTile: розтискання RLE16 у Uint16Array
 *   blit    blitTile: RGB565 → RGBA, чотири записи на піксель
 *
 * ⚠️ Чого стенд НЕ міряє: `putImageData`. Полотна під node немає, а це
 * четвертий щабель і, найімовірніше, найдорожчий — 522 КБ на кожен кадр.
 * Його межу дає лише справжній браузер. Число звідси — це **нижня** оцінка
 * вартості клієнта, і саме так його треба читати.
 *
 * Синтетичний трафік тут не годиться: вартість розтискання залежить від
 * кількості серій RLE, тобто від самої картинки.
 *
 * Запуск:
 *
 *     tools/loss_watch.py --ws ws://192.168.4.1/ws --minutes 0.5 --dump /tmp/s.bin
 *     node tools/webui_bench.js /tmp/s.bin [ШИРИНА ВИСОТА]
 */

const fs = require('fs');
const path = require('path');

const P = require(path.join(__dirname, '..', 'webui', 'proto.js'));

const file = process.argv[2];
if (!file) {
  console.error('вкажи файл із записаним потоком (loss_watch.py --dump)');
  process.exit(2);
}
const W = Number(process.argv[3] || 480);
const H = Number(process.argv[4] || 272);

const raw = fs.readFileSync(file);

/* Розмір шматка — не косметика. Браузер отримує потік порціями по одному
 * кадру WebSocket, і саме стільки роботи припадає на один виток обробника
 * подій. Заміряна середня пачка моста — 1805 Б. */
const CHUNK = Number(process.env.CHUNK || 1805);

// Полотна немає, але буфер пікселів той самий: Uint8ClampedArray, як у
// ImageData. ⚠️ Звичайний Uint8Array збрехав би на користь клієнта — записи
// в clamped-масив коштують дорожче, і саме він стоїть у браузері.
const frame = new Uint8ClampedArray(W * H * 4);

let tFeed = 0, tDecode = 0, tBlit = 0;
let tiles = 0, frames = 0, px = 0, badTiles = 0, oob = 0;

const decoder = new P.Decoder();

function onPacket(type, payload) {
  if (type === P.PKT_TILE) {
    const t0 = process.hrtime.bigint();
    const tile = P.decodeTile(payload);
    const t1 = process.hrtime.bigint();
    tDecode += Number(t1 - t0);
    if (!tile) { badTiles++; return; }

    const t2 = process.hrtime.bigint();
    const okBlit = P.blitTile(frame, W, H, tile);
    const t3 = process.hrtime.bigint();
    tBlit += Number(t3 - t2);

    if (okBlit) { tiles++; px += tile.w * tile.h; } else { oob++; }
  } else if (type === P.PKT_FRAME_END) {
    frames++;
  }
}

/* ⚠️ Час `feed` рахується без вкладених щаблів: `onPacket` кличеться
 * всередині нього, і без віднімання розбір «коштував» би разом із
 * розтисканням і малюванням. Саме так і виходить розкладка, що ні на що не
 * вказує. */
const wall0 = process.hrtime.bigint();
for (let off = 0; off < raw.length; off += CHUNK) {
  const chunk = new Uint8Array(raw.buffer, raw.byteOffset + off,
                               Math.min(CHUNK, raw.length - off));
  const a = process.hrtime.bigint();
  decoder.feed(chunk, onPacket);
  const b = process.hrtime.bigint();
  tFeed += Number(b - a);
}
const wall = Number(process.hrtime.bigint() - wall0) / 1e6;

const feedOnly = (tFeed - tDecode - tBlit) / 1e6;
const decodeMs = tDecode / 1e6;
const blitMs = tBlit / 1e6;

console.log(`потік: ${(raw.length / 1024).toFixed(0)} КБ, шматками по ${CHUNK} Б`);
console.log(`розібрано: пакетів ${decoder.packets}, плиток ${tiles}, кадрів ${frames}`);
console.log(`  помилок CRC ${decoder.crcErrors}, брехливих LEN ${decoder.oversized}, ` +
            `битих плиток ${badTiles}, поза екраном ${oob}`);
if (!tiles) {
  console.error('⚠️ жодної плитки — це не потік Remote UI або він порожній');
  process.exit(1);
}
console.log(`  пікселів ${px}, у середньому ${(px / tiles).toFixed(0)} на плитку`);

console.log(`\nчас на весь потік: ${wall.toFixed(1)} мс`);
console.log(`  feed (кадрування + CRC) ${feedOnly.toFixed(1)} мс` +
            `  (${(feedOnly / wall * 100).toFixed(0)}%)`);
console.log(`  decodeTile (RLE16)      ${decodeMs.toFixed(1)} мс` +
            `  (${(decodeMs / wall * 100).toFixed(0)}%)`);
console.log(`  blitTile (565→RGBA)     ${blitMs.toFixed(1)} мс` +
            `  (${(blitMs / wall * 100).toFixed(0)}%)`);

/* Головне число: скільки трафіку на секунду цей код здатен проковтнути,
 * якщо більше нічого не робити. Порівнювати з потребою пульта. */
const kbps = raw.length / 1024 / (wall / 1000);
console.log(`\nстеля цього щабля: ${kbps.toFixed(0)} КБ/с ` +
            `(пульт віддає ~155 КБ/с під гортанням)`);
console.log(`⚠️ це НИЖНЯ оцінка вартості клієнта: putImageData сюди не входить, ` +
            `а це ${(W * H * 4 / 1024).toFixed(0)} КБ на кожен кадр`);
console.log(`\n⚠️ і це числа ПК. Телефон повільніший — у скільки разів, ` +
            `звідси не видно.`);
