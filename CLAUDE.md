# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A console TUI (Python + `rich`) that answers board-game questions via a model picked with the
`/models` command (default `deepseek-v4-flash`), an OpenAI-compatible chat-completions model
served at OpenCode Zen (`https://opencode.ai/zen/v1/chat/completions`).
Off-topic questions get a fixed refusal phrase instead of being answered.

## Commands

```bash
pip install -r requirements.txt
echo "OPENCODE_API_KEY=sk-ваш_ключ" > .env   # or set OPENCODE_API_KEY directly
python tabletop-ai-assistant.py

pip install -r requirements-dev.txt
pytest                      # everything except the `network` marker
pytest tests/unit -q        # fast layer, no subprocesses (~1s)
pytest tests/e2e -q         # real app in a pty against a stub API (~45s)
pytest --snapshot-update    # rewrite the e2e screen snapshots after a deliberate layout change

pytest tests/e2e/test_live_api.py -m network -q   # drive the app against the real OpenCode Zen

pytest -s --show-tui=live tests/e2e/...     # stream the app's own terminal output while it runs
pytest -s --show-tui=screen tests/e2e/...   # print the final rendered screen after each test
```

`--show-tui` mirrors what the app writes to its pty. `live` writes the raw byte stream to the
test runner's stdout, so `rich.Live` repaints, the spinner and the colours all show up exactly as
in a real session; `screen` prints only the final `pyte`-rendered scrollback, which is quieter and
safe to pipe. Both need `-s` — pytest captures output at the file-descriptor level otherwise, and
the fixture warns you when the flag would do nothing.

`pytest` is configured in `pyproject.toml`; there is no linter or formatter in this repo. The
default `addopts` deselects `-m network`, so nothing ever calls the real OpenCode Zen unless
asked.

**Testing the terminal is the hard part**, and both halves of the solution are load-bearing:
- Anything touching `ui/keyboard.py` needs a real TTY — piping input via plain `subprocess`
  won't exercise the `termios`/`tty` raw-mode path. `tests/unit/test_keyboard.py` drives a real
  `pty.openpty()` with `sys.stdin` swapped for the slave end. Don't compare `termios` attributes
  bit-for-bit: the kernel sets `PENDIN` itself on a mode switch, so mask it out (see
  `_stable_attrs`) or assert on `ICANON` instead.
- Never wait for "quiet output" from the app — `rich.Live` auto-refreshes ~30x/sec even when
  nothing changes, so a quiet-period read loop never terminates while a `Live` screen is open.
  Wait on the *rendered screen* instead (`AppSession.wait_for`), or read for a fixed duration
  (`read_for`) when checking that something is absent.
- Keyboard bytes sent before an interactive panel has drawn get consumed by that panel's own
  key loop, not the main `input()` — for the `/logictask` picker, `tests/e2e/test_strategies.py`
  waits for the panel hint ("Enter — решить") to appear on screen *before* sending arrows/Enter,
  and after Esc waits for the panel title to disappear (`wait_until_gone`) before touching the
  prompt again. `wait_for_prompt()` alone is a false positive there: the prompt is already in
  scrollback from before the panel opened. Same pattern in `tests/e2e/test_commands_flow.py`
  (hint "Enter — выполнить") and `tests/e2e/test_models_flow.py` (hint "Enter — применить").
  Additionally: a byte sent immediately after a raw-mode screen
  closes can vanish on the switch back to canonical mode (first byte eaten, e.g. `/exit`
  arriving as `exit`) — either wait for the command's *effect* before the next send, or sleep
  briefly after `wait_until_gone` on the panel.

## Change workflow (OpenSpec)

Anything that changes observable behaviour — a new command, a new setting, different prompt or
format semantics — goes through OpenSpec (`openspec/`, schema `spec-driven`) rather than straight
into the code; a typo or a one-line fix doesn't need a change. `/opsx:propose "…"` creates
`openspec/changes/<change-id>/` with the planning artifacts (`proposal.md`, delta specs under
`specs/<capability-path>/spec.md`, `design.md`, `tasks.md`), `/opsx:apply` implements them task by
task, and `/opsx:archive` folds the delta specs into the main specs in `openspec/specs/` once the
change is done. `/opsx:explore` is the thinking-partner step before proposing, `/opsx:update`
revises a change's artifacts, `/opsx:sync` pushes deltas into the main specs without archiving. In
OpenCode the same six commands are spelled with a dash (`/opsx-propose`, `/opsx-apply`, …).

- The propose step is **planning only** and must not edit project code, even when the request is
  worded as "build X" — implementation starts on a separate `/opsx:apply` request.
- The delta spec is a *diff* against the main spec, not a copy of it. The main specs under
  `openspec/specs/` are only ever written by archive/sync, never edited by hand during a change.
- `openspec/specs/` holds the master spec (archived from `add-master-spec`, reverse-engineered
  from the existing code/tests) as ten capabilities, each the target for future `MODIFIED` deltas:
  `question-answering`, `answer-settings`, `api-integration`, `history-persistence`,
  `terminal-ui`, `settings-screen`, `configuration`, `test-infrastructure`, `prompt-strategies`,
  `model-selection`.
  It records deliberate decisions worth knowing before touching related code: the JSON format's
  refusal reply is a machine-readable `{"error": ...}` object rather than the verbatim refusal
  phrase used by free/compact (not a bug to fix), and `AnswerSettings` is session-only by design —
  persisting it across restarts is backlog, not a current requirement.
- The skills shell out to a bare `openspec` binary (`allowed-tools: Bash(openspec:*)`), so the CLI
  has to be on PATH: `npm i -g @fission-ai/openspec`. `npx @fission-ai/openspec@latest <cmd>` works
  for manual invocations but not from inside the skills.
- `openspec/config.yaml` sets the artifact language to Russian (structural headings and SHALL/MUST
  stay English) to match `README.md`. That file, not the generated skills in `.claude/skills/` or
  `.opencode/skills/`, is where per-artifact rules and per-operation guidance belong — the skills
  are regenerated by `openspec init` and hand edits there are lost.
- Two agents are wired up, from the same generator and against the same `openspec/` tree:
  Claude Code (`.claude/`, commands `/opsx:<verb>`) and OpenCode (`.opencode/`, commands
  `/opsx-<verb>` — a dash, since OpenCode has no command namespaces). The skill bodies are
  byte-identical apart from the command names they cite. Re-run `openspec init --tools claude` and
  `openspec init --tools opencode` after a CLI upgrade so neither side goes stale; a run naming one
  tool leaves the other's directory untouched.
- `.claude/skills/feature-builder/` (mirrored at `.opencode/skills/feature-builder/`) is
  hand-written, not generated by `openspec init` — it survives the regeneration above. Triggers
  on "создай фичу" / "new feature" and chains the full loop for one feature end to end: OpenSpec
  propose (no code yet) → a Superpowers branch plus a `writing-plans` task checklist → per-task
  TDD (failing test, minimal code, `pytest`) → final `pytest` + `/opsx:archive` + merge to `main`.
  Runs caveman-style throughout — status lines only, no code narration.

## Architecture

Two packages: `core/` (settings, prompts, API client, history, logictask prompt builders — no
`rich`/terminal dependency) and `ui/` (`tui_app.py`, `keyboard.py`, `commands_screen.py`,
`settings_screen.py`, `logictask_screen.py`, `models_screen.py` — everything that touches the terminal). Modules inside `core/`
import each other with relative imports (`from . import config`, `from .answer_settings import
AnswerFormat`); `ui/` imports from `core` with absolute imports (`from core import config,
prompts`) since they're sibling packages, and imports its own sibling module with a relative
import (`from . import keyboard`). The entry point is `tabletop-ai-assistant.py` at the repo root
(hyphenated, so it's not importable as a module — it's only ever run directly:
`from ui.tui_app import TabletopAITUI`), which is why `core/config.py`'s `BASE_DIR` resolves two
parents up (`Path(__file__).resolve().parent.parent`) rather than one — it has to reach back past
`core/` to the repo root where `.env`, `assets/`, and `history.json` actually live.

**Environment switches (`core/config.py`):** `OPENCODE_API_URL`, `TABLETOP_HISTORY_FILE`,
`TABLETOP_REQUEST_TIMEOUT` and `TABLETOP_TYPING_DELAY` (the last one read in `ui/tui_app.py`)
override the corresponding defaults. They exist so the e2e layer can point the app at a local
stub server, keep history in a temp file, and collapse the typing animation. `HISTORY_FILE`
especially: its path derives from `__file__`, not the working directory, so without the override
*any* run — a test run included — would write to the single real `history.json` in the repo root.

**Settings flow:** `core/answer_settings.AnswerSettings` (`format: AnswerFormat`, `max_words: int`,
`list_limit: int`, `temperature: float`) is the single source of truth for response control, held on
`TabletopAITUI.settings`. Each `with_*` method returns a new validated instance —
range/enum-invalid input raises `AnswerSettingsError` rather than silently clamping; the caller
(the `/settings` screen in `ui/tui_app.py`) is responsible for showing that error and keeping the
last valid value instead. There's no separate command per setting — everything is edited on one
interactive screen (`/settings`, exits on Esc); don't reintroduce a `/format`-style single-shot
command without checking whether that's actually wanted, since this was deliberately consolidated.
The screen's behaviour lives in `ui/settings_screen.py` as a pure reducer (`initial_state` →
`apply_key` → `apply_to_settings`); `ui/tui_app.py` only runs the read-key/redraw loop around it.
Keep new key handling in the reducer — that's what makes it testable without a terminal.

**Temperature (`with_temperature` + the `/settings` temperature row):** range 0.0..2.0, default
`config.TEMPERATURE` (0.7). The "one decimal digit" rule is enforced on *input*, not validation:
the reducer only accepts a dot when there isn't one yet and only one digit after it, so values
like 0.55 are untypable and the user never sees a format error. The same rule lives in
`with_temperature` (`round(value, 1) != value` → `AnswerSettingsError`) as an invariant for
programmatic calls — don't replace it with rounding (silent normalization contradicts the
no-clamping principle). The setting is passed to `client.ask(..., temperature=...)` on every
question; `/logictask` deliberately doesn't pass it and stays on the client default.

**`/logictask` (`core/logictask.py` + `ui/logictask_screen.py`):** solves one fixed tabletop-themed
logic puzzle (wolf/goat/cabbage river crossing) with a prompting strategy picked on an interactive
panel (↑/↓ move, Enter runs, Esc cancels with zero API calls). Deliberate decisions baked in:
- Exactly four strategies, and "run all in sequence" was explicitly rejected — one invocation
  solves one strategy (1 request direct/stepwise, 2 for the model-composed-prompt strategy — the
  composed prompt is fed back verbatim as the *system* message of the second request — and 3 for
  the expert panel).
- The three experts (chef, animal psychologist, game theorist) are client-side constants, never
  requested from the model; each gets a separate request with its role as the system message.
- Strategy prompts are independent of `AnswerSettings`: no format/word-count/list-limit
  instructions, and `max_tokens` is pinned to the default (`config.max_tokens_for_words(config.DEFAULT_MAX_WORDS)`)
  regardless of the session settings; `ask()` is called without `temperature` (client default)
  but carries the session model (`model=self.model`).
- Results never touch `history.json` and don't increment the session dialogue counter.
- An API error mid-run aborts the rest of the run but not the session.
- The panel follows the `/settings` pattern: a pure reducer in `ui/logictask_screen.py`
  (`initial_state` → `apply_key`), with `ui/tui_app.py` only wrapping raw-mode + `Live` and the
  read-key/redraw loop. The task text and the four options are printed to the permanent log before
  the transient `Live` panel opens — a non-terminal `Live` (as in unit tests) renders nothing, so
  anything asserted on in unit tests must go through the permanent prints.

**`/commands` (`ui/commands_screen.py`):** an interactive panel listing every command with a short
description (↑/↓ move, Enter runs the selected command, Esc cancels with zero API calls).
Deliberate decisions baked in:
- Enter **executes** the selected command directly through the same dispatcher `_handle_command`
  that serves manual input — it deliberately does NOT prefill the readline input buffer. The
  insert-into-buffer approach was tried first and failed on libedit (`/usr/bin/python3` on macOS):
  libedit silently ignores both `rl_startup_hook` and `insert_text`, so nothing happened for the
  user. Direct execution behaves identically on GNU readline, libedit, and no-readline builds —
  don't reintroduce readline-based prefill for interactive flows.
- `COMMANDS` (autocomplete + dispatcher) is derived from `ui/commands_screen.COMMAND_OPTIONS`
  (command + description pairs), so the panel and Tab-completion can't drift apart; add new
  commands there, not in `tui_app`.
- The status-bar hint uses a separate `STATUS_COMMANDS = ["/exit", "/commands"]` — the full list
  intentionally lives only in the panel.
- Selecting `/commands` inside the panel reopens it (a `while` loop in `_open_commands_screen`,
  not recursion); `/exit` selection calls `_exit()` and sets `self._exit_requested`, which the
  main loop checks after `_handle_command` returns — that's how the loop stops without an
  exception.

**`/models` (`ui/models_screen.py` + `core/config.py`):** model selection, one decision per
session. Deliberate decisions baked in:
- `config.AVAILABLE_MODELS` is the fixed list (`deepseek-v4-flash`, `deepseek-v4-pro`,
  `kimi-k2.5`, `glm-5.1`, `mimo-v2.5-free`, `kimi-k2.6`, `kimi-k3`) and `DEFAULT_MODEL` is its
  first element; the old `MODEL_NAME` constant no longer exists. The panel and the client only
  read the list from here. `kimi-k2.6` and `kimi-k3` were added specifically to give a clear
  weak/medium/strong price tier alongside `deepseek-v4-flash` for the day-5 challenge comparison
  (time/tokens/cost across model strength) — not required by any other feature.
- `config.MODEL_PRICING` maps every model in `AVAILABLE_MODELS` to a `(input_price, output_price)`
  pair in USD per 1M tokens, used by `core.usage.estimate_cost()` to price a request. Where
  OpenCode Zen has peak/off-peak pricing (DeepSeek V4 Flash/Pro), the table stores the lower
  off-peak number — comparative, not accounting-grade precision (see
  `openspec/changes/archive/2026-09-04-add-model-usage-metadata/design.md` for the full rationale).
- The selected model lives on `TabletopAITUI.model` — session-only, like `AnswerSettings` (no
  persistence between restarts is deliberate, same backlog logic). Every request passes it as
  `client.ask_with_usage(..., model=...)`; `/logictask` follows the selection too, so one session =
  one model.
- The panel follows the same reducer pattern (`initial_state(current_model)` puts the cursor on
  the active model); the active model is additionally marked "(текущая)" in the render, separate
  from the cursor. Enter applies, Esc cancels with zero API calls.
- No client-side model validation: the list is fixed so free-form input is impossible, and an
  unknown model surfaces as a regular API error through the existing `APIError` path.
- The status bar shows `Модель: <имя>` right after "Готов" — several e2e snapshots assert on it.
- After every answer — a normal question via `_handle_question` and each individual API call
  inside `/logictask` (twice for strategy 3, three times for strategy 4, once per expert) —
  `TabletopAITUI._print_usage_meta()` prints a screen-only line with response time, token counts
  and cost (`"неизвестно"` when the model has no entry in `MODEL_PRICING`). This line is never
  written to `history.json` and never replayed on restart — it exists only at the moment of the
  answer.

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
- **`config.MAX_MAX_WORDS` is 1000, not 500.** Several reasoning models in the pool (`kimi-k2.5`,
  `kimi-k2.6`, `glm-5.1`, `deepseek-v4-pro`) put their thinking into `reasoning_content`, same as
  `deepseek-v4-flash` above — and on a demanding comparison-style question, that reasoning can eat
  the *entire* `max_tokens` budget before the model ever starts writing `content`, coming back with
  `finish_reason: "length"` and an empty answer even though the request itself succeeded (no
  `APIError`, real `usage` numbers). Verified directly against the API: at the old ceiling (500
  words → `max_tokens` 2050) `kimi-k2.5`/`glm-5.1`/`deepseek-v4-pro` all returned empty content on
  a "compare X and Y, justify in detail" prompt; `kimi-k2.6` needed roughly 4000 tokens (~988
  words) to produce a real answer for the same prompt. Raising the ceiling to 1000 words (`max_tokens`
  4050) gives reasoning models enough headroom without changing the default (200 words) or floor
  (10 words). This is a client-side mitigation, not a fix — the underlying model behavior (spending
  unbounded reasoning tokens on some prompts) is unchanged, so an even harder question can still
  return empty for these models; there is no client-side signal to detect or retry on that case.
- **`config.REQUEST_TIMEOUT` is 90s, not 30s**, for the same reason as the word ceiling above:
  generating thousands of reasoning tokens takes real wall-clock time. Measured directly against
  the API, `kimi-k2.6` generated at roughly 57 tokens/sec on a demanding prompt (26.3s for 1500
  tokens), so the ~4050-token budget from the raised `MAX_MAX_WORDS` needs on the order of 70s —
  well past the old 30s default, which cut the request off with a timeout *before* the model could
  finish, even though it wasn't stuck. Retrying doesn't help here: every attempt gets the same
  `REQUEST_TIMEOUT`, so a response that genuinely needs 70s times out on all `MAX_RETRIES` attempts
  identically. Trade-off: a request that truly hangs (dead server, network partition) now takes
  proportionally longer to surface as an error to the user (up to ~273s across 3 attempts instead
  of ~93s) — accepted deliberately so slow-but-working reasoning responses aren't misreported as
  failures.

**`ui/keyboard.py` (raw terminal input for the interactive panels: `/commands`, `/settings`, `/logictask`, `/models`)** — two non-obvious constraints,
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
from the flat `COMMANDS` list. Exception: the manual API-key prompt inside `_ensure_api_key` is
read via `sys.stdin.readline` (`_read_manual_key`), NOT `input()` — GNU readline catches SIGINT
while `input()` is active and re-displays the prompt instead of raising `KeyboardInterrupt`, so
Ctrl+C during the key prompt never reached `run()`'s handler (flaky in CI, invisible locally
because the timing usually lets the interrupt fire before readline is active).

**`core/api_client.py`** — `ask()` first checks the key with `is_valid_api_key()` (non-empty and
ASCII) and raises `APIError` before making any request. The model is a parameter
(`ask(..., model=...)`, default `config.DEFAULT_MODEL`) — the client has no model names baked in.
HTTP headers are latin-1 encoded,
so a key typed in a Cyrillic keyboard layout used to blow up with `UnicodeEncodeError` from deep
inside `requests` — a traceback instead of a message. `ui/tui_app.py`'s `_ensure_api_key()` runs the
same check, including on the key that came from `.env`, so the problem surfaces at startup rather
than after the first question. Retries on timeout with exponential backoff (`config.MAX_RETRIES`), but not
on connection errors (treated as a persistent network problem, not transient). `is_valid_json_answer()`
is used only when `AnswerFormat.JSON` is active, to warn the user client-side if the model didn't
actually return valid JSON — it doesn't block or alter the displayed answer.
`ask()` and the newer `ask_with_usage()` share one private `_request()` (HTTP call, retries, error
handling, unchanged from before this split); `ask()` returns just the stripped answer text as
always, while `ask_with_usage()` additionally returns an `AnswerMeta` dataclass (content, model,
`elapsed_seconds` measured around the whole `_request()` call including retries, `prompt_tokens`/
`completion_tokens`/`total_tokens` from the API's `usage` field, and `cost_usd` from
`core.usage.estimate_cost()` — `None`, not an error, when the model has no entry in
`config.MODEL_PRICING`). `ui/tui_app.py` calls `ask_with_usage()` everywhere now (both the normal
question path and every `/logictask` strategy call); `ask()` stays only for callers that just want
the text and don't need metrics — currently none inside this app, kept because changing its
signature would have broken every existing caller/test for no benefit.

**`core/usage.py`** — `estimate_cost(model, prompt_tokens, completion_tokens)` is a pure function
over `config.MODEL_PRICING`; it exists as its own module (not inlined in `api_client.py`) so cost
math is testable without HTTP mocking.

**`core/history_manager.py`** — `history.json` (gitignored) is loaded once at startup and replayed into
the log if non-empty; every successful exchange is appended and immediately re-saved, capped at
`config.HISTORY_LIMIT`. `/clear` empties both the in-memory list and the file.

## Test layout

- `tests/unit/` — no subprocesses, ~1s for the whole layer. `core/` logic, the `/commands`,
  `/settings`, `/logictask` and `/models` reducers, and `TabletopAITUI` driven through injected dependencies:
  `TabletopAITUI(console=, history=, client=)` takes a `rich` console writing to a buffer, a
  `HistoryManager` on `tmp_path`, and a fake client that records what was asked. Passing a client
  also skips the API-key prompt at startup.
- `tests/unit/test_keyboard.py` — the only unit file that needs a pty (see above).
- `tests/e2e/` — the real `tabletop-ai-assistant.py` running in `pty.fork()`, with output fed
  through `pyte` so assertions read the *rendered* screen rather than a stream of cursor codes.
  `harness.AppSession` is the driver (`wait_for` searches the scrollback, `wait_on_screen` and
  `wait_until_gone` search only the current screen — needed when the text also appears earlier in
  the log, e.g. the input prompt around the `/settings` panel). `stub_api.StubAPI` is a threaded
  local server that both answers and **records every request**, which is where most of the value
  is: the tests assert on what actually went to the API (system message per format, the word and
  list limits in the user prompt, `max_tokens`, the session temperature, the absence of `stop`,
  the retry count).
  `_write_all` re-writes what a single `os.write` couldn't fit into the terminal buffer —
  without it a >2000-character question loses its tail along with the trailing Enter.
- `tests/e2e/snapshots/` — whole-screen snapshots. The stub server's port is normalized away
  (`_VOLATILE_PATTERNS` in `harness.py`) because it changes every run and appears inside error text.
- `tests/e2e/test_live_api.py` — the same app driven against the **real** OpenCode Zen
  (`live_app` fixture: no stub, no `OPENCODE_API_URL`, key read from `.env` by the app itself;
  skips when there is no key). It is marked `network`, so it never runs by default. These tests
  check the prompt, not the plumbing — the verbatim refusal phrase, the JSON and compact formats
  actually being produced, a question failing to override the configured format, and the word and
  list limits being honoured. Assertions are deliberately loose (field presence, generous word
  ceilings) because model output is non-deterministic; assert on the requirement, never on an
  exact wording. Note that assertions run against the *rendered* screen, so markdown is already
  gone — rich draws a ```json block as a bordered code block with no backticks left in the text.
- `tests/e2e/cassettes/` — real `deepseek-v4-flash` answers, recorded once with
  `pytest tests/e2e/test_recorded_answers.py -m network --record-cassettes` (needs a real key,
  spends quota) and replayed by the stub afterwards. Tests skip themselves when a cassette is
  missing, so the repo works without a key.
