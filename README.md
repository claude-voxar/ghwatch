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
~/ghwatch/bin/ghwatch watch -c <session-name> [filters]
```

Each new matching item arrives as one line:

```
GH n:1234:2026-11-05T10:00:00Z | org/app | PullRequest [review_requested] | Fix login | https://github.com/org/app/pull/7
```

When one arrives:

1. `ghwatch claim <ID> -c <name>`. If it reports `ALREADY CLAIMED`, another session has the item, so leave it alone.
2. `ghwatch show <ID>` returns the full GitHub JSON.
3. Do the work.
4. `ghwatch done <ID> -c <name>`. Add `--read` to also mark the GitHub notification read.

If the Monitor prints `GHWATCH-STALE ...`, polling has failed for about 12 minutes; run `ghwatch status`.
Monitors expire, so re-arm with the same command. The cursor is saved, so nothing is missed or repeated.

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
ghwatch ensure-poller
```

## Storage

All runtime state lives in `$GHWATCH_HOME`, which defaults to `~/.ghwatch`:

- `ghwatch.db`: SQLite in WAL mode, with the tables `events`, `consumers`, `claims` and `state`
- `poller.lock`: held by the running poller
- `poller.log`: poller output

Events older than 30 days, and their claims, are pruned on every poll.
Conditional requests (ETag and If-Modified-Since) mean an unchanged poll does not count against the rate limit.

## Development

```bash
make test
```
