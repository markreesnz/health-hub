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
