# Changelog

## 1.0.0

First release.

* Find projects across the computer and report the ones that are not backed up: no version control, no commits, no remote, uncommitted changes, unpushed commits, stashes and secret files tracked in git.
* `keepcode backup` creates private GitHub repositories, completes the `.gitignore` and saves uncommitted and unpushed work to `keepcode/*` branches without touching files, the staging area or real branches.
* Secret detection for common token formats and secret files.
* Daily check with desktop notifications through the Windows Task Scheduler or crontab, with optional automatic backup.
* Commands: `scan`, `backup`, `check`, `login`, `logout`, `schedule`, `roots`, `ignore`, `config`, `doctor`.
