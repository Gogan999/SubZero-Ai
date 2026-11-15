#!/bin/bash
# Puts the Code tools into the sandbox base image. It runs ONCE, as root, inside a temporary builder VM that
# `python -m subzero_ai sandbox-provision` makes from the current base disk (see subzero_ai/sandbox_image.py and
# docs/SANDBOX_IMAGE.md). It is never run in a session's sandbox.
#
#   bash sandbox_provision.sh install   installs the tools and the runner, writes their settings
#   bash sandbox_provision.sh finish    cleans the machine so the next boot from the new base disk is a fresh boot, and
#                                       schedules a power-off
#
# The caller passes these through the environment: TOOLS_VERSION, NODE_VERSION, NODE_SHA256 (all defined once, in
# sandbox_image.py) and HTTPS_PROXY / https_proxy (the builder's own proxy login, which exists only for this run).
# The proxy login is read from the environment when the commands run. It is never written to a file, never put on a
# command line and never echoed: do not add `set -x` here. The runner folder is uploaded next to this file, in ./runner.

set -euo pipefail
umask 022
trap 'echo "The step that failed is near line $LINENO of the provisioning script." >&2' ERR

MODE="${1:-install}"
export HOME=/root
export DEBIAN_FRONTEND=noninteractive
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUEST_USER=sandbox
RUNNER_DIR=/opt/subzero-runner
NODE_ROOT=/opt/node
APT_OPTS=(-o Acquire::Retries=3 -o Acquire::ForceIPv4=true -o Dpkg::Options::=--force-confold)

say() { echo "== $*"; }

need_env() {
  local name
  for name in "$@"; do
    if [ -z "${!name:-}" ]; then
      echo "The provisioning script needs $name in its environment." >&2
      exit 2
    fi
  done
}

kernels() { ls /boot/vmlinuz-* 2>/dev/null | sort || true; }

# The proxy takes secure (HTTPS) connections only, so every apt source is switched to https:// first.
use_https_sources() {
  local file
  for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do
    if [ -f "$file" ]; then
      sed -i 's#http://#https://#g' "$file"
    fi
  done
}

install_packages() {
  say "Installing packages (git, Python, Java 17, build tools)"
  use_https_sources
  apt-get "${APT_OPTS[@]}" update
  apt-get "${APT_OPTS[@]}" install -y --no-install-recommends \
    git python3 python3-pip python3-venv unzip zip curl ca-certificates build-essential openjdk-17-jdk-headless
}

install_node() {
  say "Installing Node.js ${NODE_VERSION}"
  local tmp tarball dir
  tmp="$(mktemp -d)"
  tarball="$tmp/node.tar.gz"
  dir="node-v${NODE_VERSION}-linux-x64"
  curl -fsSL --retry 3 -o "$tarball" "https://nodejs.org/dist/v${NODE_VERSION}/${dir}.tar.gz"
  echo "${NODE_SHA256}  ${tarball}" | sha256sum -c - >/dev/null || {
    echo "The Node.js download doesn't match its pinned SHA-256, so it was not unpacked." >&2
    exit 1
  }
  rm -rf "/opt/${dir}"
  tar -xzf "$tarball" -C /opt --no-same-owner
  ln -sfn "/opt/${dir}" "$NODE_ROOT"
  ln -sf "$NODE_ROOT/bin/node" /usr/local/bin/node
  ln -sf "$NODE_ROOT/bin/npm" /usr/local/bin/npm
  ln -sf "$NODE_ROOT/bin/npx" /usr/local/bin/npx
  rm -rf "$tmp"
}

install_runner() {
  say "Installing the Code runner into ${RUNNER_DIR}"
  rm -rf "$RUNNER_DIR"
  mkdir -p "$RUNNER_DIR"
  cp -a "$HERE/runner/." "$RUNNER_DIR/"
  # The lock file's integrity hashes are the pin. --ignore-scripts: nothing the packages ship is run as root.
  (cd "$RUNNER_DIR" && npm ci --omit=dev --ignore-scripts --no-audit --no-fund)
  if [ ! -d "$RUNNER_DIR/node_modules/@anthropic-ai/claude-agent-sdk-linux-x64" ]; then
    echo "npm didn't install the Linux program of the Agent SDK (claude-agent-sdk-linux-x64)." >&2
    exit 1
  fi
  # Owned by root and not writable by the sandbox user, so a session can't change the runner for the next one
  chown -R root:root "$RUNNER_DIR"
  chmod -R go-w "$RUNNER_DIR"
  chmod -R a+rX "$RUNNER_DIR"
}

write_settings() {
  say "Writing the tools' settings"
  # Claude's managed settings: nothing is denied (the sandbox itself is the boundary), and every kind of
  # background traffic is off, because the proxy only lets the model API and the package sites through.
  mkdir -p /etc/claude-code
  cat > /etc/claude-code/managed-settings.json <<'JSON'
{
  "permissions": {
    "deny": []
  },
  "env": {
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
  },
  "allowManagedHooksOnly": true,
  "cleanupPeriodDays": 1
}
JSON
  chown root:root /etc/claude-code/managed-settings.json
  chmod 0644 /etc/claude-code/managed-settings.json

  git config --system --replace-all safe.directory '*'
  git config --system user.name 'Code'
  git config --system user.email 'code@localhost'
  git config --system init.defaultBranch main

  # Proxy settings are NOT stored here. git, curl, npm, pip and apt read HTTPS_PROXY from the environment of each
  # command, and Gradle reads GRADLE_OPTS / JAVA_TOOL_OPTIONS from it. The host sets those per sandbox, per run.
  mkdir -p "$NODE_ROOT/etc"
  printf '%s\n' 'fund=false' 'audit=false' 'update-notifier=false' > "$NODE_ROOT/etc/npmrc"
  printf '%s\n' '[global]' 'break-system-packages = true' 'disable-pip-version-check = true' > /etc/pip.conf

  local home
  home="$(getent passwd "$GUEST_USER" | cut -d: -f6)"
  if [ -z "$home" ]; then
    echo "The base image has no ${GUEST_USER} user, so the cache folders can't be made." >&2
    exit 1
  fi
  install -d -o "$GUEST_USER" -g "$GUEST_USER" -m 0755 "$home/.gradle" "$home/.npm" "$home/.cache"
  printf '%s\n' 'org.gradle.daemon=false' 'org.gradle.console=plain' > "$home/.gradle/gradle.properties"
  chown "$GUEST_USER:$GUEST_USER" "$home/.gradle/gradle.properties"
}

do_install() {
  need_env TOOLS_VERSION NODE_VERSION NODE_SHA256
  if [ -z "${https_proxy:-${HTTPS_PROXY:-}}" ]; then
    echo "There is no proxy in the environment, so nothing can be downloaded." >&2
    exit 2
  fi
  local before after
  before="$(kernels)"
  install_packages
  install_node
  install_runner
  write_settings
  after="$(kernels)"
  if [ "$before" != "$after" ]; then
    # The base disk's boot menu picks the last kernel by name, and that was only proven for the original one
    echo "A new kernel was installed, which the sandbox boot menu wasn't tested with. Nothing was kept." >&2
    exit 1
  fi
  # Written last: its presence means everything above finished
  printf '%s\n' "$TOOLS_VERSION" > "$RUNNER_DIR/TOOLS_VERSION"
  chmod 0644 "$RUNNER_DIR/TOOLS_VERSION"
  say "Install finished"
}

do_finish() {
  say "Cleaning the machine for the new base disk"
  apt-get clean
  rm -rf /var/lib/apt/lists/* /root/.npm /root/.cache /root/.bash_history
  # The builder's own sudo rights come from cloud-init and must not be in the image: a session's sandbox has no sudo
  rm -f /etc/sudoers.d/90-cloud-init-users
  if grep -rqsE "^[[:space:]]*${GUEST_USER}[[:space:]]" /etc/sudoers /etc/sudoers.d; then
    echo "The ${GUEST_USER} user still has a sudo rule, so the image was not finished." >&2
    exit 1
  fi
  if id -nG "$GUEST_USER" | tr ' ' '\n' | grep -qxE 'sudo|admin|root'; then
    echo "The ${GUEST_USER} user is in a group with administrator rights, so the image was not finished." >&2
    exit 1
  fi
  # Make the next boot a fresh first boot: a new seed gets a new instance, new host keys and a new network config
  cloud-init clean --logs --seed --configs network || cloud-init clean --logs --seed || true
  rm -f /etc/netplan/50-cloud-init.yaml /etc/ssh/ssh_host_*
  truncate -s 0 /etc/machine-id
  rm -f /var/lib/dbus/machine-id
  local home
  home="$(getent passwd "$GUEST_USER" | cut -d: -f6)"
  rm -rf "$home/.ssh" "$home/.bash_history" "$HERE" /root/.ssh
  find /var/log -type f -name '*.log' -exec truncate -s 0 {} + 2>/dev/null || true
  truncate -s 0 /var/log/wtmp /var/log/btmp /var/log/lastlog 2>/dev/null || true
  journalctl --rotate >/dev/null 2>&1 || true
  journalctl --vacuum-time=1s >/dev/null 2>&1 || true
  rm -rf /var/log/journal/* /tmp/* /var/tmp/* 2>/dev/null || true
  sync
  # Power off a moment after this command returns, so the connection ends cleanly first
  nohup sh -c 'sleep 3; systemctl poweroff' >/dev/null 2>&1 &
  say "Power-off scheduled"
}

case "$MODE" in
  install) do_install ;;
  finish) do_finish ;;
  *) echo "Unknown mode: $MODE (use install or finish)" >&2; exit 2 ;;
esac
