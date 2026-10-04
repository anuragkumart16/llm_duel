# LLM Duel

The main script is `llm_duel_with_voice.py`. It connects to the two configured
LLM servers and reads each completed reply aloud using offline text-to-speech.

## Setup

```bash
uv sync
```

On macOS, the script can use the built-in `say` command if the Python speech
engine cannot initialize. On Linux, install the system speech backend required
by pyttsx3 (usually `espeak-ng`).

## Run

```bash
uv run python llm_duel_with_voice.py
```

Speech defaults to a slower, easier-to-follow rate of 115 words per minute.
Adjust it or turn it off when needed:

```bash
uv run python llm_duel_with_voice.py --voice-rate 95
uv run python llm_duel_with_voice.py --no-voice
```