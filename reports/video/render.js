// Render linkedin_video.html to MP4: headless Chromium (Playwright), clock stepped frame by frame,
// every frame captured as PNG and piped to ffmpeg (H.264, yuv420p, 30 fps).
//
// Playwright lives outside the repo: set PW_DIR to the folder where `npm install playwright` was run,
// and CHROMIUM to a Chromium executable if Playwright's own build is not installed.
//   node reports/video/render.js video                 -> linkedin_video_silent.mp4
//   node reports/video/render.js frames 5.2 16.4 ...   -> preview PNGs in reports/video/frames/
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { chromium } = require(path.join(process.env.PW_DIR || '.', 'node_modules', 'playwright'));

const HERE = __dirname;
const FPS = 30;
const FONTS = ['500 40px "Barlow Condensed"', '600 40px "Barlow Condensed"', '700 40px "Barlow Condensed"',
  '400 40px "IBM Plex Sans"', '500 40px "IBM Plex Sans"', '500 40px "IBM Plex Mono"'];
const SAMPLE = 'Oğuzhan Ketenci 0123456789 ↓→·✓’–× ABCabc';

async function open() {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
  const page = await browser.newPage({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: 1 });
  const vdata = fs.readFileSync(path.join(HERE, 'video_data.json'), 'utf8');
  const sync = fs.readFileSync(path.join(HERE, 'music_sync.json'), 'utf8');
  await page.addInitScript(`window.VDATA=${vdata};window.SYNC=${sync};`);
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto('file:///' + path.join(HERE, 'linkedin_video.html').replace(/\\/g, '/'), { waitUntil: 'networkidle' });
  // Fonts must be loaded before the first frame: load every face used, then check them all.
  const ok = await page.evaluate(async ([fonts, sample]) => {
    await Promise.all(fonts.map(f => document.fonts.load(f, sample)));
    await document.fonts.ready;
    return fonts.map(f => [f, document.fonts.check(f, 'Ag')]);
  }, [FONTS, SAMPLE]);
  const missing = ok.filter(([, v]) => !v).map(([f]) => f);
  if (missing.length) throw new Error('fonts not loaded: ' + missing.join(', '));
  const info = await page.evaluate(() => window.VIDEO);
  if (errors.length) throw new Error('page errors: ' + errors.join(' | '));
  return { browser, page, info, errors };
}

async function grab(page, t) {
  const url = await page.evaluate(t => { window.renderAt(t); return document.getElementById('film').toDataURL('image/png'); }, t);
  return Buffer.from(url.split(',')[1], 'base64');
}

(async () => {
  const mode = process.argv[2];
  const { browser, page, info, errors } = await open();
  console.log(`fonts loaded; ${info.scenes.length} scenes, total ${info.total.toFixed(3)} s`);
  if (mode === 'frames') {
    const dir = process.env.FRAMES_DIR || path.join(HERE, 'frames');
    fs.mkdirSync(dir, { recursive: true });
    for (const a of process.argv.slice(3)) {
      fs.writeFileSync(path.join(dir, `t_${Number(a).toFixed(2)}.png`), await grab(page, Number(a)));
    }
  } else {
    const n = Math.round(info.total * FPS);
    const out = path.join(HERE, 'linkedin_video_silent.mp4');
    const ff = spawn('ffmpeg', ['-y', '-v', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-c:v', 'png', '-i', '-',
      '-c:v', 'libx264', '-preset', 'slow', '-crf', '18', '-pix_fmt', 'yuv420p', '-r', String(FPS), '-movflags', '+faststart', out],
      { stdio: ['pipe', 'inherit', 'inherit'] });
    const t0 = Date.now();
    for (let f = 0; f < n; f++) {
      const buf = await grab(page, f / FPS);
      if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
      if (f % 300 === 0) console.log(`frame ${f}/${n} (${((Date.now() - t0) / 1000).toFixed(0)} s)`);
    }
    ff.stdin.end();
    await new Promise((res, rej) => ff.on('close', c => c === 0 ? res() : rej(new Error('ffmpeg exit ' + c))));
    console.log(`done: ${n} frames in ${((Date.now() - t0) / 1000).toFixed(0)} s -> ${out}`);
  }
  if (errors.length) console.log('page errors:', errors);
  await browser.close();
})().catch(e => { console.error('ERROR', e.message); process.exit(1); });
