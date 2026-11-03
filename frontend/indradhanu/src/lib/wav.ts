/** Recorded audio → 16 kHz mono WAV, the format every speech service reads.
 *
 *  Browsers record in whatever they like: Chrome and Android WebView give
 *  WebM/Opus, Safari gives MP4/AAC, and the exact `mimeType` string varies
 *  (`audio/webm;codecs=opus`). The speech service is strict about formats and
 *  a refusal there reached the person as a bare 400. Decoding in the browser
 *  and sending plain PCM removes the guesswork, and at 16 kHz mono a 25 s
 *  report is about 800 KB, well under the upload limit.
 */

const TARGET_RATE = 16000

export async function toWav16k(blob: Blob): Promise<Blob> {
  const AC: typeof AudioContext | undefined =
    window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!AC) throw new Error("no AudioContext")
  const ctx = new AC()
  try {
    const decoded = await ctx.decodeAudioData(await blob.arrayBuffer())
    const frames = Math.max(1, Math.ceil(decoded.duration * TARGET_RATE))
    const offline = new OfflineAudioContext(1, frames, TARGET_RATE)
    const src = offline.createBufferSource()
    src.buffer = decoded
    src.connect(offline.destination)
    src.start()
    const rendered = await offline.startRendering()
    return encodeWav(rendered.getChannelData(0), TARGET_RATE)
  } finally {
    void ctx.close()
  }
}

function encodeWav(samples: Float32Array, rate: number): Blob {
  const bytes = 44 + samples.length * 2
  const buf = new ArrayBuffer(bytes)
  const v = new DataView(buf)
  const str = (at: number, s: string) => { for (let i = 0; i < s.length; i++) v.setUint8(at + i, s.charCodeAt(i)) }
  str(0, "RIFF"); v.setUint32(4, bytes - 8, true); str(8, "WAVE")
  str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true)
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true)
  str(36, "data"); v.setUint32(40, samples.length * 2, true)
  let o = 44
  for (let i = 0; i < samples.length; i++, o += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true)
  }
  return new Blob([buf], { type: "audio/wav" })
}
