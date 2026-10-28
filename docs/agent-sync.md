# Syncing with Claude Code

How Claude Code runs YNABuddy for the user ("sync my Amazon transactions
with YNAB"): the cycle from `core/sync.py`, one subcommand per step, with
the user asked only about judgment calls and one confirmation before
anything is written. The design is in
`docs/superpowers/specs/2026-10-27-agent-driven-sync-design.md`.

Every command is `uv run python run.py <subcommand>`, run from the
project root. Each prints its result as JSON on stdout and nothing else;
status lines go to `ynabuddy.log`, and anything a library prints (the
Amazon sign-in notice) to stderr. A failure exits 1 with
`Failed <subcommand>: <reason>` on stderr.

## Rules that never bend

- Nothing is written to YNAB until the user has seen the `preview` and
  said yes once. `push --yes` follows that yes immediately, with no
  `decide` or `fetch` in between.
- A category is created (`add-category`) only after the user approved
  that exact name and group in chat.
- Unsure means ask. An item Claude cannot place with confidence, or that
  fits no category, is a question for the user, never a guess that gets
  pushed. A category Claude chose is recorded `"by": "agent"`, one the
  user answered `"by": "user"`.
- Never print secrets, cookies or raw order data into the chat. Item
  names, amounts, dates and order numbers are fine.

## The steps

1. **`status <plugin>`** (e.g. `status amazon.com`). Read
   `missing_settings`: if it names "Amazon sign-in", tell the user a
   browser window is about to open for them to sign in to Amazon, and
   wait for them to say they are at the keyboard. Even when nothing is
   missing, say before the fetch that a sign-in window may open if
   Amazon has dropped the saved session (it does so now and then): the
   plugin then waits up to five minutes for them. Read `mark`: with no
   `last_pushed_date` the first run covers every matching transaction in
   the budget.

2. **`fetch <plugin>`** (`--since YYYY-MM-DD` to start earlier than the
   mark). Run it in the background with a long timeout when the mark is
   absent or months back: a first scrape can take minutes. Then read:
   - `scrape.status`: `failed` means the vendor data could not be
     refreshed and matching used what was on disk. Tell the user, with
     `scrape.reason`, before asking any question, and go on only if they
     say so. `skipped` means nothing to scrape (no candidates, or the
     plugin doesn't scrape).
   - `transactions`: each with `match_status`, `match_reason`,
     `order_number` and `split_lines`. A line with `category_id` already
     set came from the plugin's item-to-category table (an earlier
     answer): it needs no decision. `ambiguous` means more than one
     vendor record fit and the closest-dated one was taken (`fetch`
     shows only that one): that is a question for the user, approve
     that record or skip the transaction. `unmatched` ones are reported
     and left alone.
   - `categories`: the budget's categories, `id` and `label`
     ("Group: Name"). Only these ids may be used.

3. **Categorize.** For every line without a category, pick one from
   `categories` from the item name, the order's other items and the
   categories similar items already have, and mark it `"by": "agent"`.
   Collect a question where two or more categories fit about equally or
   none does. Where several uncategorizable items share a theme (pet
   supplies, say), collect instead a proposed new category with the
   group it would go in and the items it would take.

4. **Ask the user** everything collected, in one message where possible:
   each question with the item, amount, order date and Claude's best
   guesses; each ambiguous match with the matched record's order number
   and date, to approve or skip; each proposed category with its group
   and items. For a proposed category
   the user accepts, run
   `add-category --group "<group>" --name "<name>"` with the name and
   group they approved, then run `fetch` again (it refetches the
   categories) and continue.

5. **`decide <plugin> <decisions.json>`.** Write the decisions file
   (shape below), run the command, and read the result: `queued` are
   the transactions now waiting to be pushed; `undecided` ones still
   have a line without a category (`missing` says which); `invalid`
   lists a category id that is not in the budget (fix and run `decide`
   again for those); `unknown_ids` were not in the fetch. A transaction
   the user said to skip goes in with `"approve": false`.

6. **`preview <plugin>`** and show the user its `entries`: one line per
   transaction (date, amount, order number) and its lines with
   categories. Ask for a single yes.

7. **`push <plugin> --yes`**, right after the yes. Report `pushed`,
   `not_pushed` (entries YNAB changed since the fetch, or that failed;
   they stay queued and `ynabuddy.log` says why) and the new `mark`.

## The decisions file

```json
{
  "transactions": [
    {
      "id": "<YNAB transaction id from fetch>",
      "approve": true,
      "lines": [
        {"line": 1, "category_id": "<category id>", "by": "agent"},
        {"line": 2, "category_id": "<category id>", "by": "user"}
      ]
    },
    {"id": "<id of a transaction with no split lines>", "approve": true, "category_id": "<category id>"},
    {"id": "<id>", "approve": false, "reason": "ambiguous match, user skipped it"}
  ]
}
```

`line` numbers are the `line` values from `fetch`. Lines already
categorized by the table need not be listed. A transaction with
`"split_status": "nothing to split"` (no line items) takes one
`category_id` for the whole transaction.
