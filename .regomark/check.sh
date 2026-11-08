#!/usr/bin/env bash
# ADR の検査と Rego lint の実行入口。説明: .regomark/README.md
set -euo pipefail

root=$(git rev-parse --show-toplevel)
cd "$root"

manifest=.regomark/shared.sha256
canonical_manifest_url=https://raw.githubusercontent.com/boxp/arch/main/.regomark/shared.sha256

shared_files() {
	{
		printf '%s\n' .regomark.yaml docs/adr/README.md docs/adr/template.md .claude/rules/adr.md
		git ls-files --cached --others --exclude-standard -- .regomark |
			grep -vE '^\.regomark/(shared\.sha256|tools\.env)$'
	} | LC_ALL=C sort -u
}

case "${1:-}" in
"") mode=all ;;
--docs) mode=docs ;;
--judge) mode=judge ;;
--questions) mode=questions ;;
--judgments) mode=judgments ;;
--update-manifest)
	shared_files | xargs sha256sum >"$manifest"
	exit 0
	;;
*)
	echo "使い方: .regomark/check.sh [--docs | --judge | --questions FILE | --judgments FILE | --update-manifest]" >&2
	exit 2
	;;
esac

# aqua の proxy は実行時の cwd から設定を探すため、一時 repo でも動く実体の path に解決する。
if [ -f .regomark/tools.env ]; then
	PATH="$(.regomark/install-tools.sh):$PATH"
	regomark=$(command -v regomark)
	opa=$(command -v opa)
	regal=$(command -v regal)
elif command -v aqua >/dev/null 2>&1 && [ -f aqua/aqua.yaml ]; then
	export AQUA_POLICY_CONFIG="$root/aqua/aqua-policy.yaml"
	# aqua which は導入しないので、先に aqua exec で導入する。
	aqua exec -- regomark --version >/dev/null
	aqua exec -- opa version >/dev/null
	aqua exec -- regal version >/dev/null
	regomark=$(aqua which regomark)
	opa=$(aqua which opa)
	regal=$(aqua which regal)
else
	regomark=$(command -v regomark)
	opa=$(command -v opa)
	regal=$(command -v regal)
fi

format=text
if [ "${GITHUB_ACTIONS:-}" = true ]; then
	format=github
fi

case "$mode" in
docs)
	"$regomark" check --format "$format" --no-judge
	echo "意味検査: 未実行 (--no-judge)。未実行は通過を意味しません"
	exit 0
	;;
judge)
	"$regomark" check --format "$format" --judge
	exit 0
	;;
questions)
	"$regomark" questions >"$2"
	asked=$(jq '.questions | length' "$2")
	limit=$(sed -n 's/^  max_questions: //p' .regomark.yaml)
	if [ "$asked" -gt "$limit" ]; then
		echo "質問 $asked 件が judge.max_questions ($limit) を超えています" >&2
		exit 2
	fi
	exit 0
	;;
judgments)
	"$regomark" check --format "$format" --judgments "$2"
	exit 0
	;;
esac

echo "== 版"
# head はパイプを途中で閉じ、pipefail 下で書込み側が SIGPIPE (exit 141) になり得るため、全行を読む sed で先頭行だけ出す。
"$regomark" --version
"$opa" version | sed -n 1p
"$regal" version | sed -n 1p

echo "== 同梱内容と一覧の一致"
diff <(shared_files) <(awk '{ print $2 }' "$manifest" | LC_ALL=C sort -u)
sha256sum --check --quiet "$manifest"
if [ -f .regomark/tools.env ]; then
	if canonical=$(curl -fsS --max-time 10 "$canonical_manifest_url" 2>/dev/null); then
		if [ "$canonical" != "$(cat "$manifest")" ]; then
			echo "::warning::.regomark の同梱内容が boxp/arch の main と異なります。正本に揃えてください"
		fi
	else
		echo "boxp/arch の正本を取得できないため差分確認を省略しました"
	fi
fi

echo "== opa check --strict"
"$opa" check --strict .regomark/policy
echo "== opa fmt"
"$opa" fmt --list --fail .regomark/policy
echo "== opa test"
"$opa" test .regomark/policy
echo "== regal lint"
"$regal" lint --config-file .regomark/.regal/config.yaml --fail-level warning .regomark/policy

echo "== regomark check --no-judge"
"$regomark" check --format "$format" --no-judge
echo "意味検査: 未実行 (--no-judge)。未実行は通過を意味しません"

echo "== regomark e2e"
.regomark/e2e/run.sh "$regomark"
