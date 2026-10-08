#!/bin/sh
# LINKBOX bootstrap: Debian / Ubuntu / Alpine.
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
printf '\n  LINKBOX 1.0.2\n  正在准备…\n'
case "$ID" in
    alpine) quiet apk add --no-cache python3 curl ca-certificates ;;
    *) quiet env DEBIAN_FRONTEND=noninteractive apt-get update
       if ! command -v wget >/dev/null 2>&1 && ! command -v curl >/dev/null 2>&1; then
           quiet env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends wget ca-certificates
       fi
       quiet env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 curl ca-certificates ;;
esac
# This pinned digest is updated together with the manager in each release.
MANAGER_SHA=c8d6abfbe2b923e33c097d1da3fe24a9141ccefa6fa8bf795d0b7cd06fa56d11
MANAGER_URL=https://raw.githubusercontent.com/aoomee/linkbox/0b172203d983895c078cdbf37ab5ceb6648fff94/linkbox.py
download_manager() {
    attempt=0
    for mode in auto http1 ipv4 ipv6; do
        attempt=$((attempt + 1))
        case "$mode" in
            auto) set -- ;;
            http1) set -- --http1.1 ;;
            ipv4) set -- --http1.1 -4 ;;
            ipv6) set -- --http1.1 -6 ;;
        esac
        if [ "$attempt" -gt 1 ]; then
            printf '  下载重试 %s/4 · %s\n' "$attempt" "$mode"
            sleep 1
        fi
        rm -f "$TASK_TMP/linkbox.py"
        if curl -q -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 90 \
            "$@" "$MANAGER_URL" -o "$TASK_TMP/linkbox.py" < /dev/null 2> "$TASK_TMP/download.log"; then
            return 0
        else
            download_code=$?
        fi
        printf '\n  %s · curl %s\n' "$mode" "$download_code" >> "$TASK_TMP/download-errors.log"
        cat "$TASK_TMP/download.log" >> "$TASK_TMP/download-errors.log"
        case "$download_code" in 23|26|60|77) break ;; esac
    done
    rm -f "$TASK_TMP/linkbox.py"
    cat "$TASK_TMP/download-errors.log" >&2
    fail '管理器下载失败；请检查到 raw.githubusercontent.com 的连接后重试。'
}
download_manager
printf '%s  %s\n' "$MANAGER_SHA" "$TASK_TMP/linkbox.py" > "$TASK_TMP/checksums"
quiet sha256sum -c "$TASK_TMP/checksums"
python3 "$TASK_TMP/linkbox.py" --bootstrap "$TASK_TMP" < /dev/null
rm -rf "$TASK_TMP"
trap - EXIT INT TERM
exec /usr/local/bin/lb <&3
