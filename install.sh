#!/bin/sh
# LinkBox bootstrap: Debian / Ubuntu / Alpine.
set -eu
umask 077
fail() { printf '\n  错误 · %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail '请用 root 运行。'
[ -r /etc/os-release ] || fail '无法识别系统。'
. /etc/os-release
case "${ID:-}" in
    alpine) [ -x /sbin/openrc-run ] || fail '需要完整的 OpenRC 系统。' ;;
    debian|ubuntu) [ -d /run/systemd/system ] || fail '需要运行中的 systemd，普通容器不适用。' ;;
    *) fail '支持 Debian、Ubuntu、Alpine。' ;;
esac
{ exec 3</dev/tty; } 2>/dev/null || fail '请在交互式 SSH 终端运行。'
TASK_TMP=$(mktemp -d)
trap 'rm -rf "$TASK_TMP"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
quiet() {
    if "$@" < /dev/null > "$TASK_TMP/install.log" 2>&1; then return 0; fi
    tail -n 8 "$TASK_TMP/install.log" >&2
    fail '安装未完成，原因见上方。'
}
printf '\n  LinkBox\n  正在准备…\n'
case "$ID" in
    alpine) quiet apk add --no-cache python3 curl ca-certificates ;;
    *) quiet env DEBIAN_FRONTEND=noninteractive apt-get update
       quiet env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 curl ca-certificates ;;
esac
# This pinned digest is updated together with the manager in each release.
MANAGER_SHA=e82229cf5f212ab06b9af9182ccec067539ee7c7151d5b0a69872ab0652b39b6
quiet curl -fsSL --retry 2 --connect-timeout 15 --max-time 90 https://raw.githubusercontent.com/aoomee/linkbox/main/linkbox.py -o "$TASK_TMP/linkbox.py"
printf '%s  %s\n' "$MANAGER_SHA" "$TASK_TMP/linkbox.py" > "$TASK_TMP/checksums"
quiet sha256sum -c "$TASK_TMP/checksums"
python3 "$TASK_TMP/linkbox.py" --bootstrap "$TASK_TMP" < /dev/null
rm -rf "$TASK_TMP"
trap - EXIT INT TERM
exec /usr/local/bin/lb <&3
