# ghwatch

Polls GitHub for notifications and activity, stores them in a local SQLite
database for 30 days, and lets several Claude Code sessions on the same machine
each watch for the items they care about.

Requirements: Python 3.9+ (stdlib only) and an authenticated `gh` CLI.

```bash
make test
```
