"use client";

/**
 * Mic recording → WAV file.
 *
 * MediaRecorder does NOT produce WAV: depending on the browser it's WebM/Opus,
 * OGG/Opus or MP4/AAC. But Chatterbox (and thus /api/voice/generate)
 * only accepts WAV or MP3. So we decode the recording to PCM with
 * the AudioContext (which can read all these containers) then re-encode a WAV
 * ourselves — no added dependency, and the backend stays unchanged.
 *
 * The render is brought down to 24 kHz mono, the model's native rate: this
 * divides the file size by ~4 compared to the mic's raw 48 kHz stereo, with no
 * useful loss since the model resamples anyway.
 */

export const VOICE_TARGET_SAMPLE_RATE = 24000;

/** Containers tried in order: the first one supported by the browser wins. */
const PREFERRED_MIME_TYPES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/mp4",
];

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder === "undefined") return undefined;
  return PREFERRED_MIME_TYPES.find((t) => MediaRecorder.isTypeSupported(t));
}

export type Recorder = {
  /** Stops the recording and returns the converted WAV. */
  stop: () => Promise<File>;
  /**
   * WAV of everything captured SINCE THE START, without interrupting
   * the recording — for live dictation. We always restart from the beginning
   * rather than the last chunk: the intermediate fragments of a WebM
   * stream aren't decodable on their own (only the first carries the header), and
   * re-transcribing everything gives better text, the model having more
   * context. Returns null as long as nothing has been captured.
   */
  snapshot: () => Promise<File | null>;
  /** Stops everything and releases the mic without producing a file (cancellation). */
  cancel: () => void;
};

export async function startRecording(): Promise<Recorder> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  const mimeType = pickMimeType();
  const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  const chunks: BlobPart[] = [];
  recorder.ondataavailable = (e) => {
    if (e.data.size > 0) chunks.push(e.data);
  };
  // Periodic chunking: without an argument, MediaRecorder only delivers the
  // data on stop, and snapshot() would never have anything to transcribe.
  recorder.start(500);

  const releaseMic = () => stream.getTracks().forEach((t) => t.stop());

  return {
    snapshot: async () => {
      if (!chunks.length) return null;
      const raw = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
      try {
        return await blobToWavFile(raw);
      } catch {
        // A stream truncated at the wrong place can be undecodable: we
        // skip this round, the next one will restart from a more complete buffer.
        return null;
      }
    },
    stop: () =>
      new Promise<File>((resolve, reject) => {
        recorder.onstop = async () => {
          releaseMic();
          try {
            const raw = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
            resolve(await blobToWavFile(raw));
          } catch (e) {
            reject(e);
          }
        };
        // An already-stopped recorder will never re-emit onstop: we keep the
        // promise resolvable in that case rather than leaving it pending.
        if (recorder.state === "inactive") recorder.onstop?.(new Event("stop"));
        else recorder.stop();
      }),
    cancel: () => {
      if (recorder.state !== "inactive") recorder.stop();
      releaseMic();
    },
  };
}

/** Decodes any audio container readable by the browser → 24 kHz mono WAV. */
async function blobToWavFile(blob: Blob): Promise<File> {
  const arrayBuffer = await blob.arrayBuffer();
  const decodeCtx = new AudioContext();
  let decoded: AudioBuffer;
  try {
    decoded = await decodeCtx.decodeAudioData(arrayBuffer);
  } finally {
    void decodeCtx.close();
  }

  // OfflineAudioContext does the resampling AND the mono downmix in one go.
  const frameCount = Math.max(1, Math.ceil(decoded.duration * VOICE_TARGET_SAMPLE_RATE));
  const offline = new OfflineAudioContext(1, frameCount, VOICE_TARGET_SAMPLE_RATE);
  const source = offline.createBufferSource();
  source.buffer = decoded;
  source.connect(offline.destination);
  source.start();
  const rendered = await offline.startRendering();

  const wav = encodeWav(rendered.getChannelData(0), VOICE_TARGET_SAMPLE_RATE);
  return new File([wav], "recording.wav", { type: "audio/wav" });
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
