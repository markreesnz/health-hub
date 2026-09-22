## 2.0.7

- Show every draw percentage and its annual dollar amount directly below the compact spending bar.

## 2.0.6

- Restore the compact Plan spending bar with corrected capital and draw levels. Label historical annualised pace separately from the planned budget; collapse explanations.

## 2.0.5

- Align Plan drawdown comparisons with daily review capital, including KiwiSaver, expected receipts and planned costs.
- Show recorded spending, annualised pace and funded allowance separately; compare 2–4% draw levels in a compact mobile layout.

# Finance 2.0.4

- Add a phone-friendly **Copy current numbers** action for pasting a dated Finance snapshot into ChatGPT or Codex.
- Keep observed balances and transactions separate from budgets, property estimates and expected receipts in the copied snapshot.
- Exclude credentials, account identifiers and the full historical transaction ledger from clipboard output.

# Finance 2.0.2

- Replace the wide transaction table with tight single-line rows; tap to expand editing controls.
- Fit the complete payee-and-amount row on phone screens without horizontal scrolling.

# Finance 2.0.1

- Compact transaction rows with descriptions expandable on tap.
- Hide routine save and bank-sync status; show small retry notices only on failure.

# Finance 2.0.0

- Simplified Spending, Plan and Settings around the visible daily workflows.
- Added transaction search and an explicit form for updating manual holdings.
- Improved phone forms, touch targets, dialog scrolling and table overflow.
- Replaced backup-based device merging with revision-checked current state and recoverable conflicts.
- Kept immutable daily recovery copies and a pre-migration archive.
- Removed unused AI services, token fields, import prototypes and page-open tracking.
- Release frontend and server together; old open pages must reload before saving.

Validation: 11 Python and 14 JavaScript tests; 320px and 390px browser checks; migration replay preserves all live transactions, learned rules, snapshots and manual balances.

Recovery: current state is in /share/financial/current-state.json. The pre-release state is retained at /share/financial/backups/pre-simplification.json. Export current state before reverting an image so subsequent edits remain recoverable. Restore a compatible state before reverting storage versions; do not blindly replace current state with a historical backup.
