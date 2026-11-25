# voice-custom — the voice mode, in your own voice (Fish Audio S2 Pro)

Fish Audio S2 Pro (4.6B, 44.1 kHz) cloning one reference voice, served as the
fourth engine of the `voice` container. It speaks the playground voice mode
contract (`/api/speak`, streamed PCM) that only Kyutai spoke before, and
announces it with `"conversation": true` in `/api/model-info` — the portal
checks that flag (`media::conversation_engine`), not an engine name.

```
recording ─▶ dataset (wavs/ + metadata.csv)
              │  reference/try_voice.sh     (throwaway container, GPU)
              ▼
         refs/<candidate>/ref.wav+ref.lab  +  samples/<candidate>/NN.wav   ◀── listen, pick one
              │  sudo install-voice.sh refs/<candidate> <name>
              ▼
   /var/lib/voice-custom/<name>   /var/lib/voice-custom/active ─▶ <name>
              │  Admin → Modèles → « custom-voice » → Lancer
              ▼
   voice-custom-recreate.sh  →  container « voice » (fish-s2pro-voice)
              │  /api/speak
              ▼
   playground voice mode reads the LLM's answer with your voice
```

## Why a reference and not a fine-tune

S2 Pro clones a voice in context from a clip and its exact transcript: that is
what it is built and ranked for (best open-weights model on the Artificial
Analysis controlled-voice arena). Fish Audio's fine-tuning notes advise against
fine-tuning this RL-aligned checkpoint (it shifts the model distribution) and
state that their LoRA learns the speaking style, not the timbre — the timbre
still comes from the reference. So the « personal voice » is a well-chosen
reference, encoded once and cached by the engine.

## 1. Build the image (once, several GB)

```bash
docker build -f voice-custom/Dockerfile -t fish-s2pro-voice:0f8a12e-cu130 voice-custom/
```

fish-speech pins torch 2.8, which has no build for the GB10 (sm_121): the image
installs its inference code without those pins on torch 2.10 cu130, with a
dependency set import-tested against commit `0f8a12e`.

## 2. Try candidate references

```bash
voice-custom/reference/try_voice.sh /path/to/dataset ~/voice-runs/s2
```

Builds four references from the dataset — `court` (14 s), `moyen` (18 s),
`long` (27 s) from the conversational text 0904, `varie` (20 s) from short
replies in several tones — then renders the same eight sentences with each
through S2 Pro, and prints each one's real-time factor and time to the first
sentence. `CANDIDATES="name=clip1,clip2;…"` overrides the sets. Only use clips
whose transcript is exact: S2 Pro reproduces what it reads.

## 3. Install the one that sounds like you

```bash
sudo voice-custom/install-voice.sh ~/voice-runs/s2/refs/moyen mael
```

Then Admin → Modèles → add a voice entry with the variant « Voix personnalisée
(S2 Pro, mode vocal) » (`custom-voice`) and launch it. Installing never restarts
anything; going back to an older voice is `install-voice.sh` on its directory,
then a relaunch.

## Memory and latency

~17 GiB of unified memory: 9.1 GB of weights, 1.9 GB codec, ~4.8 GB of KV cache
(32k context). The first launch downloads ~11 GB into the host HF cache.

The fish-speech PyTorch engine returns audio per text chunk, not per frame: one
sentence comes back in one piece. The voice mode already sends one sentence per
call, and the server splits a long sentence once at its first comma. torch.compile
is on by default (`S2PRO_COMPILE=1`) and falls back to eager if it cannot build;
`model-info` reports which one runs. Whether this is conversational on the GB10 is
for `try_voice.sh` to measure — if not, the next step is a streaming backend
(vLLM-Omni or SGLang-Omni serve S2 Pro with frame-level streaming).

## API (port 8004, voice_net only)

| Route | Body | Answer |
|---|---|---|
| `GET /api/model-info` | — | `engine: "fish-s2-pro"`, `conversation: true`, `loaded`, `voices`, `default_voice`, `sample_rate` (44100), `compiled` |
| `POST /api/speak` | `{text, voice}` (≤ 1000 chars) | raw PCM s16le mono, `X-Sample-Rate` |
| `POST /tts` | `{text, output_format: wav\|mp3\|pcm}` (≤ 3000 chars) | the whole clip (used by `/v1/audio/speech`) |

Licence: the S2 Pro weights are under the Fish Audio Research License —
personal, non-commercial use.
