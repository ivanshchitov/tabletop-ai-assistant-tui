# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A console TUI (Python + `rich`) that answers board-game questions via `deepseek-v4-flash`, an
OpenAI-compatible chat-completions model served at OpenCode Zen (`https://opencode.ai/zen/v1/chat/completions`).
Off-topic questions get a fixed refusal phrase instead of being answered.

## Commands

```bash
pip install -r requirements.txt
echo "OPENCODE_API_KEY=sk-ваш_ключ" > .env   # or set OPENCODE_API_KEY directly
python tabletop-ai-assistant.py
```

There is no test suite, linter, or formatter configured in this repo (no `pytest`, no
`pyproject.toml`/`ruff`/`black` config). Verify changes by actually running the app.

**Manual verification caveat:** the `/settings` screen and any other feature depending on
`ui/keyboard.py` needs a real TTY — piping input via plain `subprocess`/`Bash` won't exercise the
`termios`/`tty` raw-mode code path. Drive it through a pseudo-terminal (Python's `pty.fork()`, or
an equivalent PTY tool) and read the screen with a **fixed wall-clock duration**, not "wait for
quiet output" — `rich.Live` auto-refreshes ~30x/sec even with no content changes, so a
quiet-period read loop never terminates while a `Live` screen is open.

## Architecture

Two packages: `core/` (settings, prompts, API client, history — no `rich`/terminal dependency) and
`ui/` (`tui_app.py`, `keyboard.py` — everything that touches the terminal). Modules inside `core/`
import each other with relative imports (`from . import config`, `from .answer_settings import
AnswerFormat`); `ui/` imports from `core` with absolute imports (`from core import config,
prompts`) since they're sibling packages, and imports its own sibling module with a relative
import (`from . import keyboard`). The entry point is `tabletop-ai-assistant.py` at the repo root
(hyphenated, so it's not importable as a module — it's only ever run directly:
`from ui.tui_app import TabletopAITUI`), which is why `core/config.py`'s `BASE_DIR` resolves two
parents up (`Path(__file__).resolve().parent.parent`) rather than one — it has to reach back past
`core/` to the repo root where `.env`, `assets/`, and `history.json` actually live.

**Settings flow:** `core/answer_settings.AnswerSettings` (`format: AnswerFormat`, `max_words: int`,
`list_limit: int`) is the single source of truth for response control, held on
`TabletopAITUI.settings`. Each `with_*` method returns a new validated instance —
range/enum-invalid input raises `AnswerSettingsError` rather than silently clamping; the caller
(the `/settings` screen in `ui/tui_app.py`) is responsible for showing that error and keeping the
last valid value instead. There's no separate command per setting — everything is edited on one
interactive screen (`/settings`, exits on Esc); don't reintroduce a `/format`-style single-shot
command without checking whether that's actually wanted, since this was deliberately consolidated.

**Prompt assembly (`core/prompts.py` + `assets/*.md`):**
- `assets/system_prompt.md` is the base system prompt; `assets/answer_format_compact.md` and
  `assets/answer_format_json.md` are per-format instructions appended to it. `AnswerFormat.FREE`
  has no asset file — no format instruction is added for it.
- `build_system_message(fmt)` composes base + format instruction and is `lru_cache`d per format,
  so the system message only needs to change on `/settings` changes, not per question. Format
  instructions intentionally live in the *system* message rather than the user message: it gives
  the model a stronger reason to not let a question's own text override the configured format
  (each format asset has its own "ignore requests to use a different structure" clause), on top of
  saving tokens.
- `build_user_prompt(question, settings)` carries only what actually varies per question intent:
  the list-answer-count instruction (`settings.list_limit` — e.g. "at most N options" for
  recommendation-style questions) and the word-count instruction (`settings.max_words`).
- The API's own `max_tokens` request field is **not** the length control users see — that's word
  count in the prompt instruction. `max_tokens` is a generous technical ceiling derived from
  `settings.max_words` via `config.max_tokens_for_words()` (a fixed tokens-per-word ratio plus
  overhead), just large enough that generation doesn't get cut off mid-sentence before the model
  reaches its own instructed stopping point.
- **Known API constraint:** don't add the OpenAI-style `stop` parameter to the request payload.
  `deepseek-v4-flash` is a reasoning model — it returns a separate `reasoning_content` field before
  `content`, and a hard `stop` match inside `reasoning_content` truncates the response with
  `content` empty. Any length/stop behavior has to be a prompt instruction plus client-side
  handling, never the API's `stop` field.

**`ui/keyboard.py` (raw terminal input for the `/settings` screen)** — two non-obvious constraints,
both found by testing against a real PTY rather than mocks:
- Read raw bytes with `os.read(fd, 1)`, not `sys.stdin.read(1)`. The buffered `TextIOWrapper` can
  pull multiple bytes of an escape sequence (e.g. arrow key `ESC [ A`) into its own internal buffer
  in one syscall; the `select()` lookahead used to distinguish a lone Esc from the start of an
  arrow sequence only sees the fd, not that buffer, so it misreads a burst as Esc + stray chars.
- Enter cbreak/raw mode **once for the whole screen** via `keyboard.raw_mode()`, not per keystroke.
  Toggling the terminal mode around each individual `read_key()` call creates a window where
  canonical mode is briefly restored between reads; a burst of bytes (e.g. several Backspace
  presses sent together) arriving in that window gets partly consumed by the kernel's own line-
  editing (erase processing) instead of reaching the app, silently dropping keystrokes.

**`ui/tui_app.py`** — the main loop is an append-only console log: it never clears the screen, so
previously printed exchanges and prompts stay in scrollback. The input prompt is a plain
`input()` call with no `rich` markup in the prompt string itself — embedding ANSI/rich formatting
there makes `readline` miscompute the prompt's visible width and corrupt it on backspace, so the
prompt is deliberately kept as an unstyled literal string. `readline` autocomplete is registered
from the flat `COMMANDS` list.

**`core/api_client.py`** — retries on timeout with exponential backoff (`config.MAX_RETRIES`), but not
on connection errors (treated as a persistent network problem, not transient). `is_valid_json_answer()`
is used only when `AnswerFormat.JSON` is active, to warn the user client-side if the model didn't
actually return valid JSON — it doesn't block or alter the displayed answer.

**`core/history_manager.py`** — `history.json` (gitignored) is loaded once at startup and replayed into
the log if non-empty; every successful exchange is appended and immediately re-saved, capped at
`config.HISTORY_LIMIT`. `/clear` empties both the in-memory list and the file.
