# Transcription Engine Decision Brief

**Status:** PROPOSED comparison for decision Q11 (`DECISIONS_AND_OPEN_QUESTIONS.md`). **No engine is chosen, installed or tested.** Nothing was downloaded, uploaded or called while writing this; no creator footage was used.

**How to read the evidence labels.** **[VERIFIED-LOCAL]** = checked on this machine or in this repository’s code on 2026-11-08. **[RECALLED]** = general knowledge from before this session, *not* checked here and possibly outdated. **[UNKNOWN]** = nobody has measured it for this creator’s speech. Vendor claims are marked as claims. Quality on the creator’s Croatian/Kajkavian speech is **[UNKNOWN] for every option** until a sample is measured (see “Evaluation protocol”).

## 1. What the project needs (from the code and docs)

- Croatian; Kajkavian; English; Croatian/English switching **inside** a sentence.
- Wording preserved: slang, repetitions, false starts, hesitations, diacritics — no silent “correction” toward standard language (`CREATOR_DNA.md` §6; the transcript format stores text exactly as returned).
- Segment timestamps required; word timestamps and speaker ids optional (the provider interface **rejects** results that include them without declaring support — **[VERIFIED-LOCAL]**, `transcription.run_transcription`).
- Local operation preferred (creator footage staying on the machine; MASTER.md describes a free/low-cost setup).
- Windows; minimal dependencies; replaceable provider behind `TranscriptionProvider` (`capabilities()`, `transcribe(request)` returning `TranscriptSegment`s, input = a 16 kHz mono WAV analysis copy).
- Whatever the engine, its output is **unverified** until a named human reviews it (implemented review layer). The engine only needs to be a good *draft*.

## 2. This machine

**[VERIFIED-LOCAL]** AMD Ryzen 3 7320U (8 threads), 13.8 GB RAM, AMD Radeon 610M integrated graphics (2 GB reported), ~174 GB free on C:. No NVIDIA GPU / CUDA (`nvidia-smi` absent). No ASR-related Python package installed (checked: whisper, torch, onnx, vosk, nemo, transformers, ctranslate2, pyannote, speechbrain, sherpa, numpy). Python 3.14. FFmpeg 9.0.1 (gyan.dev full build) with `--enable-whisper`, `--enable-vulkan`, `--enable-opencl`, and a `whisper` audio filter. Whether any whisper.cpp model file already exists on the disk was **not established**.

Implication: realistic options are **CPU inference**, possibly Vulkan on the weak iGPU (speed **[UNKNOWN]**). Large models on this CPU are likely slow — plausibly several times slower than real time **[RECALLED/assumed, unmeasured]**; the practical question is “how many hours of footage per week”, which is not yet known.

## 3. Candidates

| | A. FFmpeg `whisper` filter (whisper.cpp) | B. faster-whisper (CTranslate2, Python) | C. OpenAI `whisper` (PyTorch) | D. NVIDIA Parakeet/Canary-class via ONNX or NeMo | E. Hosted ASR APIs (OpenAI, Google, Azure, Deepgram, AssemblyAI, ElevenLabs, Groq-hosted Whisper …) | F. LLM audio transcription (e.g. Gemini) |
|---|---|---|---|---|---|---|
| Local / offline | Yes (after a one-time model file download) | Yes (after model download) | Yes | Yes | **No — audio leaves the machine** | **No** |
| Installed here | **FFmpeg filter present [VERIFIED-LOCAL]**; model file unknown | No (pip packages needed) | No (PyTorch is large) | No | n/a | n/a |
| Windows | Yes (it is the installed build) | Yes [RECALLED] | Yes [RECALLED] | NeMo on Windows is awkward [RECALLED]; ONNX paths easier | n/a | n/a |
| Croatian | Whisper models list Croatian as supported [RECALLED]; quality UNKNOWN | same model family, same caveat | same | Parakeet v3 reportedly covers Croatian among ~25 European languages [RECALLED, unsure]; quality UNKNOWN | Google/Azure list hr-HR [RECALLED]; others vary — check current lists | works in practice [RECALLED], not as a transcript tool |
| Kajkavian | **UNKNOWN; no model is known to target it.** Expect drift toward standard Croatian or misrecognition | same | same | same | no vendor is known to list it | same |
| Code-switching in a sentence | Whisper takes one language hint per window; behaviour on mixed sentences **UNKNOWN** | same | same | UNKNOWN | UNKNOWN (some vendors advertise multilingual models — claims) | LLMs handle mixing but may rewrite |
| Keeps fillers / false starts / repetitions | Whisper-family models are widely reported to drop disfluencies and “clean up” [RECALLED, unverified] — **a risk to the authenticity rule** | same | same | RNN-T/TDT models tend to be more literal [RECALLED, unsure] | mostly “clean verbatim” by default; some offer verbatim modes (claims) | **High risk: generative, may paraphrase/summarise** |
| Hallucination in silence/music | Known Whisper issue [RECALLED]; the filter exposes a VAD model option (`vad_model`, `vad_threshold`) **[VERIFIED-LOCAL options]** | built-in VAD filter [RECALLED] | no built-in VAD | n/a | usually handled by the vendor | possible |
| Segment timestamps | Yes (`format` text/srt/json) [VERIFIED-LOCAL options] | Yes [RECALLED] | Yes | Yes [RECALLED] | Yes (claims) | unreliable [RECALLED] |
| Word timestamps | **No word option exposed by the filter** [VERIFIED-LOCAL options]; `max_len` limits segment length | Yes (`word_timestamps`) [RECALLED] | Yes [RECALLED] | Yes (TDT) [RECALLED] | usually yes (claims) | no |
| Speaker diarisation | No | No (needs a separate model, e.g. pyannote: PyTorch + gated model access) [RECALLED] | No | partial/separate [RECALLED] | often built in (claims) | no |
| Cost / operation | Free; CPU time; disk for model | Free; CPU time; disk | Free; heavier | Free; heavier setup | **Per-hour fees or rate-limited free tiers; keys needed (Groq/Gemini keys exist via OmniRoute per MASTER.md — not used here)** | per-token |
| Dependencies | **None beyond FFmpeg + a model file** | several Python wheels | PyTorch | several / NeMo | HTTP client + keys | HTTP client + key |
| Effort to wrap in `TranscriptionProvider` | Low–medium: run `ffmpeg -af whisper=…:format=json`, parse; segment-level only | Low: call library, map segments and words | Low–medium | Medium–high | Medium (auth, retries, upload limits, privacy gate) | Medium; needs a verbatim-checking guard |
| Privacy | Local | Local | Local | Local | **Footage audio uploaded and possibly retained per vendor policy [check each]** | same |

## 4. Trade-offs in plain terms

- **Kajkavian and in-sentence code-switching are probably weak for every option.** No candidate is known to be trained for Kajkavian. This is why the human review layer is not optional, and why a measured sample matters more than any feature list.
- **Local keeps footage on the machine** but, on this CPU, may be slow and may force a smaller, less accurate model. **Hosted APIs are faster and add diarisation/word timing** but upload creator audio, cost money (or have rate limits), and add a privacy decision.
- **Literalness matters as much as accuracy.** A model that tidies the creator’s speech produces a transcript that *looks* better but removes the personality the project exists to keep. Measure dropped fillers, repetitions and normalised dialect words, not only word error rate.
- **Word timestamps** are needed only if later editing wants word-accurate cuts; the filter route cannot supply them, the Python route can.
- Diarisation is not needed for a mostly single-speaker creator; defer.

## 5. Recommendation (not a decision)

1. **Do not decide on paper.** Pick a shortlist, run all of it on the **same short, permitted sample** and compare (protocol below). The review layer already turns a corrected transcript into ground truth.
2. **Shortlist for the measurement:** (a) **FFmpeg `whisper` filter** — already installed, no new dependencies, lowest-risk first trial, segment-level only; (b) **faster-whisper** — the most likely production route if word timestamps and VAD are wanted; adds Python dependencies, so it needs your approval to install. Add (c) a **hosted API** to the comparison only if you explicitly accept uploading that sample (it would also serve as a quality ceiling).
3. **Treat the first engine as a draft generator.** Expect to review transcripts that feed DNA quotes or edit cuts; sort out the review-at-scale policy (Q28) alongside.
4. **Not recommended:** LLM transcription (F) as the primary engine (paraphrase risk); PyTorch `whisper` (C) over B (heavier, no advantage here).

## 6. Evaluation protocol (to run only with your permission, on footage you approve)

1. Choose 3–5 minutes containing: ordinary Croatian, Kajkavian, English, a mixed sentence, hesitations/repetitions, some silence and background noise.
2. Create the 16 kHz mono analysis copy with `media_analysis` (the original is not touched).
3. Run each shortlisted engine locally (hosted only with explicit consent), saving the raw output unchanged.
4. A human produces a corrected transcript once (this is also the first real use of the review layer).
5. Compare: word error rate; **dropped/added fillers and repetitions**; dialect words normalised to standard forms; English words mangled; hallucinated text in silence; timestamp drift (spot-check 10 segments); runtime vs audio length on this machine; memory use.
6. Use the result to set the ASR evidence weight (Q26) and to decide Q11.

## 7. Open facts to establish before deciding

Speed and memory on this laptop for each model size; whether Vulkan on the iGPU helps or fails; the creator’s tolerance for upload vs local; expected hours of footage per week; whether word-level timing is required by the editing design.
