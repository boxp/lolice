#!/usr/bin/env bash
# .regomark/tools.env の版を SHA-256 検証つきで取得し、bin ディレクトリを標準出力に返す。
# --refresh は tools.env の SHA-256 を上流 release の checksum から書き直す。
set -euo pipefail

root=$(git rev-parse --show-toplevel)
tools_env="$root/.regomark/tools.env"
# shellcheck source=/dev/null
. "$tools_env"

case "$(uname -m)" in
x86_64 | amd64) regomark_arch=amd64 opa_arch=amd64 regal_arch=x86_64 ;;
aarch64 | arm64) regomark_arch=arm64 opa_arch=arm64 regal_arch=arm64 ;;
*)
	echo "未対応の CPU です: $(uname -m)" >&2
	exit 2
	;;
esac

regomark_base="https://github.com/babarot/regomark/releases/download/v$REGOMARK_VERSION"
opa_base="https://github.com/open-policy-agent/opa/releases/download/v$OPA_VERSION"
regal_base="https://github.com/open-policy-agent/regal/releases/download/v$REGAL_VERSION"

fetch() { curl -fsSL --retry 3 --max-time 180 -o "$2" "$1"; }

if [ "${1:-}" = "--refresh" ]; then
	work=$(mktemp -d)
	trap 'rm -rf "$work"' EXIT
	fetch "$regomark_base/regomark_${REGOMARK_VERSION}_checksums.txt" "$work/regomark.txt"
	fetch "$regal_base/checksums.txt" "$work/regal.txt"
	fetch "$opa_base/opa_linux_amd64_static.sha256" "$work/opa-amd64.txt"
	fetch "$opa_base/opa_linux_arm64_static.sha256" "$work/opa-arm64.txt"
	sum_of() { awk -v name="$2" '$2 == name || $2 == "*" name { print $1 }' "$1"; }
	set_sum() { sed -i "s/^$1=.*/$1=$2/" "$tools_env"; }
	set_sum REGOMARK_SHA256_AMD64 "$(sum_of "$work/regomark.txt" regomark_linux_amd64.tar.gz)"
	set_sum REGOMARK_SHA256_ARM64 "$(sum_of "$work/regomark.txt" regomark_linux_arm64.tar.gz)"
	set_sum REGAL_SHA256_AMD64 "$(sum_of "$work/regal.txt" regal_Linux_x86_64)"
	set_sum REGAL_SHA256_ARM64 "$(sum_of "$work/regal.txt" regal_Linux_arm64)"
	set_sum OPA_SHA256_AMD64 "$(awk '{ print $1 }' "$work/opa-amd64.txt")"
	set_sum OPA_SHA256_ARM64 "$(awk '{ print $1 }' "$work/opa-arm64.txt")"
	exit 0
fi

if [ "$regomark_arch" = amd64 ]; then
	regomark_sum=$REGOMARK_SHA256_AMD64 opa_sum=$OPA_SHA256_AMD64 regal_sum=$REGAL_SHA256_AMD64
else
	regomark_sum=$REGOMARK_SHA256_ARM64 opa_sum=$OPA_SHA256_ARM64 regal_sum=$REGAL_SHA256_ARM64
fi

bin="${XDG_CACHE_HOME:-$HOME/.cache}/regomark-tools/$(sha256sum "$tools_env" | cut -c1-16)-$regomark_arch"
mkdir -p "$bin"

install_raw() {
	if [ ! -x "$bin/$1" ]; then
		fetch "$2" "$bin/$1.tmp"
		echo "$3  $bin/$1.tmp" | sha256sum -c --quiet - >&2
		chmod 755 "$bin/$1.tmp"
		mv "$bin/$1.tmp" "$bin/$1"
	fi
}

if [ ! -x "$bin/regomark" ]; then
	archive=$(mktemp)
	fetch "$regomark_base/regomark_linux_$regomark_arch.tar.gz" "$archive"
	echo "$regomark_sum  $archive" | sha256sum -c --quiet - >&2
	tar -xzf "$archive" -C "$bin" regomark
	rm -f "$archive"
fi
install_raw opa "$opa_base/opa_linux_${opa_arch}_static" "$opa_sum"
install_raw regal "$regal_base/regal_Linux_$regal_arch" "$regal_sum"

echo "$bin"
