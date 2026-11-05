# ghwatch

A shared GitHub inbox for several Claude Code sessions on one machine.

One background **poller** fetches your GitHub notifications and activity
events every 60–300 seconds and stores them in SQLite for 30 days. Each
session registers as a **consumer**, with its own filter and read position.
It gets a line on stdout for every new item that matches. Before acting on an
item, a session **claims** it, so two sessions never handle the same thing.

```
GitHub API ──► poller (one per machine, flock) ──► ~/.ghwatch/ghwatch.db
                                                     │
                     ┌───────────────────────────────┼───────────────────────┐
              watch -c reviews               watch -c mobile           watch -c triage
              --reason review_requested      --repo 'org/mobile*'      --all-events
```

Requirements: Python 3.9+ (stdlib only) and a logged-in `gh` CLI.

## Using it from a Claude session

Start a Monitor that runs:

```bash
~/ghwatch/bin/ghwatch watch -c <session-name> [filters] 2>&1
```

Keep `2>&1`: without it, a crash would go to stderr and the Monitor would stay silent.

Each new matching item arrives as one line:

```
GH n:1234:2026-11-05T10:00:00Z | org/app | PullRequest [review_requested] | Fix login | https://github.com/org/app/pull/7
```

When one arrives:

1. `ghwatch claim <ID> -c <name>`. If it reports `ALREADY CLAIMED`, another session has the item, so leave it alone.
2. `ghwatch show <ID>` returns the full GitHub JSON.
3. Do the work.
4. `ghwatch done <ID> -c <name>`. Add `--read` to also mark the GitHub notification read.

Monitors expire, so re-arm with the same command. The cursor is saved, so nothing is missed or repeated.

Two special lines can appear:

- **`GHWATCH-UPGRADED ...`** means ghwatch was updated and the watch has exited. Re-arm it with the same command.
- **`GHWATCH-STALE ...`** means no successful poll has completed for about 12 minutes. The maintainer session is notified automatically (see below), but you can run `ghwatch status` yourself.

`watch` starts a poller automatically if none is running. As long as any session is watching, polling continues.

## Filters

Repeat a flag to OR its values. Different flags are combined with AND. Glob patterns work and matching ignores case.

| flag | matches |
|---|---|
| `--repo 'org/*'` | repository full name |
| `--reason review_requested` | notification reason (`mention`, `assign`, `author`, `comment`, `ci_activity`, `state_change`, ...) |
| `--type PullRequest` | notification subject type or event type (`Issue`, `PushEvent`, ...) |
| `--source notification` | `notification`, `received` (activity in repos you watch) or `own` |
| `--actor login` | who triggered an activity event |
| `--match text` | substring of the title |
| `--all-events` | include non-actionable activity; the default is actionable items only |

Filters are saved per consumer the first time you use them. Passing filters again replaces the saved ones.

**Actionable** means a notification whose reason is directed at you (anything except `subscribed`), or an activity event in which someone else @-mentions you.

## Commands

```
ghwatch watch -c NAME [filters] [--backlog] [--interval S] [--no-poller]
ghwatch inbox -c NAME [filters] [--all] [--unclaimed] [--limit N]
ghwatch claim ID... -c NAME
ghwatch release ID... -c NAME
ghwatch done ID... -c NAME [--read]
ghwatch show ID
ghwatch consumers
ghwatch status
ghwatch poll [--once]
ghwatch ensure-poller | stop-poller | restart-poller
ghwatch forget-consumer NAME
```

## Storage

All runtime state lives in `$GHWATCH_HOME`, which defaults to `~/.ghwatch`:

- `ghwatch.db`: SQLite in WAL mode, with the tables `events`, `consumers`, `claims` and `state`
- `poller.lock`: held by the running poller
- `poller.log`: poller output

Events older than 30 days, and their claims, are pruned on every poll.
Conditional requests (ETag and If-Modified-Since) mean an unchanged poll does not count against the rate limit.

## Reporting problems

A maintainer Claude session owns this project. It watches this repo and scans other sessions' transcripts for ghwatch errors. To report a bug or request a feature, open an issue:

```bash
gh issue create -R claude-voxar/ghwatch -t "<summary>" -b "<what you ran, what happened, what you expected>"
```

## Maintenance

```bash
ghwatch stop-poller
ghwatch restart-poller
ghwatch forget-consumer NAME
```

`~/ghwatch` is the production checkout and always stays on `main`. Do development in a separate worktree, such as `git worktree add ~/ghwatch-dev <branch>`. Editing or switching branches in the production checkout would change the code under every running watcher.

To deploy after a merge:

```bash
~/ghwatch/bin/deploy
```

The script fast-forwards `main` and runs the tests on this host. If they fail, it rolls back. If they pass, it restarts the poller. Running watchers detect the code change, print `GHWATCH-UPGRADED` and exit, so their sessions know to re-arm.

## Development

```bash
make test
```
