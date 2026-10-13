"use client";

/* Voice mode — the audio side: an always-on microphone that finds where the
 * user's sentences start and end, and a player that plays the voice as it
 * streams in and knows, at any instant, how much of the text has been said.
 *
 * Voice activity is detected on ENERGY against an adaptive noise floor: no
 * model to download, and the browser's echo cancellation keeps the
 * assistant's own voice out of the mic. While the assistant speaks, the bar
 * is raised (`guard`) so that only a real interruption cuts it. */

const PREROLL_MS = 350;      // audio kept before the trigger: the first syllable
const START_MS = 120;        // loud this long → speech started
const START_MS_GUARDED = 300; // … while the assistant speaks (barge-in)
const END_SILENCE_MS = 800;  // quiet this long → the sentence is over
// Speech lasting this long is a real interruption, not a cough, a noise or
// the assistant's own echo: only then is the answer cut (« …les ressources
// et la » was cut by a blip that transcribed to nothing, 2026-10-13).
const CONFIRM_MS = 700;
const MIN_UTTERANCE_MS = 350;
const MAX_UTTERANCE_MS = 30_000; // Whisper's window
const WAV_RATE = 16_000;     // Whisper's native rate

/** Mono float PCM → 16-bit WAV at 16 kHz (averaging decimation). */
function toWav(chunks: Float32Array[], rate: number): Blob {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const ratio = rate / WAV_RATE;
  const n = Math.floor(total / ratio);
  const out = new Int16Array(n);
  let k = 0, acc = 0, cnt = 0, pos = 0, next = ratio;
  for (const c of chunks) {
    for (let i = 0; i < c.length; i++, pos++) {
      acc += c[i]; cnt++;
      if (pos + 1 >= next && k < n) {
        const v = Math.max(-1, Math.min(1, acc / cnt));
        out[k++] = v * 0x7fff;
        acc = 0; cnt = 0; next += ratio;
      }
    }
  }
  const buf = new ArrayBuffer(44 + out.byteLength);
  const v = new DataView(buf);
  const str = (o: number, s: string) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); v.setUint32(4, 36 + out.byteLength, true); str(8, "WAVE");
  str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, WAV_RATE, true); v.setUint32(28, WAV_RATE * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, out.byteLength, true);
  new Int16Array(buf, 44).set(out);
  return new Blob([buf], { type: "audio/wav" });
}

export class MicListener {
  /** 0..1, smoothed: drives the halo while the user speaks. */
  level = 0;
  muted = false;
  /** The assistant is speaking: only a deliberate interruption counts. */
  guard = false;
  onSpeechStart: () => void = () => {};
  /** The speech has lasted CONFIRM_MS: a real turn, not a blip. */
  onSpeechConfirmed: () => void = () => {};
  onSpeechEnd: (wav: Blob | null, confirmed: boolean) => void = () => {};

  private ctx: AudioContext;
  private stream: MediaStream;
  private node: ScriptProcessorNode;
  private source: MediaStreamAudioSourceNode;
  private pre: Float32Array[] = [];
  private utt: Float32Array[] | null = null;
  private noise = 0.004;
  private loudMs = 0;
  private quietMs = 0;
  private uttMs = 0;
  private confirmed = false;

  private constructor(ctx: AudioContext, stream: MediaStream) {
    this.ctx = ctx;
    this.stream = stream;
    this.source = ctx.createMediaStreamSource(stream);
    this.node = ctx.createScriptProcessor(2048, 1, 1);
    this.node.onaudioprocess = (e) => this.process(e.inputBuffer.getChannelData(0));
    this.source.connect(this.node);
    // A ScriptProcessor only runs when connected to the output: through a
    // muted gain, so the mic is never heard.
    const mute = ctx.createGain();
    mute.gain.value = 0;
    this.node.connect(mute).connect(ctx.destination);
  }

  static async open(): Promise<MicListener> {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    const ctx = new AudioContext();
    await ctx.resume();
    return new MicListener(ctx, stream);
  }

  private process(input: Float32Array) {
    const frame = new Float32Array(input);
    const ms = (frame.length / this.ctx.sampleRate) * 1000;
    let sum = 0;
    for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
    const rms = Math.sqrt(sum / frame.length);
    this.level = this.level * 0.7 + Math.min(1, rms * 12) * 0.3;
    if (this.muted) {
      this.utt = null;
      this.loudMs = 0;
      return;
    }
    const threshold = Math.max(0.012, this.noise * 3) * (this.guard ? 2.2 : 1);
    const loud = rms > threshold;
    if (!this.utt) {
      if (!loud) this.noise = this.noise * 0.97 + rms * 0.03;   // follow the room
      this.pre.push(frame);
      let kept = this.pre.reduce((n, c) => n + c.length, 0);
      while (this.pre.length > 1 && (kept / this.ctx.sampleRate) * 1000 > PREROLL_MS) {
        kept -= this.pre.shift()!.length;
      }
      this.loudMs = loud ? this.loudMs + ms : 0;
      if (this.loudMs >= (this.guard ? START_MS_GUARDED : START_MS)) {
        this.utt = this.pre;
        this.pre = [];
        this.quietMs = 0;
        this.uttMs = this.loudMs + PREROLL_MS;
        this.confirmed = false;
        this.onSpeechStart();
      }
      return;
    }
    this.utt.push(frame);
    this.uttMs += ms;
    this.quietMs = loud ? 0 : this.quietMs + ms;
    if (!this.confirmed && this.uttMs - this.quietMs >= CONFIRM_MS) {
      this.confirmed = true;
      this.onSpeechConfirmed();
    }
    if (this.quietMs >= END_SILENCE_MS || this.uttMs >= MAX_UTTERANCE_MS) {
      const chunks = this.utt;
      const spoken = this.uttMs - this.quietMs;
      this.utt = null;
      this.loudMs = 0;
      // Always reported: the listener of a blip must undo what onSpeechStart
      // did (the ducked voice); `confirmed` tells it apart from a real turn.
      this.onSpeechEnd(spoken >= MIN_UTTERANCE_MS ? toWav(chunks, this.ctx.sampleRate) : null, this.confirmed);
    }
  }

  /** The sentence being spoken so far (live transcription), or null. */
  snapshot(): Blob | null {
    return this.utt && this.utt.length ? toWav(this.utt, this.ctx.sampleRate) : null;
  }

  get speaking(): boolean {
    return this.utt !== null;
  }

  close() {
    this.node.disconnect();
    this.source.disconnect();
    this.stream.getTracks().forEach((t) => t.stop());
    void this.ctx.close();
  }
}

/** One spoken sentence: where its characters are and when its audio plays. */
type Segment = {
  charStart: number;
  charEnd: number;
  start: number | null;    // AudioContext time of its first sample
  end: number;             // scheduled end so far
  done: boolean;           // all its audio has arrived
};

/** Audio gathered before a voice starts from silence, and after it ran dry. */
const START_BUFFER_S = 0.5;
const REBUFFER_S = 0.35;

/** Characters per second of French speech, for the highlight while a
 *  sentence's audio is still arriving (corrected as soon as it has). */
const CHARS_PER_S = 14;

export class SpeechPlayer {
  muted = false;
  private ducked = false;
  private ctx: AudioContext;
  private out: GainNode;
  private analyser: AnalyserNode;
  private sources = new Set<AudioBufferSourceNode>();
  private segments: Segment[] = [];
  private nextTime = 0;
  private abort: AbortController | null = null;
  private data = new Uint8Array(256);

  constructor() {
    this.ctx = new AudioContext();
    this.out = this.ctx.createGain();
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 256;
    this.out.connect(this.analyser).connect(this.ctx.destination);
  }

  async resume() {
    await this.ctx.resume();
  }

  setMuted(m: boolean) {
    this.muted = m;
    this.out.gain.value = m ? 0 : this.ducked ? 0.25 : 1;
  }

  /** Lowers the voice while the user may be starting to talk (barge-in not
   *  confirmed yet): a blip then costs nothing, a real turn cuts it. */
  duck(on: boolean) {
    this.ducked = on;
    this.out.gain.setTargetAtTime(this.muted ? 0 : on ? 0.25 : 1, this.ctx.currentTime, 0.05);
  }

  /** Streams one sentence's voice and schedules it after what is queued.
   *  Resolves when all of it has ARRIVED (not played): the next sentence's
   *  request starts then, so the voice never waits for the network. */
  async speak(text: string, voice: string, csrf: string, charStart: number, charEnd: number): Promise<void> {
    const ac = new AbortController();
    this.abort = ac;
    const seg: Segment = { charStart, charEnd, start: null, end: 0, done: false };
    this.segments.push(seg);
    const res = await fetch("/api/voice/speak", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify({ text, voice }),
      signal: ac.signal,
    });
    if (!res.ok || !res.body) {
      seg.done = true;
      seg.start = seg.start ?? this.ctx.currentTime;
      seg.end = seg.start;
      let error = "";
      try { error = (await res.json())?.error ?? ""; } catch { /* not JSON */ }
      throw new Error(error || `HTTP ${res.status}`);
    }
    const rate = Number(res.headers.get("X-Sample-Rate")) || 24000;
    const reader = res.body.getReader();
    let pending: AudioBuffer[] = [];
    let pendingS = 0;
    const flush = () => {
      for (const b of pending) this.schedule(b, seg);
      pending = [];
      pendingS = 0;
    };
    let carry: Uint8Array | null = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      let bytes: Uint8Array = value;
      if (carry) {
        const joined = new Uint8Array(carry.length + bytes.length);
        joined.set(carry);
        joined.set(bytes, carry.length);
        bytes = joined;
        carry = null;
      }
      if (bytes.length % 2) {
        carry = bytes.slice(bytes.length - 1);
        bytes = bytes.slice(0, bytes.length - 1);
      }
      if (!bytes.length) continue;
      const pcm = new Int16Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length));
      const buffer = this.ctx.createBuffer(1, pcm.length, rate);
      const ch = buffer.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 0x8000;
      // Nothing left ahead of the play head: the voice is arriving slower
      // than it plays (the chat model shares the GPU while it is still
      // writing). Playing each 80 ms frame as it lands then sounds like a
      // crackle — gaps between frames. We gather a little audio first, and
      // play it in one go.
      const starving = this.nextTime < this.ctx.currentTime + 0.05;
      if (starving || pending.length) {
        pending.push(buffer);
        pendingS += buffer.duration;
        if (pendingS >= (seg.start === null ? START_BUFFER_S : REBUFFER_S)) flush();
      } else {
        this.schedule(buffer, seg);
      }
    }
    flush();
    seg.done = true;
  }

  private schedule(buffer: AudioBuffer, seg: Segment) {
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.out);
    const at = Math.max(this.ctx.currentTime + 0.03, this.nextTime);
    src.start(at);
    src.onended = () => this.sources.delete(src);
    this.sources.add(src);
    if (seg.start === null) seg.start = at;
    this.nextTime = at + buffer.duration;
    seg.end = this.nextTime;
  }

  /** How many characters of the answer have been SAID by now. */
  spokenChars(): number {
    const now = this.ctx.currentTime;
    let said = 0;
    for (const s of this.segments) {
      if (s.start === null || now < s.start) return Math.max(said, s.charStart);
      const span = s.done ? s.end - s.start : Math.max(s.end - s.start, (s.charEnd - s.charStart) / CHARS_PER_S);
      const frac = span > 0 ? Math.min(1, (now - s.start) / span) : 1;
      said = s.charStart + Math.round(frac * (s.charEnd - s.charStart));
      if (frac < 1) return said;
    }
    return said;
  }

  /** Nothing queued nor playing. */
  idle(): boolean {
    return this.ctx.currentTime >= this.nextTime && this.segments.every((s) => s.done);
  }

  /** 0..1: the assistant's voice level, for the halo. */
  level(): number {
    this.analyser.getByteTimeDomainData(this.data);
    let sum = 0;
    for (let i = 0; i < this.data.length; i++) {
      const x = (this.data[i] - 128) / 128;
      sum += x * x;
    }
    return Math.min(1, Math.sqrt(sum / this.data.length) * 4);
  }

  /** Barge-in or close: silence now, drop what was queued. */
  stop() {
    this.duck(false);
    this.abort?.abort();
    this.abort = null;
    for (const s of this.sources) {
      try { s.stop(); } catch { /* already ended */ }
    }
    this.sources.clear();
    this.segments = [];
    this.nextTime = 0;
  }

  /** Forget the previous answer's segments (a new answer starts at char 0). */
  reset() {
    this.stop();
  }

  close() {
    this.stop();
    void this.ctx.close();
  }
}
