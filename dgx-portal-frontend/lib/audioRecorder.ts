"use client";

/**
 * Enregistrement micro → fichier WAV.
 *
 * MediaRecorder ne produit PAS de WAV : selon le navigateur c'est du WebM/Opus,
 * de l'OGG/Opus ou du MP4/AAC. Or Chatterbox (et donc /api/voice/generate)
 * n'accepte que du WAV ou du MP3. On décode donc l'enregistrement en PCM avec
 * l'AudioContext (qui sait lire tous ces conteneurs) puis on ré-encode un WAV
 * nous-mêmes — aucune dépendance ajoutée, et le backend reste inchangé.
 *
 * Le rendu est ramené à 24 kHz mono, la fréquence native du modèle : ça divise
 * la taille du fichier par ~4 face au 48 kHz stéréo brut du micro, sans perte
 * utile puisque le modèle rééchantillonne de toute façon.
 */

export const VOICE_TARGET_SAMPLE_RATE = 24000;

/** Conteneurs testés dans l'ordre : le premier supporté par le navigateur gagne. */
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
  /** Arrête l'enregistrement et rend le WAV converti. */
  stop: () => Promise<File>;
  /** Arrête tout et libère le micro sans produire de fichier (annulation). */
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
  recorder.start();

  const releaseMic = () => stream.getTracks().forEach((t) => t.stop());

  return {
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
        // Un recorder déjà arrêté ne réémettra jamais onstop : on garde la
        // promesse résolvable dans ce cas plutôt que de la laisser pendante.
        if (recorder.state === "inactive") recorder.onstop?.(new Event("stop"));
        else recorder.stop();
      }),
    cancel: () => {
      if (recorder.state !== "inactive") recorder.stop();
      releaseMic();
    },
  };
}

/** Décode n'importe quel conteneur audio lisible par le navigateur → WAV 24 kHz mono. */
async function blobToWavFile(blob: Blob): Promise<File> {
  const arrayBuffer = await blob.arrayBuffer();
  const decodeCtx = new AudioContext();
  let decoded: AudioBuffer;
  try {
    decoded = await decodeCtx.decodeAudioData(arrayBuffer);
  } finally {
    void decodeCtx.close();
  }

  // OfflineAudioContext fait le rééchantillonnage ET le downmix mono d'un coup.
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

/** PCM float32 [-1,1] → WAV 16 bits mono (en-tête RIFF de 44 octets). */
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
  view.setUint32(16, 16, true); // taille du bloc fmt
  view.setUint16(20, 1, true); // PCM entier
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // octets/seconde
  view.setUint16(32, 2, true); // alignement de bloc
  view.setUint16(34, 16, true); // bits par échantillon
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
