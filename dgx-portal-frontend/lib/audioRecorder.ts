"use client";

/**
 * Mic recording → WAV file, via CONTINUOUS PCM capture (Web Audio).
 *
 * Why not MediaRecorder: it emits WebM/Opus (or OGG/MP4), and a WebM stream
 * that hasn't been *finalised* is not decodable — `decodeAudioData` throws on a
 * partial blob because the container header/duration aren't written until the
 * recorder stops. That silently broke live dictation: every intermediate
 * `snapshot()` failed to decode and returned null, so text only appeared at the
 * very end (on the complete container). Capturing raw PCM instead means every
 * snapshot is a self-contained, always-decodable buffer → real-time dictation.
 *
 * We tap the mic with an AudioContext + ScriptProcessor, accumulate the mono
 * Float32 samples, and encode a 24 kHz WAV (the voice model's native rate;
 * Whisper resamples anyway) on demand. Both `snapshot()` (live) and `stop()`
 * (final) run off the same accumulated PCM, so they can never disagree on a
 * container boundary.
 */

export const VOICE_TARGET_SAMPLE_RATE = 24000;

export type Recorder = {
  /** Stops the recording and returns the converted WAV. */
  stop: () => Promise<File>;
  /**
   * WAV of everything captured SINCE THE START, without interrupting the
   * recording — for live dictation. Always valid (raw PCM, no container to
   * finalise). Returns null as long as nothing has been captured.
   */
  snapshot: () => Promise<File | null>;
  /** Stops everything and releases the mic without producing a file (cancellation). */
  cancel: () => void;
};

export async function startRecording(): Promise<Recorder> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });

  const ctx = new AudioContext();
  const sourceRate = ctx.sampleRate; // usually 48000
  const source = ctx.createMediaStreamSource(stream);
  // ScriptProcessor is deprecated but universally supported and needs no
  // separately-served worklet module — pragmatic for a short mono recording.
  const processor = ctx.createScriptProcessor(4096, 1, 1);
  // A ScriptProcessor only runs while connected to the destination; route it
  // through a silent gain so the mic isn't echoed back to the speakers.
  const silence = ctx.createGain();
  silence.gain.value = 0;

  const chunks: Float32Array[] = [];
  let total = 0;
  processor.onaudioprocess = (e) => {
    // Copy: the event buffer is reused by the engine after this callback.
    const input = e.inputBuffer.getChannelData(0);
    chunks.push(new Float32Array(input));
    total += input.length;
  };

  source.connect(processor);
  processor.connect(silence);
  silence.connect(ctx.destination);

  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    try { processor.disconnect(); } catch { /* already gone */ }
    try { source.disconnect(); } catch { /* already gone */ }
    try { silence.disconnect(); } catch { /* already gone */ }
    stream.getTracks().forEach((t) => t.stop());
    void ctx.close();
  };

  const buildWav = async (): Promise<File | null> => {
    if (total === 0) return null;
    // Flatten the accumulated chunks into one contiguous PCM buffer.
    const pcm = new Float32Array(total);
    let at = 0;
    for (const c of chunks) { pcm.set(c, at); at += c.length; }
    const resampled = await resampleMonoTo(pcm, sourceRate, VOICE_TARGET_SAMPLE_RATE);
    const wav = encodeWav(resampled, VOICE_TARGET_SAMPLE_RATE);
    return new File([wav], "recording.wav", { type: "audio/wav" });
  };

  return {
    snapshot: buildWav,
    stop: async () => {
      const file = await buildWav();
      release();
      // total>0 is guaranteed by the caller (min-duration gate); fall back to a
      // tiny silent WAV rather than reject, so stop() always resolves a File.
      return file ?? new File([encodeWav(new Float32Array(1), VOICE_TARGET_SAMPLE_RATE)], "recording.wav", { type: "audio/wav" });
    },
    cancel: release,
  };
}

/** Resample a mono Float32 buffer to `targetRate` via OfflineAudioContext. */
async function resampleMonoTo(pcm: Float32Array, sourceRate: number, targetRate: number): Promise<Float32Array> {
  if (sourceRate === targetRate) return pcm;
  const frames = Math.max(1, Math.round((pcm.length / sourceRate) * targetRate));
  const offline = new OfflineAudioContext(1, frames, targetRate);
  const buffer = offline.createBuffer(1, pcm.length, sourceRate);
  // .set() copies values without the Float32Array<ArrayBuffer> generic
  // constraint that copyToChannel imposes.
  buffer.getChannelData(0).set(pcm);
  const src = offline.createBufferSource();
  src.buffer = buffer;
  src.connect(offline.destination);
  src.start();
  const rendered = await offline.startRendering();
  return rendered.getChannelData(0);
}

/** PCM float32 [-1,1] → 16-bit mono WAV (44-byte RIFF header). */
function encodeWav(samples: Float32Array, sampleRate: number): ArrayBuffer {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeString = (offset: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i));
  };

  writeString(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true); // fmt block size
  view.setUint16(20, 1, true); // integer PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // bytes/second
  view.setUint16(32, 2, true); // block alignment
  view.setUint16(34, 16, true); // bits per sample
  writeString(36, "data");
  view.setUint32(40, samples.length * 2, true);

  let offset = 44;
  for (let i = 0; i < samples.length; i++) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    offset += 2;
  }
  return buffer;
}
