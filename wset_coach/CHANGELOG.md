## 1.0.0

- First release: the Mac-only WSET Study Coach connector (same ten tools, same preview → apply rules) running on the Green, exposed to Claude through the `wset_mcp` integration behind Home Assistant sign-in (admin only).
- The coaching workspace (`AGENTS.md`, `state/`, `quizzes/`, `tastings/`, textbook text) is a git repository in `/share/wset_coach/repo`. Every applied write is one commit; a write that cannot be committed is rolled back.
- Mac sync over git smart HTTP at `/api/wset_mcp/git/repo`, same sign-in. Pushes must fast-forward and are serialised with coach writes by one lock.
- Per-user rate limit (60 requests a minute per endpoint) and one JSON log line per request (user, MCP method/tool, status, time).
