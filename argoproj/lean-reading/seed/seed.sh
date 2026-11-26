#!/bin/sh
# BOXP-679: code-server の initContainer が起動のたびに実行する。
# 目的: 永続ホーム（PVC）に Lean の道具・VS Code 拡張・本のコード・練習用プロジェクトを用意する。
# 規則: 既にあるファイルは上書きしない（本人の書いたものを壊さない）。足りないものだけ足す。
# 入力（環境変数）:
#   HOME         永続ホーム（既定 /home/coder）
#   SEED_DIR     この script と雛形の置き場所（ConfigMap、読み取り専用）
#   SKIP_EXTENSION=1  拡張の導入を飛ばす（code-server が無い環境での試験用）
#   SKIP_BOOK_BUILD=1 本のコードの事前 build を飛ばす
set -eu

ELAN_VERSION="v4.4.0"
ELAN_SHA256_X86_64="0e8250aa2be86c7af929ade68db617af9edc8b9ccf6eaf5ece228250d0d032e2"
LEAN_TOOLCHAIN="leanprover/lean4:v4.35.0"
LEAN_EXTENSION="leanprover.lean4@0.0.243"
# Vim 操作。ブラウザ側（web 拡張）だけに入れると .lean で効かなかったため、
# サーバー側に入れておく（BOXP-679 TD-01）。
VIM_EXTENSION="vscodevim.vim@1.32.4"
BOOK_REPO="https://github.com/y-taka-23/lean-verified-compiler.git"

SEED_DIR="${SEED_DIR:-/seed}"
WS="$HOME/workspace"
ELAN="$HOME/.elan/bin/elan"

log() { printf '[seed] %s\n' "$*"; }

# 雛形を置く。既にあれば何もしない。
place() {
  src="$SEED_DIR/$1"
  dst="$2"
  if [ -e "$dst" ]; then
    log "keep  $dst"
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  log "place $dst"
}

install_elan() {
  if [ -x "$ELAN" ]; then
    log "elan already installed: $("$ELAN" --version)"
    return 0
  fi
  arch="$(uname -m)"
  if [ "$arch" != "x86_64" ]; then
    log "unsupported arch for pinned elan: $arch" >&2
    exit 1
  fi
  tmp="$(mktemp -d)"
  url="https://github.com/leanprover/elan/releases/download/${ELAN_VERSION}/elan-x86_64-unknown-linux-gnu.tar.gz"
  log "download $url"
  curl -fsSL --retry 3 -o "$tmp/elan.tar.gz" "$url"
  echo "$ELAN_SHA256_X86_64  $tmp/elan.tar.gz" | sha256sum -c -
  tar -xzf "$tmp/elan.tar.gz" -C "$tmp"
  "$tmp/elan-init" -y --no-modify-path --default-toolchain none
  rm -rf "$tmp"
}

mkdir -p "$WS"
install_elan
log "toolchain $LEAN_TOOLCHAIN"
"$ELAN" toolchain install "$LEAN_TOOLCHAIN" 2>&1 | tail -n 3
"$ELAN" default "$LEAN_TOOLCHAIN"

# 練習用（本人が書く場所）と案内。
place practice/lean-toolchain "$WS/practice/lean-toolchain"
place practice/lakefile.toml "$WS/practice/lakefile.toml"
place practice/Practice.lean "$WS/practice/Practice.lean"
place practice/Practice/Basics.lean "$WS/practice/Practice/Basics.lean"
place practice/gitignore "$WS/practice/.gitignore"
place README.md "$WS/README.md"
place reading.code-workspace "$WS/reading.code-workspace"
place settings.json "$HOME/.local/share/code-server/User/settings.json"

# 本のサポートコード（完成コード。読むための参照用）。
if [ ! -d "$WS/lean-verified-compiler/.git" ]; then
  log "clone $BOOK_REPO"
  git clone --quiet "$BOOK_REPO" "$WS/lean-verified-compiler"
else
  log "keep  $WS/lean-verified-compiler"
fi

# 拡張を入れる。既に入っていれば版も含めて触らない（本人が更新・無効化したものを戻さない）。
ensure_extension() {
  spec="$1"
  id="${spec%@*}"
  if code-server --list-extensions 2>/dev/null | grep -qx "$id"; then
    log "keep  extension $id"
    return 0
  fi
  log "install extension $spec"
  code-server --install-extension "$spec"
}

if [ "${SKIP_EXTENSION:-0}" != "1" ]; then
  ensure_extension "$LEAN_EXTENSION"
  # Vim は無くても Lean は使えるので、失敗しても起動は止めない。
  ensure_extension "$VIM_EXTENSION" || log "vim extension install failed (continue)"
fi

# 初回の build。失敗しても起動は止めない（InfoView を開けば拡張がやり直す）。
export PATH="$HOME/.elan/bin:$PATH"
if [ ! -d "$WS/practice/.lake/build" ]; then
  log "build practice"
  (cd "$WS/practice" && lake build) || log "practice build failed (continue)"
fi
if [ "${SKIP_BOOK_BUILD:-0}" != "1" ] && [ ! -d "$WS/lean-verified-compiler/.lake/build" ]; then
  log "build book code"
  (cd "$WS/lean-verified-compiler" && lake build) || log "book build failed (continue)"
fi
log "done"
