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
pytest                      # everything except the `network` marker (645 selected)
pytest tests/unit -q        # fast layer, no subprocesses (~3s)
pytest tests/e2e -q         # real app in a pty against a stub API (~95s)
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
  key loop, not the main `input()` — `tests/e2e/test_commands_flow.py` waits for the panel hint
  ("Enter — выполнить") to appear on screen *before* sending arrows/Enter, and after Esc waits for
  the panel title to disappear (`wait_until_gone`) before touching the prompt again.
  `wait_for_prompt()` alone is a false positive there: the prompt is already in scrollback from
  before the panel opened. Same pattern in `tests/e2e/test_models_flow.py` (hint "Enter —
  применить") and `tests/e2e/test_context_strategies.py` (settings marker "↑/↓ — поле").
  Arrows also need a real pause between them (`KEY_PAUSE` in that file): at a few milliseconds the
  key reader can take the head of the next escape sequence for a lone Esc.
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

```bash
openspec validate <change-id> --strict    # before showing the plan, and again before archiving
openspec archive <change-id> --yes        # non-interactive: without --yes the CLI blocks on a prompt
```

- The propose step is **planning only** and must not edit project code, even when the request is
  worded as "build X" — implementation starts on a separate `/opsx:apply` request.
- The delta spec is a *diff* against the main spec, not a copy of it. The main specs under
  `openspec/specs/` are only ever written by archive/sync, never edited by hand during a change.
- `openspec/specs/` holds the master spec (archived from `add-master-spec`, reverse-engineered
  from the existing code/tests) as a set of capabilities, each the target for future `MODIFIED`
  deltas: `agent`, `question-answering`, `answer-settings`, `api-integration`,
  `history-persistence`, `terminal-ui`, `settings-screen`, `configuration`, `context-strategies`,
  `memory-model`, `user-profile`, `test-infrastructure`, `model-selection`.
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
  on "создай фичу" / "new feature" and chains the full loop for one feature end to end:
  `/opsx:propose` (no code yet; `tasks.md` inside `openspec/changes/<change-id>/` is the only
  plan — no `writing-plans` duplicate) → `validate --strict` before the plan is shown → a plain
  branch named after the change (`git switch -c`, never a worktree: the e2e layer runs the app
  from the repo root) → `/opsx:apply` with per-task TDD (failing test, minimal code, `pytest` on
  the touched layer's file only) → one full `pytest` run → `archive --yes` (specs only, so nothing
  to re-run afterwards) → a `CLAUDE.md`/`README.md` pass that *re-checks* existing lists instead of
  only appending → fast-forward merge to `main`. Every git mutation waits for an explicit "yes"
  after `git status`/`git diff --stat`, and commit wording follows the repo's own style first.
  Runs caveman-style throughout — status lines only, no code narration.
- `.claude/skills/solve-challenge-task/` (mirrored the same way) wraps `feature-builder` for an
  AI Advent Challenge day: task parsing + demo scenario + tag, then the feature via
  `feature-builder`, then a screen recording via `scripts/record_screen.sh` (`doctor` on macOS
  records a 2-second probe to catch a missing Screen Recording permission) and the submission
  drafts under `videos/` (gitignored) — the challenge README fragment and the GitHub release body
  (commit hashes only, the diff as a link, every number re-read from a command). The demo itself is
  driven by a throwaway script that waits on *rendered* markers (`⏱`, report headings, panel hints)
  rather than on its own echo, and the recorded frames are read back with vision — a size check
  catches a black frame, only reading catches a wrong or misspelled screen. The challenge
  repository is read-only for the agent.

**Agent-loop gotchas** (found on day 11; both hand-written skills above rely on this list rather
than repeating it):
- `openspec archive <change-id>` blocks on a confirmation prompt and fails without a TTY — always
  pass `--yes`. Run `openspec validate <change-id> --strict` *before* showing the plan and before
  archiving: a `MODIFIED` block that drops a requirement's existing scenarios, and a `## Purpose`
  inside a delta for an existing capability, are both rejected there — cheaper than a second round.
- Archiving rewrites `openspec/specs/` only, so the full `pytest` run belongs *before* it; after it
  `openspec validate --all --strict` is the whole check.
- A new on-disk state file means a new `TABLETOP_*` environment switch **and** a pass-through in
  `tests/e2e/harness.AppSession`: without both, any run — a test run included — reads and writes the
  real file in the repo root (`history.json`, `memory.json`).
- Screen recording captures the user's whole screen, so the demo terminal window must be one the
  script creates itself and addresses by window id — never by index; other windows must not be
  closed, focused or moved.
- While iterating, run the test file of the layer being touched; the full suite is a single run at
  the end of the loop.
- Commit-message style is the repo's own (`git log --format=%s -10`), not Conventional Commits:
  the skills offer the repo-style wording first.

## Architecture

Two packages: `core/` (agent, settings, prompts, API client, memory layers and stores, the user
profile, the task state machine and its pipeline, context strategies and the compression logic — no
`rich`/terminal dependency) and `ui/`
(`tui_app.py`, `keyboard.py`, `commands_screen.py`, `settings_screen.py`, `branches_screen.py`,
`models_screen.py` — everything that touches the terminal). Modules inside `core/`
import each other with relative imports (`from . import config`, `from .answer_settings import
AnswerFormat`); `ui/` imports from `core` with absolute imports (`from core import config,
prompts`) since they're sibling packages, and imports its own sibling module with a relative
import (`from . import keyboard`). The entry point is `tabletop-ai-assistant.py` at the repo root
(hyphenated, so it's not importable as a module — it's only ever run directly:
`from ui.tui_app import TabletopAITUI`), which is why `core/config.py`'s `BASE_DIR` resolves two
parents up (`Path(__file__).resolve().parent.parent`) rather than one — it has to reach back past
`core/` to the repo root where `.env`, `assets/`, `history.json`, `memory.json` and
`profile.json` actually live.

**Environment switches (`core/config.py`):** `OPENCODE_API_URL`, `TABLETOP_HISTORY_FILE`,
`TABLETOP_MEMORY_FILE`, `TABLETOP_PROFILE_FILE`, `TABLETOP_TASK_FILE`, `TABLETOP_REQUEST_TIMEOUT`,
`TABLETOP_TYPING_DELAY` (the last one read in `ui/tui_app.py`), `TABLETOP_COMPRESS_AFTER` (the
session setting's default, messages, default 10) and `TABLETOP_MAX_SESSION_TOKENS` (the session
setting's default, tokens, default 20000) override the corresponding defaults. They exist so the
e2e layer can point the app at a local stub server, keep history, long-term memory and the user
profile in temp files, collapse the typing animation — or exercise compression and the token
ceiling in seconds. `HISTORY_FILE`/`MEMORY_FILE`/`PROFILE_FILE`/`TASK_FILE` especially: their paths derive
from `__file__`, not the working directory, so without the override *any* run — a test run
included — would write to the single real `history.json`/`memory.json`/`profile.json`/`task.json`
in the repo root. A new on-disk state file means a new env switch **and** a pass-through in
`tests/e2e/harness.AppSession` — see the agent-loop gotchas above.

**Settings flow:** `core/answer_settings.AnswerSettings` (`format: AnswerFormat`,
`context_strategy: ContextStrategy`, `max_words: int`, `list_limit: int`, `temperature: float`,
`compress_after: int` (5..50, default 10 — messages kept verbatim before compression fires, and the
window size for the window strategy), `max_session_tokens: int` (5000..50000, default 20000 — the
ceiling of the assembled request in tokens)) is the single source of truth for response control, held on
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
question; the strategy's auxiliary requests (facts extractor, summarizer) deliberately don't pass
it and stay on the client default.

**Context strategies (`core/context_strategies.py`, `core/context_compressor.py`,
`AnswerSettings.context_strategy`):** the session picks one of four ways of deciding what the model
sees; the strategy is a *view* on the session log, never its mutator. Deliberate decisions baked in:
- `AnswerSettings.context_strategy` (`ContextStrategy` enum: `summary`, `sliding_window`,
  `sticky_facts`, `branching`; default `summary`) is edited on the `/settings` strategy row with
  ←/→, exactly like the format row, and is session-only. `config.CONTEXT_STRATEGIES` fixes the order
  and the default; a test asserts the enum values and the list agree, so adding a strategy is one
  entry in each place.
- **The session log is append-only.** `TabletopAgent._turns` only grows; the strategy selects which
  turns go into the request. Compression therefore no longer deletes turns — the summary merely
  covers a prefix of the log (`_log_covered`), while `_summary_covers` keeps counting *file* records
  so restore and the history envelope stay consistent. Both counters are needed precisely because
  after a restart the log holds only the uncovered tail of the file. Switching a strategy is
  lossless; don't reintroduce `del self._turns[...]`.
- `summary` (the default, day-9 behaviour): the summarizer folds the oldest turns into a digest when
  the uncovered tail reaches `compress_after` messages or the estimate exceeds
  `max_session_tokens`, always keeping the last exchange verbatim. Its prompt/asset live in
  `core/context_compressor.py` and `assets/summary_prompt.md`.
- `sliding_window`: the request carries system + the last N messages (`context_strategies.window_messages`,
  whole exchanges, last exchange always) and makes no extra API call at all.
- `sticky_facts`: an extractor call precedes every question — it receives the current block plus the
  user messages not yet folded and must answer with a JSON object of key–value pairs.
  The pending queue is seeded from the session log (`_facts_covered` counts log exchanges already
  handed to the extractor), so switching to facts mid-dialog picks up what was said under other
  strategies instead of starting blind; messages restored from `history.json` count as already
  processed, since the restored block came from them. A queue that does not fit one request is sent
  in budget-sized batches, advancing only on success
  (`assets/facts_prompt.md`, parsed client-side by `context_strategies.parse_facts_response`; no
  `response_format`, for the same reasoning-model reliability reasons as the summarizer).
  `merge_facts` replaces a known key's value and evicts the earliest keys past `MAX_FACTS_KEYS`.
  A failed extractor (API error, empty or non-JSON answer) never blocks the answer (user's call):
  the question goes out with the previous block, the message stays in `_facts_pending` for the next
  update, and the journal prints "Факты не обновлены". `{}` is a *successful* empty update, not a
  failure — that distinction is what clears the pending queue; an empty body is a failure, because
  treating it as an empty block would drop the user's message from the memory for good.
- **An empty digest is a failure, not a summary.** `_digest_before_request` raises `APIError` when
  the summarizer returns empty or whitespace-only content (the reasoning-model `finish_reason=length`
  case, caught in a live run): the spend is recorded, but the summary and both coverage counters stay
  put. Without that guard the counter advanced while nothing replaced the covered turns — the request
  silently lost context, and `/context` reported "резюме нет" next to a non-zero coverage. Same rule
  as the summarizer's error path: nothing is lost, the next question retries the compression.
- `branching`: `context_strategies.BranchTree` keeps branches as index lists into the same log, so
  branches share messages instead of copying them. A checkpoint marks a position in the active
  branch; a new branch starts as a copy of the active branch's turns up to it. Branches are
  session-only: `history.json` gets every exchange flat, regardless of branch.
- The ceiling (`max_session_tokens`) is an invariant for every strategy: `_shrink_to_ceiling` drops
  the oldest turns of the view (window/branch shrink, summary digests early) until the estimate fits
  or only the last exchange is left — then the request goes out as is. The facts block is never
  trimmed.
- Auxiliary requests carry the session model, no temperature and a pinned `max_tokens`
  (`config.FACTS_MAX_WORDS` / `config.SUMMARY_MAX_WORDS`); their spend lands in the session ledger
  and `RequestPhase.FACTS_UPDATE` / `COMPRESSION` drive the spinner labels.
- `TabletopAgent.context_report()` returns a `ContextReport` (strategy, window, ceiling, log
  exchanges, turns in the next request, summary coverage, facts, branches, token estimate) — the
  terminal layer renders reports from it and must not read agent internals. This is the isolation
  boundary: keep new context data inside the snapshot rather than exposing new properties.
- The `/branches` panel (`ui/branches_screen.py`) is the same reducer pattern, with two extra
  outcome keys: `c` (checkpoint) and `n` (branch from checkpoint) — they are actions on the dialog,
  not list rows, so they are not expressed as entries in the list.

**Memory model (`core/memory_layers.py`, `core/long_term_memory.py`, `/memory`):** the agent's
memory is three explicit layers with different content, lifetime and storage — short-term (the
dialogue's own turns), working (the current task's data), long-term (what is known about the user
between sessions). Deliberate decisions baked in:
- Long-term memory is **one** type of record, not several sub-layers: one entry = key + value +
  category label (`профиль`/`решения`/`знания`/`заметка`), one file, one lifetime, one removal
  path. Only the short-term layer has a layer-level identity distinct from the record's category.
- Storage split is the point: short-term turns live in the session log and in the history
  envelope's `dialogues`; working memory is the envelope's `working` block (`{key: value}`, keys
  are the categories `цель`/`ограничения`); long-term memory is its own `memory.json`
  (`TABLETOP_MEMORY_FILE`, gitignored) holding `{"entries": {key: {"value", "category"}}}`. An
  entry without a stored category reads as a note — a hand-edited file must not lose data.
- **Routing is a deterministic rule table, never a model call.** `memory_layers.RULES` maps
  regex patterns over the user's message to (layer, category, key); the value is the sentence
  containing the match, clipped to `config.MEMORY_VALUE_MAX_CHARS`. Repeat matches replace the
  value of the same key. Regex is case-insensitive, so Cyrillic works in any case. Deliberate:
  a second LLM extractor per message would cost a request per turn, be non-deterministic and
  untestable; the rules are printed by `/memory` so the user can see why something was stored.
  Keep patterns **narrow** (the `без …` pattern lists game topics on purpose — a bare
  `без \w+` swallowed ordinary questions like "вопрос без ответа") and add a unit test per rule.
- The **model's answer is never a source of memory records** — only the user's messages are.
  Routing runs before the request is assembled, so a record made by the current message is
  visible to the model in that same request. It also runs when the request then fails: the words
  were the user's; the answer's absence changes nothing (`test_failed_exchange_is_not_saved`
  in e2e asserts on `dialogues`, not on the whole envelope for exactly this reason).
- Layers reach the model as **one extra system message** (`memory_layers.memory_message`) placed
  above the dialogue turns and above the strategy's memory (facts/summary): long-term before
  working, the fresh user turn always last. `assets/memory_prompt.md` carries the instruction
  (fresh message overrides memory, don't invent memory, don't recite the layers) and is appended
  only when a layer is non-empty — same reasoning as the format instructions living in the system
  message, but conditional, since an empty agent shouldn't pay for it. Empty layers add nothing,
  so an empty-memory request keeps its old shape (several e2e tests depend on that).
- `_shrink_to_ceiling` trims dialogue turns only: memory messages are never cut by the token
  ceiling or the estimate, like the facts block.
- **`/clear` empties the short-term and working layers and never the long-term one** — a
  deliberate asymmetry (`reset()` says so in its docstring, the confirmation says so on screen):
  `/clear` answers "start the dialogue over", while what the agent knows about the person is not
  the dialogue. `/memory forget all` is the only way to drop it. The working layer also survives a
  restart (it is in the envelope) while the short-term one keeps only the uncovered tail.
- `TabletopAgent.memory_report()` returns a `MemoryReport` snapshot (exchange count, both layers'
  records, store paths, rule descriptions) — the terminal renders it and never reads `_working`
  or the stores directly. Same isolation boundary as `context_report()`.
- `/memory` and `/profile` are plain commands with **meaningful arguments** (`goal`/`remember`/
  `forget`, `setup`/`use`/`forget`), unlike every other command whose arguments are ignored. It was deliberately not built as another panel
  reducer: the operations take free text, which would have meant yet another input mode in
  `keyboard.py`. `remember` uses the rule's category and key when a pattern matches and otherwise
  files the text as a note under its own sequential key, so notes don't evict each other.
- Journal lines (routing decision after an answer, operation results after `/memory`) are
  screen-only, never written to `history.json` — the same rule as the compression and facts lines.

**User profile (`core/user_profile.py`, `/profile`):** personalization — declared preferences that
shape every answer, deliberately kept apart from the memory layers. Deliberate decisions baked in:
- **A profile is not a memory record.** Memory entries are *derived* by the deterministic rule table
  from what the user says; the profile is *declared* by the user in a setup dialogue and describes
  how to answer, not what is known. It lives in its own file `profile.json`
  (`TABLETOP_PROFILE_FILE`, gitignored) holding several profiles plus an `active` name; `/clear`
  never touches it (same asymmetry as long-term memory), and only `/profile forget <name>` drops one.
- Sections are fixed: the profile name plus «Стиль», «Ограничения», «Опыт», «Жанры и механики».
  The question script is `QUESTIONS` (at most five questions: the name and one per section), and
  `InterviewState` is a pure automaton — `question` → `answer(text)` → `profile(base, default_name)`
  — while `ui/tui_app.py` only prints the question and reads the line. That is what makes the
  dialogue testable without a terminal, the same reason key handling lives in the panel reducers.
  A test holds the "no more than five questions" invariant and the section coverage.
- Answers are read with `sys.stdin.readline`, **not** `input()`: readline swallows SIGINT, so with
  `input()` Ctrl+C would not raise `KeyboardInterrupt` and the setup could not be cancelled (same
  reason as the manual API-key prompt). The dialogue catches `KeyboardInterrupt`/`EOFError` around
  itself and cancels **only** the setup, writing nothing; everywhere else Ctrl+C still exits the app.
- An empty answer leaves the section as it was (for a new profile — empty), so a repeated setup is
  an edit and a stray Enter destroys nothing; an empty name falls back to a free `профиль N` (the
  same trick as `/memory`'s sequential notes). Answers are clipped to
  `config.PROFILE_VALUE_MAX_CHARS`, and the journal line prints the stored value, not the typed one.
- **Storage is JSON, not the markdown files the week-3 README suggests**: repo convention is state
  in JSON with tolerant reading (missing/corrupt file = no profiles, write errors never break the
  session), while `assets/*.md` is where model instructions live.
- Request composition: system → **profile message** → memory layers → strategy memory → dialogue
  turns → fresh user turn. The profile answers "how to answer", so it sits right after the system
  prompt and above memory. `assets/profile_prompt.md` subordinates preferences to the app settings
  (format, word limit, list limit) and to the fresh message — otherwise a profile could break the
  JSON/compact format contracts and the word ceilings the e2e tests assert.
- An empty profile adds no message, so the request shape without personalization is unchanged
  (several tests depend on that). `use` never creates a profile — only the setup dialogue does, so
  a typo can't push an empty profile into a request; deleting the active profile leaves none active.
- The status bar shows the active profile's name right after the model, but **only when the profile
  is non-empty**: an empty profile never reaches the request, so a line about it would advertise
  personalization that isn't there — and the layout without a profile stays exactly as before
  (several screen snapshots depend on it). Profile names and section values are user text, so every
  line that prints them goes through `rich.markup.escape` — a value like `[/dim]` otherwise raises
  `MarkupError` inside `console.print`. The same escaping was applied to the `/memory remember` line,
  which had the same latent crash.
- `TabletopAgent.profile_report()` returns a `ProfileReport` snapshot (active name, sections as
  label/value pairs, all profile names, store path) — the terminal renders it and never reads
  `profile.json` itself; the same isolation boundary as `context_report()`/`memory_report()`.
- The dialogue is `/profile setup`; `/profile` prints the report, `use <name>` switches the active
  profile. Like `/memory`, the command has **meaningful arguments**, and it was deliberately kept
  out of the panel-reducer pattern because a question-answer dialogue is not a form.

**Task state machine (`core/task_state.py`, `core/task_pipeline.py`, `/task`):** a user goal enters
as a *task* in a queue and is carried through four stages — `Planning` → `Execution` → `Validation`
→ `Done` — by the agent itself, without the user steering the transitions. Deliberate decisions
baked in:
- **The stage machine is code, not a model contract.** `task_state` holds the fields the day-13 task
  names (`этап`, `текущий шаг`, `ожидаемое действие`) and the transition table: Planning ends by
  asking the user for edits (non-empty answer → another Planning round, empty → Execution), Execution
  runs the plan's sub-tasks one by one, Validation checks the artifact, Done reports a summary and
  moves to the next queued task. `текущий шаг`/`ожидаемое действие` are *derived* properties, never
  stored, so they cannot drift away from the stage. The model supplies content only — the plan, the
  artifact sections, the review verdict — and never chooses a transition; that is why "the agent
  jumped a stage" is structurally impossible here.
- **One operation per call.** `agent.task_step()` performs exactly one stage operation (a plan
  request, one sub-task, the validation, the summary) and writes `task.json` *before* the next
  request; the terminal layer only loops, redraws the panel and polls the pause key. `TaskPipeline`
  never touches `rich` or HTTP: it gets `ask(messages, max_words, phase)` injected, which is what
  makes every branch testable with a fake request.
- **The pipeline's requests carry their own length limits.** Every pipeline request names what it
  wants (`Число подзадач`, `Формулировка подзадачи — до N символов`, `Объём раздела — не больше N
  слов`) as part of the user message, not only in the asset: `max_tokens` is derived from per-phase ceilings (`TASK_PLAN_MAX_WORDS`,
  `TASK_SECTION_MAX_WORDS`, `TASK_VALIDATE_MAX_WORDS`) but a reasoning model spends its reasoning
  tokens *outside* that ceiling, so a section
  asked for loosely came back with thousands of tokens and a five-sub-task run took minutes. Ask for
  a section, not for prose.
- **The plan ceiling is per task and grows.** `MAX_PLAN_ITEMS` (10) caps the *first* plan — a
  runaway plan would stretch the run — but the limit lives on `TaskItem.plan_limit` and rises when
  real work appears: a plan rebuilt after the user's edits keeps every item (the user asked for
  them), and sub-tasks named by validation are appended whole rather than truncated. `plan_built`
  truncates only the initial plan; `validation_verdict` never drops additions. The plan request
  names the task's current limit (`Число подзадач: от 3 до {plan_limit}`), so a grown ceiling is
  what the model is asked for on the next round.
- **Bounded loops.** `MAX_PLAN_ROUNDS` (5) caps Planning rounds — on the last round the plan is taken
  as is; `MAX_VALIDATION_ATTEMPTS` (2) caps the Validation→Execution fix loop — exhausted attempts
  finish the task `Done` *with* the unresolved issues listed, and a review issue that names no
  sub-task ends the task right away, because there would be nothing to fix.
- **The result lands in its own file, and the review is deliberately lenient.** When a task reaches
  Done, `task_state.write_result()` writes `tasks/<номер>-<латинская-слога>.md` (`TABLETOP_TASKS_DIR`,
  gitignored, isolated in tests like every other state path): goal, a one-line итог and one `##`
  section per plan item, plus an issues block when some remarks stayed open. The file text is built
  in `task_state.result_markdown()` without Russian plurals («Разделов: 9») — the phrase with
  declension is the terminal layer's job (`plural_ru`), and the TUI prints `Результат: <путь>` right
  after the итог. A write error returns `None` and never breaks the run. The validation prompt is
  written to pass a *usable* artifact: only an absent sub-task, contradictions that make the rules
  unusable and placeholders count as issues; nitpicks are explicitly not, and `MAX_VALIDATION_ATTEMPTS`
  is 3 — the live demo kept ending "с замечаниями" because any nitpick counted.
- **Validation is deterministic first.** `deterministic_issues()` flags a missing section, a section
  marked not-executed, an empty one and one whose answer hit `max_tokens` (`finish_reason == "length"`);
  the model review only adds to that list, and an unavailable/unparsable review never cancels the
  deterministic verdict — the summary says "модельная проверка недоступна". Plan and verdict JSON are
  parsed client-side (first JSON object in the text) exactly like the facts extractor, for the same
  reasoning-model reasons.
- **Artifact lives inside `task.json`**, one section per executed sub-task, keyed by plan index — not
  a file per task. The project already keeps answers verbatim in a store envelope, and a second state
  file would have meant another switch and another test-isolation pass for the same result.
- **Pause is a state flag, taken at the operation boundary.** The terminal enters cbreak mode for the
  run and reads the key with `keyboard.read_key_nowait()` *before* each operation — the in-flight
  request finishes, the state is already on disk, so nothing is lost and the resume continues from the
  saved step. `p` pauses; so does Ctrl+C, because cbreak keeps `ISIG`, so SIGINT surfaces inside the
  run loop and MUST be translated into a pause rather than into an exit (the app-wide "Ctrl+C exits"
  rule still holds everywhere else, `/exit` remains available from the prompt). The flag is stored in
  `task.json`, so a pause survives a restart.
- **Visibility is one `rich.Live` panel for the whole run, with the status bar under it.** The panel
  (task N of M, the four stages with the current one marked, the step, the expected action, the plan
  as a *list* — one sub-task per line with its mark, the artifact volume, the pause hint) lives from
  the first operation to the end of the run, and the live region also carries the status bar *below*
  the panel (`Group(panel, Rule, status)`) — the user asked for the status bar to be printed after
  the panel and never covered by it, which inside a live region means exactly this. The panel is
  `transient=True`, so the scrollback keeps just the app's own lines: `⏱` metrics per request, the
  pause line, the per-task summary. Journal prints during a live run go *above* the region — that is
  rich's behaviour for `console.print` while a `Live` is active, and it is why the question about
  edits no longer needs the panel to be stopped (stopping and restarting the same `Live` made the
  redraw overwrite the printed lines). `/task` prints the full snapshot instead; for a queue with no
  unfinished task it prints the queue and stops there (there is no active stage then — asking for it
  used to raise `ValueError`).
  Marks like `[x]` MUST go through `rich.markup.escape`: rich reads `[x]` as an unknown tag and
  silently drops it (plain `[ ]` survives — do not "fix" that asymmetry by hand).
- **The answer to the plan question is read key by key, with `keyboard.read_char()`, and rendered in
  the panel.** `readline` cannot be used inside the run: the terminal is in cbreak mode for the pause
  key and cbreak does not echo, so the typed text would be invisible — and stopping the panel to ask
  would contradict "the panel shows the state always". `read_char()` exists because `read_key()`
  returns a single *byte*: Cyrillic is two bytes in UTF-8, so a byte-wise reader turned the user's
  edits into replacement characters before they reached the model.
- **Validation may extend the plan, and the fix round never redoes finished work.** The review's JSON
  may carry `items` — work the plan did not have. `validation_verdict(state, issues, new_items)`
  appends them to the plan (bounded per round by `MAX_PLAN_ITEMS`) and both they and the *unfinished*
  sub-tasks (`incomplete_indexes`: no section, failed section, empty section) go into `fixing`;
  Execution executes exactly those, so the panel marks the new ones `[ ]` → `[x]` like any other.
  An issue about a section that is already written does **not** restart it — the user's rule is that
  finished work is not redone; the issue stays in the list and shows up in the Done summary (a
  truncated section counts as written for the same reason, with its issue reported, not re-generated).
  Without the new-items path a general issue ("the artifact lacks X") had nothing to fix and ended the
  task with an open remark.
- **The run's spend is shown in the panel, labelled — and nowhere else.** Every pipeline step
  records `(label, meta)` into `self._task_spend`, where the label is the point *before* the request
  (`Execution, 2/3: «…»`, `Validation, попытка 1/2`); the panel renders the last request with its
  label, time, tokens and cost plus the run total. `_print_task_step` deliberately prints **no**
  `⏱` lines during a run (per-request numbers without "what for" were the first complaint, a stream
  of them in the journal the second): the journal keeps only notices and the task summary, and the
  `⏱` line still belongs to ordinary question answers.
- **The standard input field is visible during the run, and Enter uses it.** The live region is the
  panel + status bar + `INPUT_PROMPT` with `self._run_input` — the same prompt literal the main loop
  uses (`INPUT_PROMPT`, `_next_input`). `_collect_typed_keys` drains keys between operations: a bare
  `p` on an empty line still pauses, Enter with a non-empty line pauses *and* hands the line to the
  main loop through `_pending_input` (so `/task`, `/exit` or a question are handled normally, the run
  having stopped at a safe boundary). Backspace edits the line; nothing typed is lost. The panel's
  `TASK_PAUSE_HINT` line ("Пауза — клавиша p или Ctrl+C; Enter — выполнить введённое") is rendered
  on **every** frame of the run — the user asked for the pause recipe to be always on screen, so it
  must not be hidden behind the busy label or the edits question.
- **The queue is not the dialogue.** `/clear` empties the dialogue and never the queue; only
  `/task stop` drops it, and only while the run is not in progress (the prompt is busy during a run,
  so that is structural, not a guard). A failed request marks the task `не удалось` with the reason and
  the run moves to the next task — pipeline failures never raise into the UI.
- **`tests/e2e/harness.AppSession` passes `TABLETOP_TASK_FILE`** like the other stores; without it an
  e2e run would read and write the real `task.json` in the repo root.

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
  `client.ask_with_usage(..., model=...)` — including the strategies' auxiliary requests, so one
  session = one model.
- The panel follows the same reducer pattern (`initial_state(current_model)` puts the cursor on
  the active model); the active model is additionally marked "(текущая)" in the render, separate
  from the cursor. Enter applies, Esc cancels with zero API calls.
- No client-side model validation: the list is fixed so free-form input is impossible, and an
  unknown model surfaces as a regular API error through the existing `APIError` path.
- The status bar shows `Модель: <имя>` right after "Готов" — several e2e snapshots assert on it.
- After every answer `TabletopAITUI._print_usage_meta()` prints a screen-only line with response
  time, token counts and cost (`"неизвестно"` when the model has no entry in `MODEL_PRICING`); the
  auxiliary calls of a strategy record their spend in the ledger without printing. This line is
  never written to `history.json` and never replayed on restart — it exists only at the moment of
  the answer.

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

**`ui/keyboard.py` (raw terminal input for the interactive panels: `/commands`, `/settings`, `/branches`, `/models`, plus the task run's pause key)** — two non-obvious constraints,
both found by testing against a real PTY rather than mocks:
- Read raw bytes with `os.read(fd, 1)`, not `sys.stdin.read(1)`. The buffered `TextIOWrapper` can
  pull multiple bytes of an escape sequence (e.g. arrow key `ESC [ A`) into its own internal buffer
  in one syscall; the `select()` lookahead used to distinguish a lone Esc from the start of an
  arrow sequence only sees the fd, not that buffer, so it misreads a burst as Esc + stray chars.
- `read_key_nowait()` is the non-blocking sibling of `read_key()` (empty string when nothing is
  typed): the task run redraws its panel and polls the pause key between operations, and a blocking
  read there would only notice the keypress after the *next* operation.
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
`config.MODEL_PRICING`). The agent calls `ask_with_usage_messages()` (full message stack) for the
normal question path and every auxiliary strategy call; `ask()`/`ask_with_usage()` remain as
two-message sugar over the same `_request()` for callers that don't need the stack.

**`core/tabletop_agent.py`** — the agent entity: the single place that decides what goes to the
LLM. `TabletopAgent` owns the whole dialog memory and every representation of it: the append-only
session log (`_turns`), the strategy memory (summary, facts block, branch tree) and the
`history.json` manager. Every question goes out as system (from the current `AnswerSettings`,
rebuilt per ask) + the active strategy's view of the log + the new user prompt; see **Context
strategies** above for what each strategy does and why the log is never pruned. `reset()` (wired
to `/clear`) empties the log, the strategy memory and the file. `AgentConfig` is the agent's single
config object — session `AnswerSettings` plus the session `model`. The TUI's `settings`/`model`
properties route into it. After every successful request — `ask()` and each auxiliary strategy call
(summarizer, facts extractor) — the agent keeps the returned `AnswerMeta` on the read-only
`last_result` property; the read-only `last_compression` and `last_facts` hold the last digest
report (messages/exchanges folded) and the last facts update (success, key count, error), and the
`RequestPhase` listener drives the spinner labels (`Суммаризация...` during compression,
`Обновление фактов...` before the question on the facts strategy, `Отправка...` for the question
itself). The agent prints nothing: errors surface as `APIError` to the caller, except a failed
facts extractor, which is reported through `last_facts` so the answer still reaches the user.
`TabletopAITUI` is a thin view over it: it renders reports from `context_report()` and never
touches agent internals. The `history.json` manager is passed at construction, so the context
survives restarts without any UI involvement — `TabletopAgent.__init__` calls `restore_context()`
itself, and `ask()` appends each successful exchange to the file immediately. Restore seeds the
stored summary, the facts block and the exchanges not covered by the summary (rebuilt through
`build_user_prompt` with the *current* settings, because `history.json` stores the raw question,
not the assembled prompt; the assistant turn is kept verbatim); uncovered history is summarized in
token-bounded chunks at the first question, by the same trigger machinery — no separate restart
code, and only under the `summary` strategy. The summary strategy keeps `prompt_tokens` bounded
(summary + a small verbatim tail); the window and facts strategies bound it by N messages, so the
usage line reflects the strategy rather than only the dialog length.

**`core/usage.py`** — `estimate_cost(model, prompt_tokens, completion_tokens)` is a pure function
over `config.MODEL_PRICING`; it exists as its own module (not inlined in `api_client.py`) so cost
math is testable without HTTP mocking. Day-8 additions live here too: `estimate_tokens(text)`
(a deliberate *approximation* — `ceil(len/ESTIMATED_CHARS_PER_TOKEN)` with
`ESTIMATED_CHARS_PER_TOKEN = 3`, conservative for Cyrillic; never a real tokenizer, always
marked "≈" in output), `SessionLedger` (the per-session accumulator the agent feeds with every
successful `AnswerMeta`; unknown cost of any single request poisons the session cost to `None`)
and `sum_usage(dialogues)` (totals over `history.json` records carrying a `usage` block).

**`/usage` (`ui/tui_app.py::_print_usage_report`) and `/context`
(`ui/tui_app.py::_print_context_report`)** — two non-interactive reports printed to the permanent
log, zero API calls. `/usage` covers tokens: last request, session totals (from the agent's
ledger) and lifetime totals over the saved history (uncapped — the file holds every exchange).
`/context` covers the context state and renders `agent.context_report()`: strategy, window and
ceiling, turns in the next request, summary coverage, the facts block, branches and the
approximate token estimate (marked "≈"). The split is deliberate — the context window line left
`/usage` when `/context` appeared. Both are wired through `COMMAND_OPTIONS`. The status
bar (`_print_status_bar`) shows the active strategy and `Сессия: <tokens> ток., <cost>` after every step —
session-scoped only (restored exchanges carry no usage metrics). The per-session dialogue
counter is removed from the status bar by design. Truncation warnings
(`finish_reason == "length"` in `AnswerMeta`, captured by the client from `choices[0]`):
empty content → "модель исчерпала бюджет max_tokens" (the documented reasoning-model failure
mode), non-empty → "ответ мог быть обрезан". Question path only — the strategy's auxiliary
requests keep their pinned `max_tokens`.

**`core/history_manager.py`** — `history.json` (gitignored) holds the short-term layer (the
dialogue, verbatim) and the working layer (the current task's data):
`TabletopAgent` owns the manager (passed at construction, `TabletopAgent(client, history=...)`),
seeds its context (summary, facts, working memory, uncovered tail) from it automatically at
creation, appends every successful exchange to it immediately inside `ask()`, and empties the
short-term and working layers in `reset()`. Long-term memory is a *different* store
(`core/long_term_memory.py`, `memory.json`), so `/clear` cannot reach it. The TUI only displays the
file's contents (startup replay) and the memory snapshot — it never writes or clears a store itself
except through the agent's memory methods. The file is an envelope
`{"summary", "summary_covers", "facts", "working", "dialogues"}`; a bare list (the old format)
loads as an envelope with an empty summary, no facts and an empty working block, a missing `facts`
or `working` key reads as an empty block, and a missing or corrupt file reads as empty history.
There is NO eviction cap — every exchange stays verbatim forever, so the «всего диалога» total in
`/usage` is the true all-time spend of the file. Each record may carry a `usage` block
(`{"question", "answer", "usage": {prompt_tokens, completion_tokens, total_tokens, cost_usd}}`)
written by `ask()`; records in the old shape load unchanged and are just skipped by totals.
`summary`/`summary_covers` are updated by the agent's compression (a digest of the leading
exchanges the model folded; `summary_covers` counts *file* records), `facts` by the extractor, and
both are restored on construction — a lost or stale summary is not data loss, it is recomputed from
the verbatim records. `working` is written by memory routing and by `/memory goal`. Branches are NOT
saved: `dialogues` stays a flat list across branches.

## Test layout

- `tests/unit/` — no subprocesses, ~3s for the whole layer. `core/` logic (including the memory
  routing table, the long-term store, the profile store and the setup dialogue automaton, the task
  state machine with every transition branch and the task pipeline against a fake request), the
  `/commands`, `/settings`, `/branches` and `/models` reducers, and `TabletopAITUI` driven through
  injected dependencies:
  `TabletopAITUI(console=, history=, client=)` takes a `rich` console writing to a buffer, a
  `HistoryManager` on `tmp_path`, and a fake client that records what was asked. Passing a client
  also skips the API-key prompt at startup. Agent and TUI tests patch the *default* stores
  (`tabletop_agent.HistoryManager`/`LongTermMemory`/`ProfileStore`/`TaskStore`) so no test ever
  touches the real `history.json`/`memory.json`/`profile.json`/`task.json`. The setup dialogue is driven in unit tests by
  replacing `sys.stdin` (it reads lines, not `input()`), and in e2e by sending a line only after the
  app's own question is on screen — the echoed answer appears before the next question is ready, so
  waiting on the echo loses an answer.
- `tests/unit/test_keyboard.py` — the only unit file that needs a pty (see above).
- `tests/e2e/` — the real `tabletop-ai-assistant.py` running in `pty.fork()`, with output fed
  through `pyte` so assertions read the *rendered* screen rather than a stream of cursor codes.
  `harness.AppSession` is the driver (`wait_for` searches the scrollback, `wait_on_screen` and
  `wait_until_gone` search only the current screen — needed when the text also appears earlier in
  the log, e.g. the input prompt around the `/settings` panel). `stub_api.StubAPI` is a threaded
  local server that both answers and **records every request**, which is where most of the value
  is: the tests assert on what actually went to the API (system message per format, the word and
  list limits in the user prompt, `max_tokens`, the session temperature, the absence of `stop`,
  the retry count, and — for `/profile` — that two profiles produce two different system messages
  for the same question).
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
- `tests/e2e/test_task_flow.py` — the task pipeline in a real pty: a full two-task run without a
  single keypress (the stub records the order and shape of the plan/execute/validate requests), the
  edits round rebuilding the plan, a pause pressed mid-Execution and the resume continuing from the
  saved step, a restart continuing the same task from `task.json`, `task.json` surviving `/clear`, a
  failed task keeping the queue moving, and the repo's real `task.json` staying untouched. The run is
  slowed down by the stub replies' `delay` so the panel states are observable: with instant replies a
  whole run finishes in milliseconds and the assertions race the app.
- `tests/e2e/test_profile.py` — the setup dialogue in a real pty: five questions answered line by
  line, the profile landing in the temp `profile.json`, the next question carrying the profile
  message, `/clear` and a restart keeping it, `Ctrl+C` cancelling without writing, and the repo's
  real `profile.json` staying untouched.
- `tests/e2e/cassettes/` — real `deepseek-v4-flash` answers, recorded once with
  `pytest tests/e2e/test_recorded_answers.py -m network --record-cassettes` (needs a real key,
  spends quota) and replayed by the stub afterwards. Tests skip themselves when a cassette is
  missing, so the repo works without a key.
