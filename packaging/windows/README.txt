SubZero Hub for Windows
=======================

Install (on the mini PC)
  1. Right-click SubZeroAI-windows.zip and choose Extract All. The .cmd files don't work from inside the zip.
  2. Double-click setup.cmd and choose Yes when Windows asks for permission.
  3. Paste the Cloudflare Tunnel token when asked (or press Enter to add it later).
  4. Finish on the setup page that opens: http://localhost:8787/setup

Update or repair
  setup.cmd does both. Run it from a newer release's unzipped folder to update to that release, or run
  setup.cmd in C:\Program Files\SubZeroAI to download the newest release. It backs up the data first and
  puts the old version back if the new one doesn't start. Run it from the same version to repair.

The install guide in README.md on the project's GitHub page has every step, including the Cloudflare and
Slack parts. After installing, this folder can be deleted.

Try it first without installing
  Double-click start-test-mode.cmd. It uses sample sign-ins and a pretend Claude, needs no accounts,
  and keeps its data in a test-data folder here. Close the window to stop it.

Where things are after installing
  C:\Program Files\SubZeroAI       the app, its own Python, its own Node.js (the node folder, used by the
                                    AI helper in app\pi_gateway), and these tools:
      setup.cmd                     update to the newest release (or repair), keeping all data
      restore.cmd                   put a backup back
      back-up-now.cmd               make a backup now (one runs every night at 2:00 AM)
      check-settings.cmd            check the Slack and Claude settings
      recovery-code.cmd             make a new recovery code for signing in on this PC
      uninstall.cmd                 remove the app (the data folder is kept)
  C:\ProgramData\SubZeroAI          settings (config.env), chats, attached files, backups, and logs.
                                    Only administrators and the app's service can open it.
  C:\Program Files\cloudflared      the Cloudflare Tunnel connector

Both the SubZero Hub and Cloudflared services start on their own after a restart, with nobody signed in.
