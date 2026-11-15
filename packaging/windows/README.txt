SubZero AI for Windows
======================

Install (on the mini PC)
  1. Right-click SubZeroAI-windows.zip and choose Extract All. The .cmd files don't work from inside the zip.
  2. Double-click install.cmd and choose Yes when Windows asks for permission.
  3. Paste the Cloudflare Tunnel token when asked (or press Enter to add it later).
  4. Finish on the setup page that opens: http://localhost:8787/setup

The install guide in README.md on the project's GitHub page has every step, including the Cloudflare and
Slack parts. After installing, this folder can be deleted.

Try it first without installing
  Double-click start-test-mode.cmd. It uses sample sign-ins and a pretend Claude, needs no accounts,
  and keeps its data in a test-data folder here. Close the window to stop it.

Where things are after installing
  C:\Program Files\SubZeroAI       the app, its own Python, its own Node.js (the node folder, used by the
                                    AI helper in app\pi_gateway), and these tools:
      update.cmd                    update to the newest release, keeping all data
      restore.cmd                   put a backup back
      back-up-now.cmd               make a backup now (one runs every night at 2:00 AM)
      check-settings.cmd            check the Slack and Claude settings
      prepare-sandbox-image.cmd     put the Code tools into the sandbox's base disk (once, about 20 minutes; it
                                    stops SubZero AI while it runs, so do it when no one is using Code)
      recovery-code.cmd             make a new recovery code for signing in on this PC
      uninstall.cmd                 remove the app (the data folder is kept)
  C:\ProgramData\SubZeroAI          settings (config.env), chats, attached files, backups, and logs.
                                    Only administrators and the app's service can open it.
  C:\Program Files\cloudflared      the Cloudflare Tunnel connector

Code sandboxes (optional, needs Hyper-V)
  Updating from 0.10: run scripts\sandbox-network.ps1 -Action Install once more in an administrator
  PowerShell here (it opens the Code gateway port 3129), then prepare-sandbox-image.cmd. The steps are in
  README.md under "Updating from 0.10".
  Once the plain base disk is built, double-click prepare-sandbox-image.cmd once. It adds
  Node.js, Java, git and Python to a new base disk and switches the setting to it. Run it again after an update
  only if it says the tools are out of date. The old disk is kept. Details: docs\SANDBOX_IMAGE.md on GitHub.

Both the SubZero AI and Cloudflared services start on their own after a restart, with nobody signed in.
