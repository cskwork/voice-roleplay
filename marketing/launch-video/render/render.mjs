// Frame-by-frame renderer: headless Chromium renders scene/index.html at t = frame / fps, screenshots each frame
// and pipes the PNGs into ffmpeg (libx264 + AAC from audio/build/mix.wav).
//
//   node render/render.mjs                       # 1920x1080, 60 fps -> out/voice-roleplay-launch-1080p.mp4 + poster
//   node render/render.mjs --format vertical     # 1080x1920       -> out/voice-roleplay-launch-vertical.mp4
//   node render/render.mjs --stills 0.5,3.6,...  # PNG stills into .cache/stills/ (quick look, no video)
//   options: --fps 30  --from 7 --to 10 (partial render for checks, written to .cache/)
import { spawn } from 'node:child_process'
import { createReadStream, existsSync, mkdirSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { dirname, extname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const PKG = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const arg = (name, dflt) => (process.argv.includes(`--${name}`) ? process.argv[process.argv.indexOf(`--${name}`) + 1] : dflt)
const format = arg('format', 'landscape')
const fps = Number(arg('fps', 60))
const stills = arg('stills', null)
const from = Number(arg('from', 0))
const POSTER_T = Number(arg('poster', 28.9))

// Static server for scene/ (fetch and fonts need http, not file://).
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.webp': 'image/webp', '.woff2': 'font/woff2', '.json': 'application/json' }
const server = createServer((req, res) => {
  const p = join(PKG, 'scene', decodeURIComponent(new URL(req.url, 'http://x').pathname))
  if (!p.startsWith(join(PKG, 'scene')) || !existsSync(p) || statSync(p).isDirectory()) return res.writeHead(404).end()
  res.writeHead(200, { 'Content-Type': TYPES[extname(p)] ?? 'application/octet-stream' })
  createReadStream(p).pipe(res)
})
await new Promise((r) => server.listen(0, '127.0.0.1', r))
const port = server.address().port

const W = format === 'vertical' ? 1080 : 1920
const H = format === 'vertical' ? 1920 : 1080
const browser = await chromium.launch({ headless: true, args: ['--force-color-profile=srgb', '--disable-lcd-text', '--font-render-hinting=none'] })
const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 })
page.on('pageerror', (e) => console.error('page error:', e.message))
await page.goto(`http://127.0.0.1:${port}/index.html?format=${format}`)
await page.evaluate(() => window.__scene.ready)
const duration = await page.evaluate(() => window.__scene.duration)
const to = Number(arg('to', duration))
const frame = async (t) => {
  await page.evaluate((x) => window.__scene.render(x), t)
  return page.screenshot({ type: 'png', clip: { x: 0, y: 0, width: W, height: H } })
}

try {
  if (stills) {
    const dir = join(PKG, '.cache/stills')
    mkdirSync(dir, { recursive: true })
    const { writeFileSync } = await import('node:fs')
    for (const s of stills.split(',').map(Number)) {
      writeFileSync(join(dir, `${format}-${s.toFixed(2)}.png`), await frame(s))
    }
    console.log(`stills -> ${dir}`)
  } else {
    const partial = from > 0 || to < duration
    const outDir = partial ? join(PKG, '.cache') : join(PKG, 'out')
    mkdirSync(outDir, { recursive: true })
    const name = format === 'vertical' ? 'voice-roleplay-launch-vertical' : 'voice-roleplay-launch-1080p'
    const out = join(outDir, partial ? `${name}-part.mp4` : `${name}.mp4`)
    const audio = join(PKG, 'audio/build/mix.wav')
    const useAudio = existsSync(audio)
    const n0 = Math.round(from * fps)
    const n1 = Math.round(to * fps)
    const ff = spawn(
      'ffmpeg',
      [
        '-loglevel', 'error', '-y',
        '-f', 'image2pipe', '-framerate', String(fps), '-c:v', 'png', '-i', '-',
        ...(useAudio ? ['-ss', String(from), '-t', String((n1 - n0) / fps), '-i', audio] : []),
        '-map', '0:v', ...(useAudio ? ['-map', '1:a', '-c:a', 'aac', '-b:a', '256k', '-ar', '48000'] : []),
        '-c:v', 'libx264', '-preset', 'slow', '-crf', '17', '-pix_fmt', 'yuv420p', '-profile:v', 'high',
        '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
        '-vf', 'scale=out_color_matrix=bt709:out_range=tv',
        '-movflags', '+faststart', '-r', String(fps), out,
      ],
      { stdio: ['pipe', 'inherit', 'inherit'] },
    )
    const done = new Promise((res, rej) => ff.on('close', (c) => (c === 0 ? res() : rej(new Error(`ffmpeg exit ${c}`)))))
    const t0 = Date.now()
    for (let f = n0; f < n1; f++) {
      const buf = await frame(f / fps)
      if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once('drain', r))
      if ((f - n0) % 120 === 0) console.log(`${format} frame ${f}/${n1} (${((Date.now() - t0) / 1000).toFixed(0)} s)`)
    }
    ff.stdin.end()
    await done
    console.log(`video -> ${out}`)
    if (!partial) {
      const { writeFileSync } = await import('node:fs')
      const poster = join(outDir, format === 'vertical' ? 'poster-vertical.png' : 'poster.png')
      writeFileSync(poster, await frame(POSTER_T))
      console.log(`poster -> ${poster}`)
    }
  }
} finally {
  await browser.close()
  server.close()
}
