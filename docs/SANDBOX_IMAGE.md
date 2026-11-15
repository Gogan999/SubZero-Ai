# Sandbox image: the Code tools

Status: built 2026-11, tested offline only (fake PowerShell, fake SSH). It has not been run on a real PC. Read "Not yet
proven on a real PC" before the first run.

The Code engine starts `node /opt/subzero-runner/runner.mjs` inside each sandbox. Installing Node.js, Java and the
runner per session would take minutes, so they are baked into the sandbox base disk once. This page covers that step.

## What the step does

Run `prepare-sandbox-image.cmd` (in `C:\Program Files\SubZeroAI`, as an administrator) after `sandbox-image.ps1 -Action
Build` has made the plain base disk and the sandbox network is set up. It calls `python -m subzero_ai sandbox-provision`
(code in `subzero_ai/sandbox_image.py`, the in-VM script in `subzero_ai/sandbox_provision.sh`).

1. Checks that the sandbox network passes its check, that nothing else is running a provisioning step (a lock file in
   `sandbox\provision.lock`), that SubZero AI is stopped, that no sandbox is in use, and that there are 15 GB free next to
   the base disk. A builder VM left by an earlier run that was cut off is deleted first.
2. Makes a temporary builder VM (`szsandbox-build-s<slot>-<random>`) from the current base disk, as a differencing disk,
   with sudo for that VM only. It has its own throwaway SSH key and folders under `sandbox\provision`, so the app's own
   client key and its file permissions are never touched. Memory and CPUs come from the normal sandbox settings.
3. Starts its own internet proxy on the same address and port the app's proxy uses, with one login (for the builder).
   Its allow-list is the normal list plus `azure.archive.ubuntu.com`, `archive.ubuntu.com`, `security.ubuntu.com`,
   `nodejs.org` and `registry.npmjs.org` (`PROVISION_HOSTS`, HTTPS port 443 only). A session's proxy never gets these.
4. Uploads the script and the `sandbox_runner` folder (package.json, package-lock.json, runner.mjs, lib/) and runs
   `provision.sh install` as root over SSH. The output goes to the console and to
   `C:\ProgramData\SubZeroAI\logs\sandbox-image-<date>.log`, with the proxy login replaced by `[hidden]`.
   It installs git, python3 (pip, venv), unzip, zip, curl, ca-certificates, build-essential, OpenJDK 17 (headless), Node.js
   (the pinned linux-x64 tarball, SHA-256 checked before it is unpacked), and the runner (`npm ci --omit=dev
   --ignore-scripts`, owned by root and not writable by the `sandbox` user). It writes
   `/etc/claude-code/managed-settings.json`, git defaults, npm and pip settings, the Gradle and npm cache folders, and
   last of all `/opt/subzero-runner/TOOLS_VERSION`. No proxy login is written to the disk: git, curl, npm, pip and apt read
   `HTTPS_PROXY` from the environment of each command, and Gradle reads `GRADLE_OPTS`/`JAVA_TOOL_OPTIONS`, which the app sets
   per sandbox when it runs a command.
5. Checks inside the VM: `node --version` (the pinned version), `java -version` (17), `git --version`, `python3
   --version`, and the runner's `--version` (the expected runner and Agent SDK versions) plus `TOOLS_VERSION`.
6. Runs `provision.sh finish` (removes the builder's sudo rule, cleans apt, runs `cloud-init clean`, removes the SSH host
   keys, empties `/etc/machine-id`, schedules a power-off), waits until the VM is Off, then `Convert-VHD` turns its
   differencing disk into a standalone dynamic disk next to the old base: `sandbox-base-<TOOLS_VERSION>.vhdx` (a rebuild with
   `--force` adds `-r2`, `-r3`). The disk is made read-only and given the permissions of the old base (SYSTEM and
   Administrators full control, the app's service and `NT VIRTUAL MACHINE\Virtual Machines` read), its SHA-256 goes in
   `<disk>.sha256`, and a record of what it was built from goes in `<disk>.tools.json`.
7. Saves the new path as the `sandbox_base_image` setting (the saved setting in the database wins over `config.env`) and
   writes the audit entries `setting_changed` and `sandbox_image_prepared`. A failure writes `sandbox_image_failed`.
8. Always, also on failure: stops the proxy, forgets the login, deletes the builder VM and its disks and key, and deletes any
   unfinished new disk. The old base disk and the setting stay as they were.

Running it again when the image is current ("current" means the disk's `.tools.json` matches `TOOLS_VERSION`, the Node.js
pin, the runner version and the SDK version) prints "Nothing to do" and finishes at once. `--force` builds again.

Why the app must be stopped: the builder's proxy listens on `10.77.0.1:3128`, the same place the app's proxy does, and the
firewall rules allow only that port. `prepare-sandbox-image.cmd` stops the service first and starts it again at the end,
whatever happens. Running it from a console while the app runs is refused with a plain message. It is also refused while
any sandbox record is still alive, so no running session is touched.

## How long it should take

About 20 to 25 minutes in all: builder boot about 1 to 2 minutes, `apt` and the downloads 5 to 8 minutes, `npm ci` 1 to 2
minutes, shutdown and `Convert-VHD` 2 to 4 minutes, SHA-256 about 1 minute. The new disk should be about 4 to 6 GB on disk
(its virtual size stays 30 GB, under the 31 GB `sandbox_disk_gb` limit).

## What to check afterwards

- The last console line says the image is ready, and the log has no `Stopped while ...` line.
- Settings, Base disk for sandboxes, shows `...\sandbox-base-<version>.vhdx`.
- `sandbox-image.ps1 -Action Check -ParentName sandbox-base-<version>.vhdx` passes (read-only, dynamic, VMs can read it).
- Start one Code session. The sandbox must start (about 70 seconds the first time from a new base), and the session must
  not say the tools are missing. If it does, the message is "The sandbox image doesn't have the Code tools yet. Run
  prepare-sandbox-image.cmd on the server, then try again." (not there) or "...are out of date..." (older version).
- In a session's sandbox, `sudo` must be refused (the builder's rule is removed in the image) and `ssh` must still work.

## Going back to the old disk

Nothing is deleted. In Settings, set Base disk for sandboxes back to the old path (usually
`C:\ProgramData\SubZeroAI\sandbox\images\sandbox-base.vhdx`). New sandboxes then use it; running ones keep the disk they
started from. To delete the new disk once no sandbox uses it, run `sandbox-image.ps1 -Action Remove -ParentName
sandbox-base-<version>.vhdx`, and delete its `.sha256` and `.tools.json` files if they remain. Code sessions need the tools,
so going back also means the Code engine will report "doesn't have the Code tools yet".

If a run is cut off (power loss, closed window), the lock is ignored after 6 hours, and the next run (or the app's own
cleanup at start) deletes a leftover builder VM. An unfinished `sandbox-base-*.building.vhdx` can be deleted by hand.

## When to run it again

`TOOLS_VERSION` in `subzero_ai/sandbox_image.py` is bumped whenever anything baked in changes (the script, the package
list, the Node.js pin, the runner or Agent SDK version). A test fails if the pins change without a bump. After an update that
bumps it, a Code session says the image is out of date, and the admin runs `prepare-sandbox-image.cmd` again.

## Not yet proven on a real PC

Every item here is an assumption nobody has checked on the mini PC.

1. **Ubuntu mirrors over HTTPS through the proxy.** The script rewrites every apt source from `http://` to `https://` and the
   proxy allows only CONNECT on 443. Whether `azure.archive.ubuntu.com`, `archive.ubuntu.com` and `security.ubuntu.com` all
   answer on HTTPS with the paths apt asks for, and whether any of them redirects to a host not on the list, is unchecked.
2. **apt behind a CONNECT proxy with a login.** apt should read `https_proxy` (with `name:token@`) from its environment, and
   keep it when it starts its download helper as `_apt`. Also unchecked: that the ESM/apt-news hook on `apt update` fails
   quietly when `esm.ubuntu.com` is refused.
3. **`sudo -E` keeps the proxy variables.** The builder's sudo rule is `ALL=(ALL) NOPASSWD:ALL` from cloud-init, which should
   allow `-E`. If not, the install fails at its first download and says so.
4. **`Convert-VHD` on a stopped differencing disk.** It should write a standalone dynamic disk that holds the old base's data
   too. The script refuses a result that still has a parent, but whether the result boots has not been tried.
5. **cloud-init on a re-used base.** After `cloud-init clean --logs --seed --configs network` the next boot, with a new seed and
   a new instance-id, should take the new user-data (keys, host key, sysctl file, static address). If it doesn't, new
   sandboxes won't answer SSH and the sandbox start fails with "didn't start in time". Also unchecked: that no old
   `/etc/netplan` file or machine-id survives, and that the Azure agent stays masked.
6. **GRUB still finds the right kernel.** The boot menu of the old base picks the last `/vmlinuz-*` by name. The script fails
   the install if a new kernel appears, but the new disk itself is not boot-tested by the step. A session start is the test.
7. **Disk size.** The root filesystem has to hold JDK 17, build-essential, Node.js and the runner with room for a student's
   Gradle caches. About 3 to 4 GB for the tools is a guess; the 30 GB virtual disk and the 8 GB `sandbox_disk_cap_gb` per
   sandbox are unchanged.
8. **The Linux native package.** `npm ci --omit=dev --ignore-scripts` is expected to install
   `@anthropic-ai/claude-agent-sdk-linux-x64` (the script fails if the folder is missing). Whether the SDK works without its
   install scripts, and that `claude` inside it runs on this image's libc, is untested.
9. **The `sandbox` user's home and uid** exist in the builder as in any session VM (the script fails if the user is missing).
   The cache folders are made owned by that user and must survive in the new base.
10. **Runtime proxy settings.** git and curl read `https_proxy`; npm and pip read it too; Gradle gets it from `GRADLE_OPTS`.
    That all of them work with the JDK's Basic-for-tunnels override and the per-sandbox login is the same open point as in
    `docs/CODE_SANDBOX_PLAN.md` (M5).
11. **`pip` with `break-system-packages = true`** is set so `pip install` works in the disposable VM (Ubuntu 24.04 otherwise
    refuses). Whether that suits how the Code engine installs packages is a choice to confirm.
12. **Managed settings.** `cleanupPeriodDays: 1` and the four `env` flags are the values the plan asked for; whether this
    Agent SDK version reads `/etc/claude-code/managed-settings.json` on Linux with exactly these keys is unchecked.
13. **Time and size limits.** The 3-hour proxy login, the 4 GB proxy byte allowance for the builder, and the 1-hour limit for the
    install step are guesses.
14. **The Windows wrapper.** `prepare-sandbox-image.ps1` stopping and starting the service, and `ssh.exe`/`ssh-keygen.exe`
    working for an administrator account (not the service account), follow the other scripts but have not been run.
