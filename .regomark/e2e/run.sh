#!/usr/bin/env bash
# regomark の実バイナリで policy を終端まで検査する。使い方: run.sh <regomark のパス>
# 各ケースは一時 git repo を作り、終了コードと rule の集合を完全一致で比較する。
set -uo pipefail

regomark=$1
source_root=$(git rev-parse --show-toplevel)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
failures=0
passes=0

new_repo() {
	repo="$work/repo"
	rm -rf "$repo"
	mkdir -p "$repo/docs/adr" "$repo/.regomark"
	cp -R "$source_root/.regomark/policy" "$repo/.regomark/policy"
	cp "$source_root/.regomark.yaml" "$repo/.regomark.yaml"
	git -C "$repo" init --quiet
}

# adr <番号> <Status 先頭段落> [Decision 本文]
adr() {
	printf '# ADR-%s: 例\n\n## Status\n\n%b\n\n## Context\n\nCONTEXT-MARKER\n\n## Decision\n\n%s\n\n## Consequences\n\n帰結\n' \
		"$1" "$2" "${3:-決定}" >"$repo/docs/adr/$1-example.md"
}

run_regomark() {
	(cd "$repo" && "$regomark" check --format json "$@") >"$work/out" 2>"$work/err"
	actual_exit=$?
}

# expect <ケース名> <終了コード> <severity:rule をカンマ区切り。なければ空>
expect() {
	local name=$1 want_exit=$2 want_rules=$3 got_rules=""
	if [ "$actual_exit" -ne 2 ]; then
		if ! got_rules=$(jq -r '[.[] | "\(.severity):\(.rule)"] | unique | join(",")' "$work/out" 2>/dev/null); then
			got_rules="<json を解析できません>"
		fi
	fi
	if [ "$actual_exit" -eq "$want_exit" ] && [ "$got_rules" = "$want_rules" ]; then
		passes=$((passes + 1))
		echo "ok   $name"
	else
		failures=$((failures + 1))
		echo "FAIL $name: exit $actual_exit (期待 $want_exit), rules [$got_rules] (期待 [$want_rules])"
		sed 's/^/     /' "$work/err" | head -n 5
	fi
}

# deny_case <rule> <red の準備関数> : red は rule の deny だけで exit 1。その rule を skip すると exit 0。
# 後者で、他の rule が同時に発火して偶然 red になっているケースを排除する。
deny_case() {
	local rule=$1 setup=$2
	new_repo
	"$setup"
	run_regomark --no-judge
	expect "red:$rule" 1 "deny:$rule"
	sed -i "s/^skip_rules: \[\]/skip_rules: [$rule]/" "$repo/.regomark.yaml"
	run_regomark --no-judge
	expect "isolated:$rule" 0 ""
}

### 正常系 (green)
new_repo
adr 001 'Accepted'
touch "$repo/guard.txt"
printf '\n[guard](../../guard.txt) [dir](../adr) [web](https://example.com) [self](#status)\n' >>"$repo/docs/adr/001-example.md"
run_regomark --no-judge
expect "green:link-resolves" 0 ""

new_repo
adr 001 'Superseded by ADR-002'
adr 002 'Accepted\nPartly replaced by ADR-003'
adr 003 'Deprecated'
adr 004 'Rejected'
run_regomark --no-judge
expect "green:all-status-values" 0 ""

new_repo
adr 001 'Accepted'
cp "$source_root/docs/adr/README.md" "$source_root/docs/adr/template.md" "$repo/docs/adr/"
run_regomark --no-judge
expect "green:readme-and-template-excluded" 0 ""

new_repo
sed 's/ADR-NNN/ADR-001/' "$source_root/docs/adr/template.md" >"$repo/docs/adr/001-from-template.md"
run_regomark --no-judge
expect "green:template-is-valid-adr" 0 ""

new_repo
adr 001 'Deprecated'
printf '\n[gone](../../gone.txt)\n' >>"$repo/docs/adr/001-example.md"
run_regomark --no-judge
expect "green:non-accepted-link-not-checked" 0 ""

### 構造 rule ごとの異常系 (red)
setup_file_name() { adr 001 'Accepted'; mv "$repo/docs/adr/001-example.md" "$repo/docs/adr/001_Example.md"; }
deny_case file-name setup_file_name

setup_title_number() { adr 001 'Accepted'; sed -i 's/^# ADR-001: 例/# ADR-009: 例/' "$repo/docs/adr/001-example.md"; }
deny_case title setup_title_number

setup_title_count() { adr 001 'Accepted'; printf '\n# 二つ目\n' >>"$repo/docs/adr/001-example.md"; }
deny_case title setup_title_count

setup_missing_section() { adr 001 'Accepted'; sed -i 's/^## Consequences/### Consequences/' "$repo/docs/adr/001-example.md"; }
deny_case required-sections setup_missing_section

setup_duplicate_section() { adr 001 'Accepted'; sed -i "s/^CONTEXT-MARKER$/CONTEXT-MARKER\n\n## Context\n\n再掲/" "$repo/docs/adr/001-example.md"; }
deny_case required-sections setup_duplicate_section

setup_order() {
	printf '# ADR-001: 例\n\n## Status\n\nAccepted\n\n## Decision\n\n決定\n\n## Context\n\n背景\n\n## Consequences\n\n帰結\n' >"$repo/docs/adr/001-example.md"
}
deny_case section-order setup_order

setup_status_paragraph() {
	printf '# ADR-001: 例\n\n## Status\n\n- Accepted\n\n## Context\n\n背景\n\n## Decision\n\n決定\n\n## Consequences\n\n帰結\n' >"$repo/docs/adr/001-example.md"
}
deny_case status-paragraph setup_status_paragraph

setup_proposed() { adr 001 'Proposed'; }
deny_case status-value setup_proposed

setup_status_form() { adr 001 'Superseded by ADR-2'; }
deny_case status-value setup_status_form

setup_status_lines() { adr 001 'Accepted\n理由はあとで書く'; }
deny_case status-lines setup_status_lines

setup_placement() { adr 001 'Accepted\n\nSuperseded by ADR-002'; adr 002 'Accepted'; }
deny_case reference-placement setup_placement

setup_target() { adr 001 'Superseded by ADR-009'; }
deny_case reference-target setup_target

setup_direction() { adr 001 'Accepted'; adr 002 'Superseded by ADR-001'; }
deny_case reference-direction setup_direction

setup_partly_target() { adr 001 'Accepted\nPartly replaced by ADR-009'; }
deny_case reference-target setup_partly_target

setup_duplicate() {
	adr 001 'Accepted'
	cp "$repo/docs/adr/001-example.md" "$repo/docs/adr/001-other.md"
}
deny_case duplicate-number setup_duplicate

setup_gap() { adr 001 'Accepted'; adr 003 'Accepted'; }
deny_case number-sequence setup_gap

setup_zero() { adr 001 'Accepted'; adr 000 'Accepted'; }
deny_case number-sequence setup_zero

setup_missing_link() {
	adr 001 'Accepted'
	printf '\n[guard](../../renamed-away.txt)\n' >>"$repo/docs/adr/001-example.md"
}
deny_case broken-link setup_missing_link

setup_outside_link() {
	adr 001 'Accepted'
	printf '\n[out](../../../outside.txt)\n' >>"$repo/docs/adr/001-example.md"
}
deny_case broken-link setup_outside_link

# 循環は方向違反を伴うため、両方の deny を完全一致で確認する。
new_repo
adr 001 'Superseded by ADR-002'
adr 002 'Superseded by ADR-001'
run_regomark --no-judge
expect "red:replacement-cycle" 1 "deny:reference-direction,deny:replacement-cycle"

# 除外は完全一致だけで、似た名前の実 ADR は検査から漏れない。
new_repo
adr 001 'Accepted'
cp "$repo/docs/adr/001-example.md" "$repo/docs/adr/readme.md"
sed -i 's/^# ADR-001/# ADR-777/' "$repo/docs/adr/readme.md"
run_regomark --no-judge
expect "red:excluded-name-lookalike" 1 "deny:file-name"

### 検査できない異常は exit 2 (偽 green にならない)
overlay() { printf '%s\n' "$2" >"$repo/.regomark/policy/$1"; }

new_repo; adr 001 'Accepted'
overlay typo.rego 'package adr
denies contains {"rule": "title", "path": "x", "line": 1, "message": "m"} if { input.documents }'
run_regomark --no-judge; expect "exit2:deny-misspelled" 2 ""

new_repo; adr 001 'Accepted'
overlay wrongpkg.rego 'package adr.links
deny contains {"rule": "title", "path": "x", "line": 1, "message": "m"} if { input.documents }'
run_regomark --no-judge; expect "exit2:wrong-entrypoint-package" 2 ""

new_repo; adr 001 'Accepted'
overlay undefinedref.rego 'package adr
import data.adrdoc
deny contains {"rule": "title", "path": "x", "line": 1, "message": "m"} if { adrdoc.no_such_helper == 1 }'
run_regomark --no-judge; expect "exit2:undefined-reference" 2 ""

new_repo; adr 001 'Accepted'
overlay badinput.rego 'package adr
deny contains {"rule": "title", "path": d.path, "line": 1, "message": "m"} if { some d in input.documents; d.nmae == "x" }'
run_regomark --no-judge; expect "exit2:input-schema-typo" 2 ""

new_repo; adr 001 'Accepted'
overlay fnrule.rego 'package adr
deny(x) := x if { x }'
run_regomark --no-judge; expect "exit2:deny-as-function" 2 ""

new_repo; adr 001 'Accepted'
overlay syntax.rego 'package adr
deny contains {'
run_regomark --no-judge; expect "exit2:rego-parse-error" 2 ""

new_repo; adr 001 'Accepted'
overlay shape.rego 'package adr
deny contains {"rule": "title", "path": "x"} if { input.documents }'
run_regomark --no-judge; expect "exit2:violation-shape" 2 ""

new_repo; adr 001 'Accepted'
overlay unknownrule.rego 'package adr
deny contains {"rule": "not-declared", "path": "x", "line": 1, "message": "m"} if { input.documents }'
run_regomark --no-judge; expect "exit2:undeclared-rule-id" 2 ""

new_repo; adr 001 'Accepted'
overlay readsjudge.rego 'package adr
deny contains {"rule": "title", "path": j.path, "line": 1, "message": "m"} if { some j in input.judgments; j.verdict == "yes" }'
# 回答なしの評価と結果が変わる deny は、回答を渡した実行で exit 2 になる。
(cd "$repo" && "$regomark" questions) >"$work/questions.json"
jq '{answers: [.questions[] | {key, verdict: "yes", reason: "r", evidence_line: 13}]}' "$work/questions.json" >"$work/answers.json"
run_regomark --judgments "$work/answers.json"; expect "exit2:deny-reads-judgments" 2 ""

new_repo; adr 001 'Accepted'
sed -i 's/^skip_rules: \[\]/skip_rules: [no-such-rule]/' "$repo/.regomark.yaml"
run_regomark --no-judge; expect "exit2:skip-rules-typo" 2 ""

new_repo; adr 001 'Accepted'
printf 'unknown_key: 1\n' >>"$repo/.regomark.yaml"
run_regomark --no-judge; expect "exit2:unknown-config-key" 2 ""

new_repo; adr 001 'Accepted'
rm "$repo/.regomark.yaml"
run_regomark --no-judge; expect "exit2:missing-config" 2 ""

new_repo
run_regomark --no-judge; expect "exit2:no-documents" 2 ""

new_repo; adr 001 'Accepted'
rm -rf "$repo/.regomark/policy"; mkdir "$repo/.regomark/policy"
run_regomark --no-judge; expect "exit2:no-policy" 2 ""

new_repo; adr 001 'Accepted'
ln -s missing-target.md "$repo/docs/adr/002-broken.md"
run_regomark --no-judge; expect "exit2:broken-symlink" 2 ""

### 意味検査 (warn のみ)。実 LLM の代わりに判定コマンドを差し替えて境界を確かめる。
use_judge() { # <回答のシェル断片>
	local command_script="$work/judge.sh"
	printf '#!/bin/sh\necho called >>"%s/judge-calls"\ncat >"%s/judge-prompt"\n%s\n' "$work" "$work" "$1" >"$command_script"
	chmod +x "$command_script"
	sed -i "s|^  command: .*|  command: [\"$command_script\"]|; s|^  timeout: .*|  timeout: 1s|" "$repo/.regomark.yaml"
	printf '  cache: .judge-cache\n' >>"$repo/.regomark.yaml"
	printf '.judge-cache\n' >"$repo/.gitignore"
	: >"$work/judge-calls"
}
calls() { wc -l <"$work/judge-calls" | tr -d ' '; }
check_count() { # <ケース名> <期待値> <実測>
	if [ "$2" = "$3" ]; then
		passes=$((passes + 1))
		echo "ok   $1"
	else
		failures=$((failures + 1))
		echo "FAIL $1: $3 (期待 $2)"
	fi
}
yes_answer='printf "{\"verdict\":\"yes\",\"reason\":\"実装の説明です\",\"evidence_line\":13}"'

new_repo; adr 001 'Accepted'; use_judge "$yes_answer"
run_regomark --judge
expect "warn:yes-keeps-exit0" 0 "warn:multiple-decisions,warn:procedure,warn:spec-detail"
if grep -q CONTEXT-MARKER "$work/judge-prompt"; then prompt_has_context=yes; else prompt_has_context=no; fi
check_count "judge:prompt-has-only-needed-blocks" no "$prompt_has_context"
check_count "judge:asked-three-questions" 3 "$(calls)"
run_regomark --judge
check_count "judge:second-run-uses-cache" 3 "$(calls)"
sed -i 's/^決定$/変更後の決定/' "$repo/docs/adr/001-example.md"
run_regomark --judge
check_count "judge:change-invalidates-cache" 6 "$(calls)"

new_repo; adr 001 'Accepted'; use_judge 'exit 1'
run_regomark --judge
expect "warn:provider-failed-keeps-exit0" 0 "warn:multiple-decisions,warn:procedure,warn:spec-detail"
if grep -q provider_failed "$work/out"; then failed_reported=yes; else failed_reported=no; fi
check_count "judge:provider-failure-is-reported" yes "$failed_reported"

new_repo; adr 001 'Accepted'; use_judge 'sleep 5'
run_regomark --judge
expect "warn:timeout-keeps-exit0" 0 "warn:multiple-decisions,warn:procedure,warn:spec-detail"

new_repo; adr 001 'Accepted'; use_judge 'echo not-json'
run_regomark --judge
expect "warn:invalid-answer-keeps-exit0" 0 "warn:multiple-decisions,warn:procedure,warn:spec-detail"
if grep -q invalid_answer "$work/out"; then invalid_reported=yes; else invalid_reported=no; fi
check_count "judge:invalid-answer-is-reported" yes "$invalid_reported"

new_repo; adr 001 'Accepted'; use_judge 'printf "{\"verdict\":\"yes\",\"reason\":\"範囲外\",\"evidence_line\":1}"'
run_regomark --judge
if grep -q invalid_answer "$work/out"; then outside_evidence=invalid; else outside_evidence=accepted; fi
check_count "judge:evidence-outside-sent-lines-rejected" invalid "$outside_evidence"

new_repo; adr 001 'Accepted'; use_judge 'printf "{\"verdict\":\"no\",\"reason\":\"問題なし\",\"evidence_line\":0}"'
run_regomark --judge
expect "judge:no-verdict-is-silent" 0 ""

new_repo; adr 001 'Proposed'; use_judge "$yes_answer"
run_regomark --no-judge; expect "judge:deny-baseline" 1 "deny:status-value"
run_regomark --judge
expect "judge:answers-do-not-change-deny" 1 "deny:status-value"
check_count "judge:non-accepted-adr-not-asked" 0 "$(calls)"

# 利用者が明示しない限り判定コマンドは動かない。
new_repo; adr 001 'Accepted'; use_judge "$yes_answer"
run_regomark
expect "judge:off-by-default" 0 ""
check_count "judge:off-by-default-not-called" 0 "$(calls)"
(cd "$repo" && REGOMARK_JUDGE=on "$regomark" check --format json --no-judge) >"$work/out" 2>"$work/err"
actual_exit=$?
expect "judge:no-judge-beats-env" 0 ""
check_count "judge:no-judge-beats-env-not-called" 0 "$(calls)"

# 質問数の上限は --judge だけを止め、必須の構造検査には影響しない。
new_repo; adr 001 'Accepted'; use_judge "$yes_answer"
sed -i 's/^  max_questions: .*/  max_questions: 2/' "$repo/.regomark.yaml"
run_regomark --judge; expect "judge:over-limit-exits-2" 2 ""
check_count "judge:over-limit-not-called" 0 "$(calls)"
run_regomark --no-judge; expect "judge:over-limit-structure-still-runs" 0 ""

# CI 向けの回答ファイル。キーは内容から作られ、変更後の古い回答は当たらない。
new_repo; adr 001 'Accepted'
(cd "$repo" && "$regomark" questions) >"$work/questions.json"
jq '{answers: [.questions[] | {key, verdict: "yes", reason: "ファイル回答", evidence_line: 13}]}' "$work/questions.json" >"$work/answers.json"
run_regomark --judgments "$work/answers.json"
expect "judgments:answers-file-warn-only" 0 "warn:multiple-decisions,warn:procedure,warn:spec-detail"
sed -i 's/^決定$/変更後の決定/' "$repo/docs/adr/001-example.md"
run_regomark --judgments "$work/answers.json"
if grep -q 'ファイル回答' "$work/out"; then stale=used; else stale=dropped; fi
check_count "judgments:stale-answer-dropped" dropped "$stale"
if grep -q provider_failed "$work/out"; then unanswered=reported; else unanswered=hidden; fi
check_count "judgments:unanswered-is-not-pass" reported "$unanswered"

# 期待値の比較自体が不一致を検出できることを確かめる。
saved_failures=$failures saved_passes=$passes
actual_exit=1
printf '[]' >"$work/out"
expect "self-test (不一致を検出するので FAIL 表示が正常)" 0 "" >/dev/null
if [ "$failures" -eq "$((saved_failures + 1))" ]; then self_test=detected; else self_test=missed; fi
failures=$saved_failures passes=$saved_passes
check_count "self-test:mismatch-detected" detected "$self_test"

echo "e2e: $passes 件成功、$failures 件失敗"
[ "$failures" -eq 0 ] && [ "$passes" -gt 0 ]
