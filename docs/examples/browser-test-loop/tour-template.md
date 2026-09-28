This is the tour document for the example app. A Test process reads all of it on every pass.
Every heading below is one area, and a finding names its area exactly as the heading spells it,
so keep this opening text free of headings and give each area a heading of its own.

The driver is `tools/drive.py`, a headless browser run from the shell. Each command starts the
browser, does its steps, and closes the browser before it returns. Run
`python3 tools/drive.py visit --help` for every step it takes. A typical call:

    python3 tools/drive.py visit --url http://127.0.0.1:8765/search \
        --state ~/.example-app/storage-state.json --signin-marker form#signin \
        --step 'fill:#query=>lamp' --step press:Enter --step wait:.result \
        --screenshot /tmp/search.png --text

The signed in session is the storage state file `~/.example-app/storage-state.json`, outside
the repository, written once by the operator with `python3 tools/drive.py signin`. Pass it to
every `visit` with `--state`, and pass `--signin-marker form#signin`, the selector only the sign
in page matches. When `visit` exits 3 the file is missing or unreadable, and when it exits 4
the app showed its sign in page: stop and report the pass as `not_run`. Never type a credential.

Outbound integrations are stubbed while the loop runs: mail, payments, and webhooks go to local
files and never leave the machine. That is a second line of defense, not permission. Every
approval step named below is still checked up to the step and never approved.

## Search

What to check:

- A search for a word shows every item whose name or description contains it.
- The result count above the list matches the rows shown.
- An empty search shows the empty state, not an error.

What counts as a defect: a missing or duplicated result, a count that disagrees with the list,
an error in the console, or a layout that hides a result.

Approval steps not to pass: none.

## Orders

What to check:

- The order list loads, newest first, and each row opens its order.
- An order's total equals the sum of its lines.
- Editing a line's quantity updates the total without a reload.

What counts as a defect: a wrong total, a row that opens another order, or a change that is lost
on reload.

Approval steps not to pass:

- **Send the invoice.** Open an order and choose Send invoice. Check that the confirmation shows
  the right customer, total, and address, then stop. Never choose Confirm.

## Settings

What to check:

- Each settings page loads and shows the saved values.
- A change saved on one page is still there after a reload.

What counts as a defect: a value that does not save, a page that does not load, or a control
that cannot be reached with the keyboard.

Approval steps not to pass:

- **Delete the account.** Check that the confirmation names the account, then stop. Never
  confirm it.
