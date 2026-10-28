# Agent-driven sync: Claude Code runs the Match and Split cycle

Date: 2026-10-27. Status: approved by the user on 2026-10-27, with category creation through the YNAB API added at their request.

## Intent

The user wants to say, in Claude Code, "sync my Amazon transactions with
YNAB" and have Claude run the whole YNABuddy cycle: fetch the YNAB
transactions not yet handled, scrape Amazon, match, split by item,
categorize from the budget's existing categories, and write the result to
YNAB. Claude comes back to the user only for judgment calls: an item that
fits no category well, a match that is ambiguous, or a cluster of
purchases that suggests the budget is missing a category. One
confirmation, showing every change, precedes the write.

Why: the user has just started with YNAB and wants Amazon purchases
categorized at item level without working the console app's review screen
transaction by transaction. The console app stays as it is for anyone who
prefers it.

Spike findings this design rests on (2026-10-27, the user's real budget
and Amazon account):

- The user's 18 Amazon-payee YNAB transactions are all approved already,
  so the console app's `unapproved` run mode finds none of them.
- With amazon-orders 5.2 and the browser sign-in (both now in the
  working tree), a 60-day scrape takes under 30 s and 17 of the 18
  transactions match a card charge; 1 is ambiguous (two charges fit).
- Amazon's sign-in needs a person at the keyboard once; afterwards the
  saved session carries every run.

## Goals

- One request in Claude Code runs the cycle end to end; the user answers
  only the questions Claude cannot settle, then confirms once.
- Each run handles only what earlier runs have not: a per-plugin mark
  records the newest transaction written to YNAB.
- Every item gets a category from the budget's existing categories; the
  user's answers are remembered, so the same item is never asked twice.
- Claude can propose a new category when several items fit none; once
  the user approves it in chat, the app creates it in YNAB through the
  API, and the run continues with it.
- The console app keeps every current behaviour.

## Non-goals

- Changing the review screen, the console menus, or the console AI
  provider (`core/agents.py`). In this mode Claude Code is the agent; the
  app's own AI connector is never called.
- Any YNAB write other than transactions and the categories the user
  approved by name in chat. No renames, no deletions, no budgeting
  amounts.
- Costco. The mode is plugin-neutral because core is, and nothing in it
  may be Amazon-specific, but v1 is built and tested with the Amazon
  plugin.
- Running without a display. The first Amazon sign-in opens a browser
  window.

## Anti-goals (a failure even if it works)

- Writing to YNAB without the user having seen the full list of changes
  and said yes once. No partial or silent writes.
- Claude guessing a category for an item it is unsure about and pushing
  it. Unsure means ask.
- Printing secrets, cookies, or raw order data into the chat beyond what
  the user needs to decide (item names, amounts, dates, order numbers are
  fine).
- A category marked as the user's choice when Claude chose it.
- A category created in YNAB that the user did not approve by name, in
  the group Claude named, in this conversation.

## Constraints

- CLAUDE.md's coding rules apply: plain stdlib code, money in
  milliunits, JSON files via `read_text`/`write_text`, questionary only
  in `core/ui.py`, statuses only where the data checks them, tests that
  fail on plausible bugs, offline tests.
- Claude drives the app from a shell (the Bash tool). Commands must be
  non-interactive: no prompts, results as JSON on stdout, status lines in
  `ynabuddy.log` as today, a non-zero exit code and a one-line reason on
  failure.
- The YNAB rate limit (200 requests/hour) means the bulk endpoints as
  today; a run should not need more than a handful of requests.
- `.claude/` and `CLAUDE.md` are gitignored on this install. Anything
  Claude needs to know about the procedure that should be versioned with
  the code goes under `docs/`.

## How it works

### The mark

A per-plugin file `plugins/<plugin>/<plugin>_sync.json`, owned by core,
named by the plugin like its other state files:

```json
{"last_pushed_date": "2026-10-25", "last_run_at": "2026-10-27T21:30:00+00:00"}
```

`last_pushed_date` is the YNAB date of the newest transaction the mode
has written successfully. It advances only when YNAB confirms a push.
Absent on the first run.

A run's candidates are the YNAB transactions that match the plugin's
mapping (account list or payee name, as today), dated on or after the
mark (all history when there is no mark), that are not already split in
YNAB and whose id is not in the plugin's pushed list. Approval status is
ignored: this mode has its own notion of handled, which is the mark plus
the pushed list. A transaction older than the mark that an earlier run
skipped or could not match is not revisited unless the user asks for an
earlier start (`--since <date>`).

The scrape looks back from the earliest candidate minus the global
lookback margin, as the cycle does today.

### Driving it: a non-interactive command

`run.py` gains a subcommand surface for this mode and keeps starting the
console app when given no arguments. The subcommands are the cycle's
steps, so that Claude can look at each step's result before the next and
the user can see in the log what happened:

- `status <plugin>`: is Amazon signed in (saved session present), the
  mark, the mapping, counts of queued approvals. Claude runs this first
  and, if not signed in, tells the user a sign-in window is about to
  open before anything else.
- `fetch <plugin> [--since <date>]`: candidates from YNAB, the scrape,
  matching and splitting, as one step: it is what the console cycle does
  up to the review, with the mark's candidate rule instead of the run
  mode. Writes the plugin's matched file as today. Output: per
  transaction its id, date, amount, match status and reason, order
  number, and split lines with memo, amount, and any category already
  known from the plugin's item-to-category table (the review screen's
  prefill, applied here).
- `decide <plugin> <decisions.json>`: applies Claude's and the user's
  decisions and queues the approved transactions, exactly as approving
  on the review screen would (same approved-queue entries, same
  item-to-category table updates). Output: what was applied and what is
  still undecided.
- `preview <plugin>`: the push plan, one entry per queued transaction
  with its lines. This is what Claude shows the user for the single
  confirmation; it is produced by the app, not composed by Claude, so the
  user sees what will be written.
- `push <plugin> --yes`: pushes the queue through `core/push.py` with the
  y/n prompt answered by the flag, re-checking YNAB for changes as today,
  then advances the mark to the newest pushed transaction's date. Output:
  pushed ids, failures with reasons, the new mark.
- `add-category --group <group name> --name <category name>`: creates
  the category in the selected budget through the YNAB API (the SDK's
  `create_category`; a group that does not exist is created first with
  `create_category_group`) and refetches the category list. Output: the
  new category's id, name and group. Claude runs it only after the user
  approved that exact name and group in chat. This is the app's second
  write path to YNAB, beside `core/push.py`; it lives in
  `core/ynab_client.py` next to the other YNAB calls and is used by no
  console menu.

The decisions file is written by Claude:

```json
{
  "transactions": [
    {
      "id": "<ynab transaction id>",
      "lines": [{"line": 1, "category_id": "<id>", "by": "agent"},
                {"line": 2, "category_id": "<id>", "by": "user"}],
      "approve": true
    },
    {"id": "<ynab transaction id>", "approve": false, "reason": "ambiguous match left for the user"}
  ]
}
```

`by` records who chose: `agent` when Claude decided, `user` when the
user answered a question. The item-to-category table stores it the same
way the console app does (its `by` field), so a later console review
shows the same provenance. A transaction is queued only when every
non-tax line has a category and `approve` is true.

### What Claude does, in order (the procedure)

The procedure lives in `docs/agent-sync.md`, versioned with the code, and
an untracked `.claude/skills/sync/SKILL.md` on this install invokes it so
"sync my Amazon transactions" triggers it. Claude:

1. Runs `status`. If not signed in, warns the user that a browser window
   will open for Amazon sign-in, then continues.
2. Runs `fetch`. Reads the result. Transactions already fully
   categorized by the table need no decision.
3. For each line without a category, picks one from the budget's
   categories, from the item name, the order's other items, and the
   table's existing entries for similar items. Marks the choice `agent`.
   Where it is unsure between two or more, or none fits, it collects a
   question. Where several uncategorizable items share a theme, it
   collects a proposal for a new category instead, naming the group it
   would go in and the items it would take.
4. Asks the user all collected questions in one message where possible,
   each with the item, amount, order date, and Claude's best guesses.
   Ambiguous matches are questions too: approve the closest-dated record
   the match took, or skip the transaction (v1 surfaces only that
   record; picking among the fitting records is a later addition). A proposed new category the user accepts, by name and
   group, Claude creates with `add-category`, then re-runs `fetch`
   (which refetches categories) and continues.
5. Writes the decisions file, runs `decide`, then `preview`, and shows
   the preview to the user: one line per transaction (date, amount,
   order number) and its split lines with categories.
6. On the user's yes, runs `push --yes` and reports what was written and
   the new mark. Anything else is edited and previewed again.

The first run has no mark and covers every Amazon-payee transaction in
the budget (18 today); later runs are expected to take a minute and ask
zero to two questions.

### Decisions already made

- Single confirmation at the end, not per transaction (the user's
  choice).
- Splits by item, as the console app does (the user's choice).
- Amazon data comes from the plugin's scrape with amazon-orders 5.2
  and the browser sign-in; no Amazon password is stored (done, in the
  working tree).
- The app creates a YNAB category only after the user approved its name
  and group in chat (the user's choice: they want Claude to make the
  change once they have said yes, not to do it by hand in YNAB).
- The non-interactive surface is subcommands of `run.py` with JSON on
  stdout (my call: the Bash tool is how Claude acts; several steps, not
  one, so each result is inspectable and the log stays readable).
- The procedure is versioned under `docs/`, the skill is an untracked
  pointer (my call, because `.claude/` is gitignored here).

### Answers to the builder check (2026-10-27)

- **The Bash tool can open the sign-in window and run the scrape.** The
  spike that signed in and scraped was driven from Claude Code's Bash
  tool on this machine: the window opened, the user signed in, and the
  60-day scrape finished in under 30 s. The tool's foreground limit is
  10 minutes, so the procedure runs `fetch` in the background when the
  mark is absent or far back, and the plugin already saves after each
  year of orders. On a headless or sandboxed install `status` reports
  "not signed in" and the procedure stops there with the console app's
  Settings as the way to sign in.
- **`push --yes` writes the whole queue; the procedure is the guard.**
  `preview` shows the plugin's whole approved queue for the budget,
  console-queued entries included, and `push --yes` writes that same
  queue through `push_approved` and lists every id it wrote. No snapshot
  token: the procedure runs `push` right after the preview the user
  said yes to, with nothing in between, and a check the change did not
  ask for is against CLAUDE.md's rules (my call).
- **A scrape failure is reported inside a successful `fetch`.** As in the
  console cycle, matching goes on with the data on disk; `fetch` exits 0
  and its output carries a `scrape` block: `ok`, `failed` with the
  reason, or `skipped`. The procedure has Claude tell the user about a
  failed scrape before any question and go on only if they say so (my
  call; the SDK has no way for a plugin to say how far its data on file
  reaches, so that date is not reported).

## Left to the builder

- Module layout for the subcommands (a `core/sync.py` or similar that
  reuses `match`, `split`, `review.prefill`, `push.queue`,
  `push.push_approved`; the argument parsing in `run.py`/`main.py`).
- The exact JSON shapes beyond the fields named above; how an ambiguous
  transaction's other fitting records are surfaced so the user can pick
  one (the matched file today keeps only the closest-dated record).
- How `push --yes` reuses `push_approved` without a prompt (a parameter,
  not a copy).
- Test design, within CLAUDE.md's rules; fixtures in `tests/fixtures/`.
- DIAGRAM.md and CHANGELOG.md updates, README section for the mode, and
  CLAUDE.md's "writes only through `core/push.py`" line, which becomes
  "transactions through `core/push.py`, categories through
  `add-category` after the user's approval".
- The mark file is per-install data: gitignored like the other
  `plugins/*/*_*.json` files, named by the plugin like them (an SDK
  attribute), and CLAUDE.md's file list updated.
- INFO status lines must not land on stdout when a subcommand runs, or
  they corrupt the JSON; the log file keeps them as today.
