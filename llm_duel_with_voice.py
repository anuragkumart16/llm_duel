#!/usr/bin/env python3
"""
llm_duel_with_voice.py - Make two LLMs (on two different Macs) talk to each
                          other, with YOU as the moderator in the middle.

Uses pyttsx3 for offline text-to-speech. Works on macOS / Linux. Python 3.8+.

QUICK START
    1. Edit the CONFIG section below (IPs, models, system prompts, topic).
    2. python3 llm_duel_with_voice.py --check        # verify both Macs answer
    3. python3 llm_duel_with_voice.py                # start the conversation
    4. python3 llm_duel_with_voice.py --topic "Is free will an illusion?" --turns 20
    5. python3 llm_duel_with_voice.py --voice-rate 115

LIVE CONTROLS (while it is running)
    SPACE   pause after the current message and open the moderator menu
    x       cut the current message off right now and open the menu
    q       quit (saves the transcript)
    Ctrl+C  same as 'x'

MODERATOR MENU (after pressing SPACE)
    Enter          resume
    <any text>     type a sentence (2+ chars) -> sent to BOTH as the Moderator
    m              multi-line / file message to BOTH
    a / b          private "director note" whispered to ONLY Mac A / Mac B
    t              change the topic
    s              edit system prompt of A / B / both (replace or append)
    d              apply a mood / character preset (or custom) to A / B / both
    n              choose who speaks next
    u              undo last event (re-roll a reply, remove a message)
    k              settings: temperature, max tokens, delay, turn limit, streaming
    i              show current prompts / moods / settings
    v              view the last few events
    w              save transcript now
    q              quit

HOW TO TEST ONE MAC MANUALLY WITH curl (Ollama example)
    curl http://192.168.1.XX:11434/api/chat -d '{
      "model": "llama3.1:8b",
      "messages": [{"role": "user", "content": "hi"}],
      "stream": false }'

SERVER SETUP NOTES
    Ollama:    on each Mac run   OLLAMA_HOST=0.0.0.0 ollama serve
               (default port 11434; allow incoming connections in the macOS firewall)
    LM Studio / llama.cpp / vLLM / anything OpenAI-compatible:
               set "api": "openai" and the right port (LM Studio default 1234).
               Ollama ALSO exposes /v1/chat/completions, so "openai" works there too.
VOICE
    Replies are read aloud after they finish generating. The default rate is
    intentionally slower than the system default so the conversation is easier
    to follow. Use --voice-rate to change it, or --no-voice to disable speech.
"""

import argparse
import json
import os
import re
import select
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

try:
    import termios
    import tty
except ImportError:  # e.g. Windows - the script still works, just without hotkeys
    termios = tty = None


# =============================================================================
#                                  CONFIG
#          Everything marked  ✏️  is a placeholder you should change.
# =============================================================================

# ---- Mac A -------------------------------------------------------------------
MAC_A = {
    "name": "Happy",
    "host": "10.7.22.50",
    "port": 11434,
    "api": "ollama",
    "path": "",
    "model": "gpt-oss:20b",
    "api_key": "",
    "temperature": 0.8,
    "max_tokens": 300,
    "color": "cyan",
    "mood": "",
    "system_prompt": (
        "You are {name}, a curious and thoughtful conversationalist. "
        "You are chatting with {other}. The current topic is: {topic}. "
        "Share your own view, ask good questions, and build on what {other} says."
    ),
}

# ---- Mac B -------------------------------------------------------------------
MAC_B = {
    "name": "Rounak",
    "host": "10.7.17.203",
    "port": 11434,
    "api": "ollama",
    "path": "",
    "model": "gpt-oss:20b",
    "api_key": "",
    "temperature": 0.8,
    "max_tokens": 300,
    "color": "magenta",
    "mood": "",
    "system_prompt": (
        "You are {name}, a curious and thoughtful conversationalist. "
        "You are chatting with {other}. The current topic is: {topic}. "
        "Share your own view, ask good questions, and build on what {other} says."
    ),
}

# ---- Session settings --------------------------------------------------------
TOPIC = "Your point of view on humanity, are they cruel?"      # ✏️ default topic (override with --topic)

# Prepended to the very first message that the first speaker receives.
OPENER = (                                      # ✏️
    "Let's start a conversation with {other} about: {topic}\n"
    "You speak first. Give your opening thoughts."
)

# Appended to BOTH system prompts. {name}, {other}, {topic} are filled in automatically.
SHARED_RULES = (                                # ✏️
    "Rules: reply only as {name} and never write {other}'s lines. "
    "Do not start your reply with your own name. Keep replies to 2 sentences. "
    "Messages labelled [Moderator] come from the human host - respond to them naturally "
    "and steer the conversation accordingly. Messages labelled [Private note] are for you "
    "alone; follow them but never mention them."
)

FIRST_SPEAKER = "A"          # ✏️ "A" or "B"
MAX_TURNS = 0                # ✏️ total messages before stopping (0 = unlimited)
DELAY_BETWEEN_TURNS = 1.5    # ✏️ seconds between messages (a nice window to press SPACE)
STREAM = True                # ✏️ True = show tokens live; False = wait for whole reply
REQUEST_TIMEOUT = 180        # ✏️ seconds to wait for a Mac before giving up
HISTORY_WINDOW = 0           # ✏️ keep only the last N messages per model (0 = everything)
STRIP_THINK = True           # ✏️ remove <think>...</think> blocks (reasoning models) from history
MODERATOR_LABEL = "Moderator"   # ✏️ how your interjections are labelled to the models
TRANSCRIPT_DIR = "transcripts"  # ✏️ where .md and .json transcripts are saved ("" = don't save)
VOICE_ENABLED = True             # ✏️ read completed replies aloud
VOICE_RATE = 115                 # ✏️ words per minute; lower is slower

# ---- Mood / character presets (menu option 'd') ------------------------------
# ✏️ Add your own! The text is appended to that model's system prompt.
MOODS = {
    "neutral":          "Speak naturally and calmly.",
    "enthusiastic":     "You are extremely enthusiastic and upbeat about everything.",
    "sarcastic":        "You are dryly sarcastic, but never cruel.",
    "grumpy":           "You are grumpy and impatient, and easily annoyed.",
    "philosopher":      "You think like an ancient philosopher; you answer with questions and analogies.",
    "devils_advocate":  "Always argue the opposite of what the other person just said, in good faith.",
    "skeptic":          "You demand evidence for every claim and point out logical gaps.",
    "poetic":           "You speak in vivid, lyrical, metaphor-rich language.",
    "pirate":           "You talk like a pirate captain. Arr.",
    "five_year_old":    "You are a curious five-year-old who keeps asking 'but why?'.",
    "scientist":        "You are a precise scientist; you cite mechanisms and limits of knowledge.",
    "comedian":         "You are a stand-up comedian; make jokes but still make a point.",
    "diplomat":         "You are a smooth diplomat who looks for common ground.",
    "conspiracy":       "You are a playful conspiracy theorist who sees hidden patterns everywhere.",
    "teacher":          "You are a patient teacher who explains with simple examples.",
    "angry":            "You are furious about this topic and it shows.",
}

# =============================================================================
#                          END OF CONFIG - code below
# =============================================================================

DEFAULT_PATHS = {"ollama": "/api/chat", "openai": "/v1/chat/completions"}
ANSI = {
    "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m", "magenta": "\033[35m",
    "blue": "\033[34m", "red": "\033[31m", "white": "\033[37m", "dim": "\033[2m",
    "bold": "\033[1m", "reset": "\033[0m",
}
USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text, color):
    if not USE_COLOR:
        return text
    return f"{ANSI.get(color, '')}{text}{ANSI['reset']}"


class Quit(Exception):
    pass


class Speech:
    """Small offline speech wrapper with a clear fallback on macOS."""

    def __init__(self, enabled=True, rate=VOICE_RATE):
        self.enabled = enabled
        self.rate = rate
        self.engine = None
        if not enabled:
            return
        try:
            import pyttsx3
            self.engine = pyttsx3.init()
            self.engine.setProperty("rate", rate)
        except Exception as e:
            if sys.platform == "darwin" and self._has_macos_say():
                self.engine = "macos"
            else:
                self.enabled = False
                print(c(f"  speech disabled: could not initialize pyttsx3 ({e})", "yellow"))

    @staticmethod
    def _has_macos_say():
        return shutil.which("say") is not None

    def say(self, text):
        if not self.enabled or not text:
            return
        spoken = re.sub(r"\s+", " ", text).strip()
        if not spoken:
            return
        try:
            if self.engine == "macos":
                # macOS `say` uses a roughly 200 wpm baseline.
                import subprocess
                subprocess.run(["say", "-r", str(max(60, int(self.rate))), spoken], check=True)
            else:
                self.engine.say(spoken)
                self.engine.runAndWait()
        except (OSError, RuntimeError) as e:
            print(c(f"  speech failed: {e}", "yellow"))


# ----------------------------------------------------------------------------
# Agents & state
# ----------------------------------------------------------------------------
class Agent:
    def __init__(self, key, cfg):
        self.key = key
        self.cfg = cfg
        self.name = cfg["name"]
        self.system = cfg["system_prompt"]
        self.mood = cfg.get("mood", "")
        self.temperature = cfg.get("temperature", 0.8)
        self.max_tokens = cfg.get("max_tokens", 300)

    @property
    def url(self):
        path = self.cfg.get("path") or DEFAULT_PATHS[self.cfg["api"]]
        return f"http://{self.cfg['host']}:{self.cfg['port']}{path}"

    def system_text(self, state, other):
        parts = [self.system, SHARED_RULES]
        if self.mood.strip():
            parts.append("Your current mood / style: " + self.mood.strip())
        text = "\n\n".join(p for p in parts if p.strip())
        return (text.replace("{name}", self.name)
                    .replace("{other}", other.name)
                    .replace("{topic}", state.topic))

    def build_messages(self, state):
        other = state.other(self.key)
        msgs = []

        def add(role, content):
            if msgs and msgs[-1]["role"] == role:     # merge same-role neighbours
                msgs[-1]["content"] += "\n\n" + content
            else:
                msgs.append({"role": role, "content": content})

        for ev in state.events:
            k = ev["kind"]
            if k == "say":
                add("assistant" if ev["who"] == self.key else "user", ev["text"])
            elif k == "mod":
                add("user", f"[{MODERATOR_LABEL}]: {ev['text']}")
            elif k == "private" and ev["to"] == self.key:
                add("user", ev["text"])

        if HISTORY_WINDOW and len(msgs) > HISTORY_WINDOW:
            msgs = msgs[-HISTORY_WINDOW:]
            while msgs and msgs[0]["role"] != "user":
                msgs.pop(0)

        return [{"role": "system", "content": self.system_text(state, other)}] + msgs


class State:
    def __init__(self, topic, first):
        self.topic = topic
        self.agents = {"A": Agent("A", MAC_A), "B": Agent("B", MAC_B)}
        self.events = []
        self.next_key = first
        self.max_turns = MAX_TURNS
        self.delay = DELAY_BETWEEN_TURNS
        self.stream = STREAM
        self.started = datetime.now()
        self.base = None
        if TRANSCRIPT_DIR:
            os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
            self.base = os.path.join(TRANSCRIPT_DIR, "duel_" + self.started.strftime("%Y%m%d_%H%M%S"))

    def other(self, key):
        return self.agents["B" if key == "A" else "A"]

    @property
    def turns(self):
        return sum(1 for e in self.events if e["kind"] == "say")

    def add(self, kind, **kw):
        ev = {"kind": kind, "time": datetime.now().isoformat(timespec="seconds"), **kw}
        self.events.append(ev)
        if kind == "say":
            self.next_key = "B" if kw["who"] == "A" else "A"
        self.save()
        return ev

    def undo(self):
        if len(self.events) <= 1:       # keep the opener
            return None
        ev = self.events.pop()
        if ev["kind"] == "say":
            self.next_key = ev["who"]   # same speaker re-rolls
        self.save()
        return ev

    def save(self):
        if not self.base:
            return
        try:
            with open(self.base + ".json", "w", encoding="utf-8") as f:
                json.dump({"topic": self.topic, "events": self.events}, f, indent=2, ensure_ascii=False)
            lines = [f"# LLM duel - {self.topic}", ""]
            for ev in self.events:
                if ev["kind"] == "say":
                    lines += [f"**{self.agents[ev['who']].name}:** {ev['text']}", ""]
                elif ev["kind"] == "mod":
                    lines += [f"> **{MODERATOR_LABEL}:** {ev['text']}", ""]
                elif ev["kind"] == "private":
                    lines += [f"> _(private to {self.agents[ev['to']].name})_ {ev['text']}", ""]
            with open(self.base + ".md", "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except OSError as e:
            print(c(f"(could not save transcript: {e})", "red"))


# ----------------------------------------------------------------------------
# Keyboard watcher (SPACE / x / q while the conversation runs)
# ----------------------------------------------------------------------------
class KeyWatcher:
    def __init__(self):
        self.enabled = bool(termios and sys.stdin.isatty())
        self.old = None
        self.pause = False
        self.cut = False
        self.quit = False

    def start(self):
        if self.enabled and self.old is None:
            self.old = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())

    def stop(self):
        if self.old is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old)
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
            self.old = None

    def poll(self):
        if self.old is None:
            return
        fd = sys.stdin.fileno()
        while select.select([fd], [], [], 0)[0]:
            for ch in os.read(fd, 64).decode("utf-8", "ignore"):
                if ch == " ":
                    self.pause = True
                elif ch in "xX":
                    self.pause = self.cut = True
                elif ch in "qQ":
                    self.quit = True

    def wait(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.poll()
            if self.pause or self.quit:
                return
            time.sleep(0.05)


# ----------------------------------------------------------------------------
# Talking to the Macs
# ----------------------------------------------------------------------------
def call_llm(agent, messages, watcher, on_token, stream):
    """Returns (text, interrupted). Raises on network / HTTP errors."""
    api = agent.cfg["api"]
    if api == "ollama":
        payload = {"model": agent.cfg["model"], "messages": messages, "stream": stream,
                   "options": {"temperature": agent.temperature, "num_predict": agent.max_tokens}}
    else:
        payload = {"model": agent.cfg["model"], "messages": messages, "stream": stream,
                   "temperature": agent.temperature, "max_tokens": agent.max_tokens}

    headers = {"Content-Type": "application/json"}
    if agent.cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + agent.cfg["api_key"]
    req = urllib.request.Request(agent.url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    out, interrupted = [], False
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            if not stream:
                obj = json.loads(resp.read().decode("utf-8"))
                text = (obj["message"]["content"] if api == "ollama"
                        else obj["choices"][0]["message"]["content"])
                on_token(text)
                return text, False
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line:
                    continue
                if api == "openai":
                    if not line.startswith("data:"):
                        continue
                    line = line[5:].strip()
                    if line == "[DONE]":
                        break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if api == "ollama":
                    piece = (obj.get("message") or {}).get("content", "")
                    done = obj.get("done", False)
                else:
                    choices = obj.get("choices") or [{}]
                    piece = (choices[0].get("delta") or {}).get("content") or ""
                    done = False
                if piece:
                    out.append(piece)
                    on_token(piece)
                watcher.poll()
                if watcher.cut:
                    interrupted = True
                    break
                if done:
                    break
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"HTTP {e.code} from {agent.url}: {body}")
    return "".join(out), interrupted


def clean(text):
    if STRIP_THINK:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    return text.strip()


# ----------------------------------------------------------------------------
# Terminal helpers & moderator menu
# ----------------------------------------------------------------------------
def ask(prompt):
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        raise Quit()


def read_text(label):
    """One line by default. Type '<<' for multi-line (end with a lone '.'), or '@file.txt' to load a file."""
    first = ask(f"{label}\n  (one line, '<<' for multi-line, '@path' to load a file; empty = cancel)\n> ").strip()
    if not first:
        return ""
    if first.startswith("@"):
        try:
            with open(os.path.expanduser(first[1:]), encoding="utf-8") as f:
                return f.read().strip()
        except OSError as e:
            print(c(f"  could not read file: {e}", "red"))
            return ""
    if first == "<<":
        lines = []
        print("  (finish with a single '.' on its own line)")
        while True:
            line = ask("")
            if line.strip() == ".":
                break
            lines.append(line)
        return "\n".join(lines).strip()
    return first


def pick_agents(state, label, default_both=True):
    ans = ask(f"{label} [a/b/both{' (default)' if default_both else ''}]: ").strip().lower()
    if ans == "a":
        return [state.agents["A"]]
    if ans == "b":
        return [state.agents["B"]]
    if ans in ("both", "") and default_both:
        return list(state.agents.values())
    return []


def show_event(state, ev):
    k = ev["kind"]
    if k == "mod":
        print(c(f"\n  🎤 [{MODERATOR_LABEL}] {ev['text']}", "yellow"))
    elif k == "private":
        print(c(f"\n  🤫 [private -> {state.agents[ev['to']].name}] {ev['text']}", "magenta"))
    elif k == "say":
        a = state.agents[ev["who"]]
        print(c(f"\n  {a.name}:", a.cfg["color"]) + f" {ev['text']}")


def show_info(state):
    print(c("\n  Topic: ", "bold") + state.topic)
    for a in state.agents.values():
        print(c(f"\n  {a.key}: {a.name}  ({a.url}, model {a.cfg['model']})", a.cfg["color"]))
        print(f"    temperature={a.temperature}  max_tokens={a.max_tokens}")
        print(f"    mood: {a.mood or '(none)'}")
        print("    system prompt: " + a.system.replace("\n", "\n      "))
    print(f"\n  turns={state.turns}  limit={state.max_turns or 'unlimited'}  "
          f"delay={state.delay}s  stream={state.stream}  next={state.agents[state.next_key].name}")


def menu_text(state):
    nxt = state.agents[state.next_key].name
    return f"""
{c('─── PAUSED ───', 'bold')}  turn {state.turns}, next speaker: {c(nxt, state.agents[state.next_key].cfg['color'])}
  Enter  resume                    <text>  say it to BOTH as {MODERATOR_LABEL}
  m  multi-line/file msg to both   a / b   whisper to ONLY A / B
  t  change topic                  s  edit system prompt
  d  mood / character preset       n  choose next speaker
  u  undo last event               k  settings (temp, tokens, delay, limit, stream)
  i  info                          v  view recent    w  save    q  quit"""


def pause_menu(state, watcher):
    watcher.stop()
    try:
        while True:
            print(menu_text(state))
            choice = ask("> ").strip()
            if choice == "":
                return
            low = choice.lower()

            if len(choice) > 1 and low not in ("quit", "exit"):
                show_event(state, state.add("mod", text=choice))
                continue
            if low in ("q", "quit", "exit"):
                raise Quit()

            if low == "m":
                text = read_text(f"Message to BOTH (as {MODERATOR_LABEL}):")
                if text:
                    show_event(state, state.add("mod", text=text))

            elif low in ("a", "b"):
                key = low.upper()
                who = state.agents[key]
                text = read_text(f"Private note for {who.name} only (the other model won't see it):")
                if text:
                    show_event(state, state.add(
                        "private", to=key, text=f"[Private note]: {text}"))

            elif low == "t":
                text = read_text("New topic:")
                if text:
                    state.topic = text
                    show_event(state, state.add(
                        "mod", text=f"Change of topic. The new topic is: {text}"))

            elif low == "s":
                for a in pick_agents(state, "Edit system prompt of"):
                    print(f"\n  Current ({a.name}): {a.system}\n")
                    mode = ask(f"  [r]eplace / [a]ppend / [c]ancel for {a.name} (default cancel): ").strip().lower()
                    if mode in ("r", "a"):
                        text = read_text(f"New text for {a.name}:")
                        if text:
                            a.system = text if mode == "r" else a.system + "\n" + text
                            print(c(f"  ✓ {a.name}'s system prompt updated (applies from its next message)", "green"))

            elif low == "d":
                names = list(MOODS)
                for i, n in enumerate(names, 1):
                    print(f"   {i:2}. {n:16} {MOODS[n]}")
                print("    0. clear mood      c. custom text")
                sel = ask("  Choose number / c / 0: ").strip().lower()
                mood = None
                if sel == "0":
                    mood = ""
                elif sel == "c":
                    mood = read_text("Custom mood / character:")
                elif sel.isdigit() and 1 <= int(sel) <= len(names):
                    mood = MOODS[names[int(sel) - 1]]
                if mood is not None:
                    for a in pick_agents(state, "Apply to"):
                        a.mood = mood
                        print(c(f"  ✓ {a.name} mood = {mood or '(none)'}", "green"))

            elif low == "n":
                ans = ask("  Who speaks next? [a/b]: ").strip().upper()
                if ans in ("A", "B"):
                    state.next_key = ans

            elif low == "u":
                ev = state.undo()
                if ev:
                    print(c(f"  ↩ removed last {ev['kind']} event: {ev['text'][:80]}", "yellow"))
                else:
                    print("  nothing to undo")

            elif low == "k":
                for a in pick_agents(state, "Settings for"):
                    t = ask(f"  {a.name} temperature [{a.temperature}]: ").strip()
                    m = ask(f"  {a.name} max_tokens [{a.max_tokens}]: ").strip()
                    try:
                        if t:
                            a.temperature = float(t)
                        if m:
                            a.max_tokens = int(m)
                    except ValueError:
                        print("  (invalid number, kept old value)")
                d = ask(f"  delay between turns, seconds [{state.delay}]: ").strip()
                l = ask(f"  turn limit, 0 = unlimited [{state.max_turns}]: ").strip()
                s = ask(f"  streaming on/off [{'on' if state.stream else 'off'}]: ").strip().lower()
                try:
                    if d:
                        state.delay = float(d)
                    if l:
                        state.max_turns = int(l)
                except ValueError:
                    print("  (invalid number, kept old value)")
                if s in ("on", "off"):
                    state.stream = s == "on"

            elif low == "i":
                show_info(state)

            elif low == "v":
                for ev in state.events[-6:]:
                    show_event(state, ev)

            elif low == "w":
                state.save()
                print(f"  saved -> {state.base}.md / .json" if state.base else "  (transcripts disabled)")

            else:
                print("  unknown command")
    finally:
        watcher.pause = watcher.cut = False
        watcher.start()


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
def speak(state, watcher, speech):
    """One turn. Returns False if something failed (caller decides what to do)."""
    agent = state.agents[state.next_key]
    print(c(f"\n━━ Turn {state.turns + 1} · {agent.name} ({agent.cfg['host']}) ━━", agent.cfg["color"]))
    started = time.time()

    def on_token(piece):
        sys.stdout.write(piece)
        sys.stdout.flush()

    try:
        text, interrupted = call_llm(agent, agent.build_messages(state), watcher, on_token, state.stream)
    except KeyboardInterrupt:
        print(c("\n  ^C - paused (that unfinished message was discarded)", "yellow"))
        watcher.pause = True
        return True
    except Exception as e:
        print(c(f"\n  ✗ {agent.name} failed: {type(e).__name__}: {e}", "red"))
        return False

    print()
    text = clean(text)
    if not text:
        print(c("  (empty reply - not recorded)", "yellow"))
        return False
    if interrupted:
        text += " …"
        print(c("  ✂ cut off by you", "yellow"))
    state.add("say", who=agent.key, text=text)
    print(c(f"  [{time.time() - started:.1f}s]", "dim"))
    speech.say(text)
    return True


def handle_failure(state, watcher):
    watcher.stop()
    try:
        ans = ask("  [r]etry / [m]enu / [q]uit (default retry): ").strip().lower()
    finally:
        watcher.start()
    if ans == "q":
        raise Quit()
    if ans == "m":
        watcher.pause = True


def run_check(state):
    print("Checking both Macs...\n")
    ok = True
    for a in state.agents.values():
        msgs = [{"role": "system", "content": "You are a connectivity test."},
                {"role": "user", "content": "Reply with the single word: OK"}]
        t = time.time()
        try:
            text, _ = call_llm(a, msgs, KeyWatcher(), lambda p: None, False)
            print(c(f"✓ {a.key} {a.name:10} {a.url}  ->  {text.strip()[:40]!r}  ({time.time() - t:.1f}s)", "green"))
        except Exception as e:
            ok = False
            print(c(f"✗ {a.key} {a.name:10} {a.url}  ->  {type(e).__name__}: {e}", "red"))
    return ok


def main():
    global STREAM, USE_COLOR, VOICE_ENABLED, VOICE_RATE
    ap = argparse.ArgumentParser(description="Make two LLMs on two Macs talk, with you as moderator.")
    ap.add_argument("--topic", help="conversation topic (overrides TOPIC)")
    ap.add_argument("--turns", type=int, help="stop after N messages (0 = unlimited)")
    ap.add_argument("--first", choices=["A", "B", "a", "b"], help="who speaks first")
    ap.add_argument("--delay", type=float, help="seconds between messages")
    ap.add_argument("--no-stream", action="store_true", help="wait for whole replies instead of streaming")
    ap.add_argument("--no-voice", action="store_true", help="disable reading replies aloud")
    ap.add_argument("--voice-rate", type=int, metavar="WPM",
                    help=f"speech speed in words per minute (default: {VOICE_RATE})")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--check", action="store_true", help="test connectivity to both Macs and exit")
    ap.add_argument("--host-a"); ap.add_argument("--host-b")
    ap.add_argument("--port-a", type=int); ap.add_argument("--port-b", type=int)
    args = ap.parse_args()

    if args.no_color:
        USE_COLOR = False
    if args.host_a: MAC_A["host"] = args.host_a
    if args.host_b: MAC_B["host"] = args.host_b
    if args.port_a: MAC_A["port"] = args.port_a
    if args.port_b: MAC_B["port"] = args.port_b

    state = State(args.topic or TOPIC, (args.first or FIRST_SPEAKER).upper())
    if args.turns is not None: state.max_turns = args.turns
    if args.delay is not None: state.delay = args.delay
    if args.no_stream: state.stream = False
    if args.no_voice:
        VOICE_ENABLED = False
    if args.voice_rate is not None:
        if args.voice_rate < 60:
            ap.error("--voice-rate must be at least 60 words per minute")
        VOICE_RATE = args.voice_rate

    if args.check:
        sys.exit(0 if run_check(state) else 1)

    speech = Speech(VOICE_ENABLED, VOICE_RATE)
    first = state.agents[state.next_key]
    opener = (OPENER.replace("{topic}", state.topic).replace("{other}", state.other(first.key).name)
                    .replace("{name}", first.name))
    state.events.append({"kind": "private", "to": first.key, "text": opener,
                         "time": datetime.now().isoformat(timespec="seconds")})

    print(c("═" * 64, "bold"))
    print(c(" LLM DUEL", "bold") + f"   topic: {state.topic}")
    for a in state.agents.values():
        print(c(f"   {a.key}: {a.name:10} {a.url}  [{a.cfg['model']}]", a.cfg["color"]))
    print(c("   SPACE = pause & add your thoughts   x = cut message   q = quit", "dim"))
    print(c("═" * 64, "bold"))

    watcher = KeyWatcher()
    watcher.start()
    try:
        while True:
            watcher.poll()
            if watcher.quit:
                raise Quit()
            if watcher.pause:
                pause_menu(state, watcher)
                continue

            if state.max_turns and state.turns >= state.max_turns:
                if not sys.stdin.isatty():
                    break
                watcher.stop()
                try:
                    n = ask(f"\nReached the turn limit ({state.max_turns}). Extra turns? (number, 0/Enter = finish): ").strip()
                finally:
                    watcher.start()
                if n.isdigit() and int(n) > 0:
                    state.max_turns += int(n)
                    continue
                break

            if not speak(state, watcher, speech):
                if not sys.stdin.isatty():
                    break
                handle_failure(state, watcher)
                continue

            watcher.wait(state.delay)
    except Quit:
        pass
    finally:
        watcher.stop()
        state.save()
        print(c("\nConversation ended.", "bold"))
        if state.base:
            print(f"Transcript saved: {state.base}.md  and  {state.base}.json")


if __name__ == "__main__":
    main()
