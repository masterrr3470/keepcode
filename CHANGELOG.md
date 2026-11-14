# Changelog

## 1.1.0

* **The keepcode app.** Double-clicking `keepcode.exe`, or the new desktop and Start menu shortcuts, opens keepcode in its own window: scan, choose, back up, connect GitHub, update and change settings without typing a command. `keepcode app` opens it from a terminal. Where the window cannot open, keepcode falls back to the terminal.
* **Windows executable.** keepcode is published as a single `keepcode.exe` built by GitHub Actions, with a one-command installer (`install.ps1`) that adds the shortcuts and opens the app. No Python needed, no administrator rights.
* **Guided mode.** Running `keepcode` in a terminal without a command walks through updating, scanning, backing up and the daily check.
* **Animated scan.** A grid of squares, like the GitHub contribution graph, lights up while keepcode scans, in the app and in the terminal. Each project gets its own square in the colour of its status, and squares turn cyan as projects are backed up. In the app, clicking a square shows its project.
* **Login in the browser.** `keepcode login` uses GitHub's device flow: type a short code in the browser and click Authorize. No tokens to create or paste.
* **Automatic updates.** keepcode checks GitHub Releases when it opens and offers to update itself, verifying the SHA-256 of the download. New `keepcode update` command and `config --auto-update` setting.
* **FiveM servers.** A server and all its resources count as one project, and the files that come with FXServer are skipped. FiveM license keys, `rcon_password` and Steam Web API keys are detected as secrets.
* **Copies.** Folders that look like copies of each other are flagged, and the guided mode skips them by default.
* The daily check runs without opening a window, also for the `.exe`.
* Backups say which secret files were kept out of the repository.
* Symbols that old Windows console fonts cannot show were replaced by the keepcode square.

## 1.0.0

First release.

* Find projects across the computer and report the ones that are not backed up: no version control, no commits, no remote, uncommitted changes, unpushed commits, stashes and secret files tracked in git.
* `keepcode backup` creates private GitHub repositories, completes the `.gitignore` and saves uncommitted and unpushed work to `keepcode/*` branches without touching files, the staging area or real branches.
* Secret detection for common token formats and secret files.
* Daily check with desktop notifications through the Windows Task Scheduler or crontab, with optional automatic backup.
* Commands: `scan`, `backup`, `check`, `login`, `logout`, `schedule`, `roots`, `ignore`, `config`, `doctor`.
