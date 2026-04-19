# claude-speak

A Claude Code plugin that speaks each response aloud in **natural spoken English** — 100% local, zero paid APIs.

**Stack:**
- **TTS:** [Kokoro](https://github.com/thewh1teagle/kokoro-onnx) (ONNX, ~170MB model)
- **Rewrite:** `claude -p --model sonnet` (uses your existing Claude subscription)
- **STT:** [whisper.cpp](https://github.com/ggerganov/whisper.cpp) (local, Homebrew)

Drop the plugin in, answer no questions on install, and it just works offline. All three stages auto-install their dependencies on first use.

## Why not just pipe text to TTS?

Most TTS plugins read raw markdown verbatim — you hear "asterisk asterisk bold asterisk asterisk" and file paths spelled letter-by-letter. claude-speak inserts a rewrite step: a small Claude model summarizes the response into natural spoken English first, stripping code/paths/markdown. The audio sounds like a peer giving a verbal update, not a screen reader.

## Features

- **Sentence-streaming pipeline** — plays sentence 1 while sentence 2 synthesizes in the background; near-zero gap once playback starts.
- **Auto-dictation loop** — after each response, records from your mic, transcribes, and feeds the transcript back to Claude as your next prompt. Fully hands-free.
- **Cancel by voice or silence** — say "cancel"/"skip"/"stop", or just stay silent.
- **macOS notifications** when recording starts, finishes, or cancels.
- **Kill-previous** on new turn: starting to respond kills any audio still playing from the previous turn.
- **Per-module CLI** — every backend runs standalone for benchmarking / testing.

## Setup

### Prerequisites

- macOS (uses `afplay` / `osascript`; recording via `sounddevice` works cross-platform but playback is macOS-only right now)
- Python 3.8+
- Claude Code with an active Claude.ai login
- [Homebrew](https://brew.sh) (for whisper.cpp auto-install)
- `ffmpeg` + `ffplay` (`brew install ffmpeg`) — optional but recommended for pitch-preserving playback rate
- `sounddevice` + `numpy` (`pip3 install --user sounddevice numpy`) — optional but cuts the mic cold-start

### Install

```
/plugin marketplace add thu-san/claude-speak
/plugin install claude-speak@thu-san
/reload-plugins
```

Or for local development against a checkout:

```
/plugin marketplace add /absolute/path/to/claude-speak
/plugin install claude-speak@thu-san
/reload-plugins
```

### First session

A `SessionStart` hook kicks off a background installer the first time you open Claude Code after install. You'll see macOS notifications as each component lands:

1. `sounddevice` + `numpy` (pip, ~5MB) — mic capture
2. `silero-vad` + `torch` + `onnxruntime` (pip, ~500MB) — speech detection
3. `kokoro-onnx` (pip, ~15MB) + Kokoro model (~170MB) + voices (~27MB) — TTS
4. `whisper.cpp` (brew) + `ggml-small.en.bin` (~500MB) — STT

**Total first-time download:** roughly 1GB. Takes ~5-10 minutes depending on bandwidth. A marker file (`$CLAUDE_PLUGIN_DATA/.installed_v1`) prevents re-running.

**To run it manually or refresh:**
```
/claude-speak:speak install
/claude-speak:speak install --force   # redo even if marker exists
```

**Or from the terminal:**
```bash
cd /path/to/claude-speak/scripts
python3 -m claude_speak.install          # shows per-step progress
python3 -m claude_speak.install --quiet  # notifications only
```

After that, everything runs fully offline.

## Slash command

| Command | Effect |
|---|---|
| `/speak on` / `/speak off` | Toggle the plugin without uninstalling |
| `/speak stop` | Kill current playback |
| `/speak voice <name>` | Set Kokoro voice (`/speak voices` to list) |
| `/speak voices` | List all Kokoro voices with the active one starred |
| `/speak rate <0.25-4.0>` | Playback speed (pitch-preserving via ffplay atempo) |
| `/speak silence <0.3-10>` | Silence seconds before recording stops |
| `/speak mode stream \| whole` | `stream`: play sentence-by-sentence (fast first audio). `whole`: one big synthesis then play (smoother prosody, slower start) |
| `/speak dictate on \| off` | Enable the hands-free voice loop |
| `/speak notifications on \| off` | Speak Claude Code notifications (permission prompts, idle, etc.) aloud |
| `/speak status` | Show config + tooling availability |
| `/claude-speak:daemon-status` | Show daemon uptime / pid / in-flight |
| `/claude-speak:daemon-restart` | Restart the daemon (pick up code changes) |
| `/claude-speak:daemon-stop` | Stop the daemon (next request respawns it unless `daemon: false`) |

## Kokoro voices

Prefix legend:
- `af_*` — US female (e.g. `af_sarah`, `af_nova`, `af_heart`)
- `am_*` — US male (e.g. `am_onyx`, `am_michael`, `am_puck`)
- `bf_*` — UK female (`bf_emma`, `bf_alice`, `bf_isabella`, `bf_lily`)
- `bm_*` — UK male (`bm_george`, `bm_daniel`, `bm_fable`, `bm_lewis`)

Run `/speak voices` for the full list.

## Configuration

Lives at `$CLAUDE_PLUGIN_DATA/config.json`. All keys have sensible defaults — only set what you want to change.

Relevant fields:

```jsonc
{
  "enabled": true,
  "kokoro_voice": "af_sarah",
  "kokoro_speed": 1.0,
  "playback_rate": 1.0,
  "mode": "stream",
  "auto_dictation": false,
  "claude_model": "sonnet",
  "record_silence_seconds": 1.2,
  "record_max_seconds": 60,
  "cancel_phrases": ["cancel", "skip", "never mind", "nevermind", "stop"]
}
```

Override `CLAUDE_SPEAK=0` in the environment to mute for a single session.

## Developing against a local marketplace

If you've installed claude-speak from a **local checkout** (e.g. `/plugin marketplace add /path/to/claude-speak`) instead of from GitHub (`thu-san/claude-speak`), your install's data dir is `~/.claude/plugins/data/claude-speak-<your-marketplace-name>/` — not the production default `claude-speak-thu-san/`.

Claude Code sets `CLAUDE_PLUGIN_DATA` correctly for hook invocations, so the Stop / Notification / SessionStart flow always finds the right dir. But **terminal commands** (`python3 -m claude_speak.stt`, `… .rewrite`, `… turn`, etc.) have no hook context and default to `claude-speak-thu-san`. Set the env var in your shell rc so terminal commands follow your local install:

```bash
# ~/.zshrc (or ~/.bashrc)
export CLAUDE_PLUGIN_DATA=~/.claude/plugins/data/claude-speak-<your-marketplace-name>
```

Example — if your marketplace is named `claude-speak-local`:

```bash
export CLAUDE_PLUGIN_DATA=~/.claude/plugins/data/claude-speak-claude-speak-local
```

Production users (`claude-speak@thu-san` from GitHub) never need to set this — they get the canonical dir automatically.

## Testing individual modules from the CLI

Each backend — rewrite, TTS, STT, daemon — is its own module and can run standalone. Pipeline from Stop hook:

```
raw Claude reply → rewrite (claude -p) → TTS (Kokoro) → ffplay → record (Silero VAD) → STT (whisper.cpp) → decision:block
   └─ debug with ─┘   └──── debug with ────┘                     └─────────── debug with ────────────────┘
    rewrite CLI       tts.kokoro CLI                              stt CLI
```

Invoke any layer in isolation to identify where time goes or what's broken without the full hook flow.

```bash
# path to the checkout; adjust if installed elsewhere
cd /path/to/claude-speak/scripts

# Kokoro: synthesize and play, with per-sentence timing
python3 -m claude_speak.tts.kokoro \
  --text "Hello from Kokoro running locally on my machine." \
  --voice af_nova --speed 1.0 --rate 1.2

# Benchmark against the bundled sample (~1500 chars, 17 sentences)
# Default is overlapped per-sentence (fast first audio); --whole forces one call
python3 -m claude_speak.tts.kokoro --file fixtures/input.txt
python3 -m claude_speak.tts.kokoro --file fixtures/input.txt --voice bm_george --rate 1.3
python3 -m claude_speak.tts.kokoro --file fixtures/input.txt --whole       # single synthesis

# List voices or read from stdin
python3 -m claude_speak.tts.kokoro --list-voices
echo "Pipe this in." | python3 -m claude_speak.tts.kokoro

# Rewrite via claude -p (bypasses the daemon — useful for debugging timeouts).
# fixtures/rewrite_input.txt is a 1.8KB realistic reply for reproducible tests.
python3 -m claude_speak.rewrite --file fixtures/rewrite_input.txt
python3 -m claude_speak.rewrite --file fixtures/rewrite_input.txt --model sonnet --timeout 120
python3 -m claude_speak.rewrite --text "Raw assistant response..."

# Record AND transcribe in one shot.
# Routes through the warm daemon by default; --no-daemon to bypass.
python3 -m claude_speak.stt
python3 -m claude_speak.stt --silence 0.8 --max 10 --lang en
python3 -m claude_speak.stt --no-daemon

# Transcribe an existing WAV (skip recording).
# After any record, the "🎤 recorded ... → /path/input.wav" log line shows
# the exact file — copy that path here to A/B-test models on the same audio.
python3 -m claude_speak.stt ~/.claude/plugins/data/claude-speak-thu-san/input.wav
python3 -m claude_speak.stt ~/.claude/plugins/data/claude-speak-thu-san/input.wav \
    --model ggml-tiny.en.bin --threads 8

# End-to-end: run the FULL Stop-hook pipeline against canned input.
# rewrite → TTS → speak → listen → STT → prints the transcribed reply.
# This is what fires when Claude finishes a turn — just sourced from a
# file / --text instead of a live transcript. Each phase is timed in
# speak.log so you can see exactly where seconds go.
python3 -m claude_speak turn --file fixtures/rewrite_input.txt
python3 -m claude_speak turn --text "Done — all 37 tests passing on ARM."
python3 -m claude_speak turn --file fixtures/rewrite_input.txt --no-daemon

# Notification hook: pipe a canned Claude Code Notification payload
# through the shim — closest to what fires on a real permission prompt.
echo '{"session_id":"x","hook_event_name":"Notification","message":"Claude needs your permission to use Bash","title":"Permission needed","notification_type":"permission_prompt"}' \
  | python3 announce.py

# Same thing via the daemon directly (skip the shim + stdin parsing).
# rewrite=False + voice_loop=False = speak verbatim, no mic recording after.
python3 -c "
from claude_speak.daemon import send_request
print(send_request({
    'op': 'turn',
    'text': 'Heads up — the build finished.',
    'rewrite': False,
    'voice_loop': False,
}))
"

# Daemon control + persistent whisper-model switch
python3 -m claude_speak.stt --restart-daemon
python3 -m claude_speak.stt --set-model ggml-large-v3-turbo-q5_0.bin
python3 -m claude_speak.daemon status   # also: stop | restart | serve
```

### Whisper models

`--set-model` accepts any `ggml-*.bin` from the [whisper.cpp HF repo](https://huggingface.co/ggerganov/whisper.cpp). Curated presets (also exposed as slash commands):

| Model | Size | Speed | Accuracy |
|---|---:|---|---|
| `ggml-tiny.en.bin` | ~75MB | fastest | low |
| `ggml-base.en.bin` | ~150MB | fast | mediocre on dev terms |
| `ggml-small.en.bin` | ~466MB | balanced | **default** |
| `ggml-medium.en-q5_0.bin` | ~470MB | ~2× slower than small | better |
| `ggml-large-v3-turbo-q5_0.bin` | ~870MB | slowest | best |

Slash command equivalents: `/claude-speak:whisper-tiny` · `whisper-base` · `whisper-small` · `whisper-medium` · `whisper-large`. For anything else: `/claude-speak:whisper-model <ggml-*.bin>`.

## Fixtures

Two test inputs ship under `scripts/fixtures/`:

**`input.txt`** — ~1500 characters, 17 sentences, intentionally varied (decimals, semicolons, short/long sentences). Use it as a benchmarking input for the Kokoro CLI:

```bash
cd scripts
python3 -m claude_speak.tts.kokoro --file fixtures/input.txt --no-play
```

Default mode is overlapped per-sentence (sentence N+1 renders while sentence N plays). `--no-play` disables the player so you get clean synthesis timings. Use `--whole` to disable per-sentence and synthesize everything in one call.

**`rewrite_input.txt`** — ~1.8KB realistic assistant reply (prose + a Go code block + a numbered list + inline backticks + a trailing question). Use it to exercise the rewrite path — handy when you're debugging `claude -p` timeouts or comparing rewrite prompts:

```bash
cd scripts
python3 -m claude_speak.rewrite --file fixtures/rewrite_input.txt
time python3 -m claude_speak.rewrite --file fixtures/rewrite_input.txt --model sonnet

# full rewrite → TTS chain on a known input
python3 -m claude_speak.rewrite --file fixtures/rewrite_input.txt \
  | python3 -m claude_speak.tts.kokoro
```

## Tests

```bash
cd scripts && python3 -m pytest tests/ -v
```

37 tests covering cancel detection, sentence splitting, WAV framing, STT routing (daemon + in-process + fallback), whisper noise-marker filtering, and end-to-end daemon lifecycle (status / shutdown cleanup / bad-input handling).

## Architecture

```text
scripts/
  speak.py                       # thin shim invoked by the Stop hook
  speak_config.py                # /speak slash command handler
  claude_speak/                  # the actual plugin package
    main.py                      # hook entrypoint
    daemon.py                    # long-running Unix-socket daemon (warm models)
    defaults.py                  # DEFAULTS dict
    config.py                    # DATA_DIR, CONFIG_PATH, load_config, get_key
    logging.py                   # log, notify, beep
    audio.py                     # WAV framing, ffplay atempo chain, sync/async playback
    recording.py                 # sounddevice + ffmpeg capture with silence detection
    transcript.py                # read_last_turn, build_rewrite_input, sentence splitter
    rewrite.py                   # claude -p wrapper
    pipeline.py                  # run_pipeline (sentence queue + serial playback)
    tts/
      __init__.py                # synthesize() dispatch
      kokoro.py                  # local Kokoro ONNX
    stt/
      __init__.py                # dictate() — single entry; routes daemon ↔ in-process
      __main__.py                # CLI: `python -m claude_speak.stt`
      cancel.py                  # is_cancel (pure logic)
      whisper_cpp.py             # local whisper.cpp adapter — ensure() + transcribe()
  tests/                         # pytest suite
```

Every module has a single responsibility and a narrow public API. Adding a new TTS or STT backend is one file + one line in the dispatcher.

## How it works (condensed)

1. `Stop` hook fires after each Claude turn → `scripts/speak.py` → `claude_speak.main.main`.
2. Read the last user + assistant turn from the transcript JSONL.
3. `claude -p --model sonnet` rewrites the assistant reply into spoken English.
4. Sentences are yielded as rewrite tokens arrive. Each sentence → Kokoro synthesis in a worker thread (inference is serialized with a lock; ONNX is CPU-bound).
5. A player thread pulls audio in order and plays via `ffplay` stdin streaming.
6. If `auto_dictation: true`: the hook waits for playback to end, records via sounddevice with silence detection, transcribes with whisper.cpp, and emits `{"decision":"block","reason":"..."}` so Claude Code treats the transcript as the next user prompt.

## Latency budget

Rough per-reply wall-clock on Intel i9 CPU, start of Stop hook → first audio:

| Phase | Typical | Why |
|---|---:|---|
| Hook → daemon dispatch | <50ms | warm daemon, unix socket |
| `claude -p` startup | **3–5s** | **Node.js startup + plugin sync + keychain reads + TLS + API round trip — CLI overhead, not model inference.** Unavoidable without an API key. |
| Rewrite model inference | 1–2s | actual Sonnet response |
| Kokoro first-sentence synth | 1–2s | ONNX on CPU, fp32 |
| ffplay start | <200ms | |
| **→ first audio at** | **~5–9s** | |

The floor is `claude -p` overhead. If you're willing to set `ANTHROPIC_API_KEY` and hit `/v1/messages` directly, it drops to ~1s — but we keep the default path on subscription auth so users don't have to manage API keys. Document choice: simplicity > speed.

Knobs that actually help on the current path:
- `--setting-sources local` is already passed → skips hooks and MCPs from user/project settings (was the biggest unpredictable stall).
- `mode: stream` (default) → you hear sentence 1 while sentence 2+N synthesize in parallel. Don't switch to `whole` unless you're benchmarking.
- Apple Silicon cuts Kokoro + whisper times 3–5×. See [Future upgrade paths](#future-upgrade-paths-apple-silicon).

## Daemon

A long-running Unix-socket daemon (`claude_speak.daemon`) keeps Python + Silero VAD + Kokoro + whisper.cpp models warm in memory across turns. Without it, every Stop hook spawns a fresh Python process and reloads everything from scratch (~10-15s of pure cold-start before the mic even opens).

- One daemon per machine, shared across all Claude Code instances; auto-spawned on first Stop hook.
- Both the Stop hook (`scripts/speak.py`) and the standalone CLI (`python -m claude_speak.stt`) route through it via `stt.dictate(cfg)` — that's the single public entry point; routing is internal.
- Pass `--no-daemon` to the CLI, or set `daemon: false` in config, to bypass.
- `/claude-speak:daemon-status` / `daemon-restart` / `daemon-stop` to manage it.

## Notifications

Claude Code fires a `Notification` hook whenever the UI needs your attention — permission prompts ("Claude needs your permission to use Bash"), MCP elicitation dialogs, idle-waiting reminders, auth-success. claude-speak speaks those aloud so you can work with headphones on and still know when the turn is blocked on your click. The plugin doesn't (and can't) auto-approve — the decision still happens in the UI; it just tells you it's there.

- **How it works**: `scripts/announce.py` receives the hook JSON, routes to the daemon's `turn` op with `text=<message> rewrite=False voice_loop=False`. Spoken verbatim (no 3-5s `claude -p` floor) and speak-only (no mic recording afterward).
- **Toggle**: `/speak notifications on|off` (global). Per-type toggles live under `speak_notification_types` in config — `permission_prompt` / `elicitation_dialog` / `idle_prompt` are on by default, `auth_success` is off.
- **Interrupts the current turn**: if a notification arrives while Claude's reply is still being spoken, the reply gets cut so the notification is heard immediately. Permission prompts are higher priority.

See the **[Testing individual modules from the CLI](#testing-individual-modules-from-the-cli)** section above for commands that exercise the notification path end-to-end without waiting for a real Claude Code prompt.

## Future upgrade paths (Apple Silicon)

The current default — Kokoro for TTS — was chosen because it's the only local model that runs in real-time-ish on Intel CPU (~1-2s per sentence). It sounds good but slightly mechanical. On an Apple Silicon Mac (M2 and up), the Core ML / MPS / Neural Engine paths open up much more natural local TTS options:

| Model | Quality vs. Kokoro | Intel i9 CPU | M-series (M2+) | License |
|---|---|---|---|---|
| **MeloTTS** | slight step up | ~similar to Kokoro | ~similar | MIT |
| **XTTS-v2** (Coqui) | clearly more natural; voice cloning from 6s sample | 10-30s/sentence 🪦 | **1-3s ✅** | CPML (non-commercial) |
| **OpenVoice v2** | very natural; voice cloning | 15-40s/sentence 🪦 | **2-4s ✅** | Apache-2.0 |
| **F5-TTS** | most natural; emotion + pacing close to ElevenLabs | 30-90s/sentence 🪦 | 3-8s (M Pro/Max better) | CC-BY-NC |

Whisper itself stays — it's already state-of-the-art for local STT. Apple Silicon just makes it ~3-5× faster via the Core ML backend (e.g. `large-v3-turbo` encode drops from ~16s to ~3-5s).

When that switch happens, what would change in this plugin:

- `tts/__init__.py` — add a dispatcher (the same shape as `stt/__init__.py`) that picks Kokoro / XTTS / F5 by config.
- `tts/xtts.py` (new) — provider adapter, mirroring `tts/kokoro.py`.
- Wire a `coreml: true` flag through to `whisper-cli` when running on Apple Silicon (it'll auto-pick the right backend if the Core ML model is present).

For now (Intel CPU): Kokoro is the right call and trying any of the above hurts more than it helps. If you want a quality bump *today*, the practical options are (a) try a different Kokoro voice — `af_heart`, `am_michael`, `bm_lewis` sound less robotic to most ears — or (b) opt-in cloud (OpenAI `tts-1` / ElevenLabs).

## Troubleshooting

- **No sound after a message**: `tail -f $CLAUDE_PLUGIN_DATA/speak.log` (or check `~/.claude/plugins/data/claude-speak-*/speak.log`). The hook swallows exceptions to never break your Claude Code session, but they show up in the log.
- **Kokoro install fails**: run `pip3 install --user kokoro-onnx onnxruntime` manually and retry.
- **whisper.cpp not found**: `brew install whisper-cpp`.
- **"No speech detected" when you did speak**: lower the threshold — `/speak silence 0.8` or edit `record_silence_db` (e.g. `-40` for more permissive).

## Known limitations

- **`/plugin uninstall claude-speak` doesn't kill the running daemon.** Claude Code wipes the plugin data dir (venv, models, sockets) but the daemon process keeps running with stale fds, holding ~500MB until you reboot. Harmless (no future session can reach it) but wasteful — run `/claude-speak:daemon-stop` *before* uninstalling if you care.
- **Single shared daemon serializes requests across all Claude Code windows.** If three windows finish replies at the same instant, two block waiting for the first. Acceptable for typical use; if it bites, set `daemon: false` in config and pay the per-turn cold start.
- **Daemon auto-restarts on plugin code changes** — it watches mtimes of its own `.py` files every 30s and self-shuts when one moves forward, so you don't have to remember `/claude-speak:daemon-restart` after pulling.
- **`speak.log` rotates daily** to `speak.log.YYYY-MM-DD`. Older days stay on disk — delete what you don't need.

## Roadmap / TODO

### Conversational queue (multi-Claude turn-taking)

Today's daemon serializes requests with a single lock — when 3 Claude Code windows finish replies at the same instant, two of them block waiting for the first to play out, which is fine but unfair (and any auto-dictation reply only goes back to the most-recent window).

The intended design is a **FIFO turn queue**: each Stop hook enqueues a "conversational turn" (transcript path + session id) and blocks waiting for its result. A daemon worker pops turns in arrival order and runs the full flow for each one — `rewrite → speak → listen for user voice reply → return reply to that hook`. Three windows finishing at once means three turns served in arrival order; the user triages each in turn.

Sketch:
- Replace `_request_lock` with a `queue.Queue` + worker thread in `daemon.py`.
- New op `turn(transcript_path, session_id)`: enqueue + wait on a `Future`; worker pops, runs speak-then-listen, sets the Future.
- Per-item voice prefix when queue has >1 item ("From window B in `/proj`: ...") so the user knows who's talking.
- Voice commands: `skip` (drop current, advance), `defer` (push current back, advance), existing `cancel` already maps to "skip".
- Per-item TTL (e.g. 5 min): drop stale items so the queue doesn't pile up if the user goes AFK.
- Verify Stop hook timeout in Claude Code — if the hook can't block for minutes, daemon needs to give up and emit `{}` past the timeout, letting that turn end without a user reply.
- Daemon crash drops the queue → unblocked hooks treat as "no reply" (no decision:block) which is acceptable.

### Beyond the queue

- **Fuse speak + dictate into one daemon op** (`turn` above). Today they're two phases held together by the `auto_dictation` config flag.
- **Per-session state registry** in the daemon — `{session_id: {cwd, status, last_reply_excerpt, ts}}` updated from Stop hook payloads. Foundation for status voice commands and the longer-term Jarvis-style features (wake word, proactive narration, conversational memory). See conversation in PRs / issues for the full design discussion.

## License

MIT.
