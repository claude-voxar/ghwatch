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

Every session on this machine has the **`watch-repo`** skill (`skills/watch-repo`, linked into `~/.claude/skills` by `bin/deploy`). Asking a session to "watch org/app" is enough. The details below are what the skill follows.

Run the watch as a background command (Bash with `run_in_background`):

```bash
~/ghwatch/bin/ghwatch watch -c <session-name> [filters] --until-event 2>&1
```

It waits silently until something matches, prints that batch, and exits, and the exit is what wakes the session. After handling the batch, run the command again. The cursor is saved, so nothing is missed or repeated in between. A Monitor running `watch` without `--until-event` also works, but Monitors expire every 30 minutes and wake the session to re-arm even when nothing happened.

Keep `2>&1`: without it, a crash would go to stderr and the session would never see it.

Each new matching item arrives as one line:

```
GH n:1234:2026-11-05T10:00:00Z | org/app | PullRequest [review_requested] | Fix login | https://github.com/org/app/pull/7
```

When one arrives:

1. `ghwatch claim <ID> -c <name>`. If it reports `ALREADY CLAIMED`, another session has the item, so leave it alone.
2. `ghwatch show <ID>` returns the full GitHub JSON.
3. Do the work.
4. `ghwatch done <ID> -c <name>`. Add `--read` to also mark the GitHub notification read.

Two special lines can appear:

- **`GHWATCH-UPGRADED ...`** means ghwatch was updated and the watch has exited. Run the same command again.
- **`GHWATCH-STALE ...`** means no successful poll has completed for about 12 minutes. It repeats at most every 30 minutes per consumer. The maintainer session is notified automatically (see below), but you can run `ghwatch status` yourself.

`watch` starts a poller automatically if none is running. As long as any session is watching, polling continues.

## Filters

Repeat a flag to OR its values. Different flags are combined with AND. Glob patterns work and matching ignores case.

| flag | matches |
|---|---|
| `--repo 'org/*'` | repository full name |
| `--reason review_requested` | notification reason (`mention`, `assign`, `author`, `comment`, `ci_activity`, `state_change`, ...) |
| `--type PullRequest` | notification subject type or event type (`Issue`, `PushEvent`, ...) |
| `--source notification` | `notification`, `received` (activity in repos you watch), `own` or `repo` (a tracked repo's feed) |
| `--actor login` | who triggered an activity event |
| `--match text` | substring of the title |
| `--all-events` | include non-actionable activity; the default is actionable items only |
| `--skip-late-echoes` | drop our own `PushEvent`/`CreateEvent`/`DeleteEvent` that GitHub delivered over an hour late (they are echoes the pushing session already handled) |

Filters are saved per consumer the first time you use them. Passing filters again replaces the saved ones.

**Tracking repos.** Notifications and the account's own feeds only cover repos the account is subscribed to or active in. A `--repo owner/name` filter with no wildcards makes the poller follow that repo's event feed directly. You can also do this explicitly with `ghwatch track owner/name -c NAME`. To see a tracked repo's pushes, PRs and issues, combine the filter with `--all-events`:

```bash
ghwatch watch -c mobile --repo org/mobile-app --all-events --skip-late-echoes 2>&1
```

An event can come from more than one feed but is stored once, so filter on `--repo` rather than `--source`.

**Actionable** means a notification whose reason is directed at you (anything except `subscribed`), or an activity event in which someone else @-mentions you.

## Commands

```
ghwatch watch -c NAME [filters] [--until-event] [--backlog] [--interval S] [--no-poller]
ghwatch inbox -c NAME [filters] [--all] [--unclaimed] [--limit N]
ghwatch claim ID... -c NAME
ghwatch release ID... -c NAME
ghwatch done ID... -c NAME [--read]
ghwatch show ID
ghwatch consumers
ghwatch status
ghwatch poll [--once]
ghwatch ensure-poller | stop-poller | restart-poller
ghwatch install-agent | uninstall-agent
ghwatch forget-consumer NAME
ghwatch track OWNER/NAME... -c NAME | untrack OWNER/NAME... | tracked
ghwatch scan-transcripts [--follow | --until-event] [--exclude-session ID] [--from-start]
```

## Storage

All runtime state lives in `$GHWATCH_HOME`, which defaults to `~/.ghwatch`:

- `ghwatch.db`: SQLite in WAL mode, with the tables `events`, `consumers`, `claims`, `tracked_repos` and `state`
- `poller.lock`: held by the running poller
- `poller.log`: poller output
- `transcript-scan.json`: the transcript scanner's position

Events older than 30 days, and their claims, are pruned on every poll.
Conditional requests (ETag and If-Modified-Since) mean an unchanged poll does not count against the rate limit.

## Reporting problems

A maintainer Claude session owns this project. It watches this repo and scans other sessions' transcripts for ghwatch errors. To report a bug or request a feature, open an issue:

```bash
gh issue create -R claude-voxar/ghwatch -t "<summary>" -b "<what you ran, what happened, what you expected>"
```

The maintainer also runs a scanner over other sessions' transcripts, which reports failing `ghwatch` commands and `GHWATCH-STALE` notices without anyone filing an issue:

```bash
ghwatch scan-transcripts --until-event --exclude-session <maintainer-session-id> 2>&1
```

## Maintenance

```bash
ghwatch stop-poller
ghwatch restart-poller
ghwatch forget-consumer NAME
ghwatch install-agent
ghwatch uninstall-agent
```

### launchd agent

`ghwatch install-agent` installs `~/Library/LaunchAgents/com.github.claude-voxar.ghwatch.poller.plist`, which runs `ghwatch poll --wait-lock` with `KeepAlive`. Polling then continues through crashes and reboots, and while no session is watching. A poller that a watcher starts while the agent is running waits on the lock rather than competing.

With the agent installed, `restart-poller` (and so `bin/deploy`) restarts the agent onto the new code and retires any other poller. Run `install-agent` from the production checkout, because the plist points at the checkout it was run from.

`~/ghwatch` is the production checkout and always stays on `main`. Do development in a separate worktree, such as `git worktree add ~/ghwatch-dev <branch>`. Editing or switching branches in the production checkout would change the code under every running watcher.

To deploy after a merge:

```bash
~/ghwatch/bin/deploy
```

The script fast-forwards `main` and runs the tests on this host. If they fail, it rolls back. If they pass, it links `skills/*` into `~/.claude/skills` and restarts the poller. Running watchers detect the code change, print `GHWATCH-UPGRADED` and exit, so their sessions know to re-arm.

## Development

```bash
make test
```
