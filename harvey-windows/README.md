# Harvey for Windows: one-click install

Installs the open-source [Harvey](https://github.com/ethanplusai/harvey) sales agent for your Windows user. No Python or coding knowledge needed.

1. Download `Install-Harvey.bat` from the latest "Harvey installer" release.
2. Double-click it. If Windows warns, click **More info**, then **Run anyway**.
3. Follow the questions. It installs Python if needed, installs Harvey, helps you log in to Claude, and puts **Harvey** and **Harvey setup** on your Desktop.
4. Double-click **Harvey** to open the dashboard in your browser.

Everything lives in `%LOCALAPPDATA%\Harvey`. Running the installer again keeps your settings and data. To remove Harvey, delete that folder and the two Desktop shortcuts.

You still need: a Claude Pro or Max plan, and a mailbox to send from (a separate sending domain, warmed up, is strongly advised). Read the "Legal and deliverability" section of Harvey's README before your first campaign. Harvey never sends without your approval unless you switch that off.
The installer is pinned to a checked version of Harvey, so a later upstream change cannot alter what you install.

## Troubleshooting
**"Error running 'harvey status': duplicate column name: ..."** Two Harvey processes started at the same moment on a brand-new database and left it half-upgraded (a bug in Harvey's start-up). If you have no data you need yet, close Harvey, delete the folder `%LOCALAPPDATA%\Harvey\app\data`, and start Harvey once. The installer now prepares the database once to avoid this.

**"claude is not recognized".** Claude Code is installed in `%USERPROFILE%\.local\bin`. Close and reopen PowerShell after installing it. The Claude app and claude.ai in a browser are a different thing; Harvey needs Claude Code.
