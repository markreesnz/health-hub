## 1.0.1

- Fix: the Home Assistant proxy could forward only the first chunk of a large request (for example saving a long planning file), causing a parse error. It now reads the whole body.

## 1.0.0

- First release: the Mac-only Finance connector (1.0.1, Finance 2.0.13 contract) running as a Home Assistant add-on, exposed to Claude through the `finance_mcp` integration behind Home Assistant sign-in (admin only).
- Talks to the Finance add-on directly on the Supervisor network; no Home Assistant token needed.
- New tools: `finance_planning_files`, `finance_save_planning_file` (planned payments and other planning JSON, stored on the Green).
