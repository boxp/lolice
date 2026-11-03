# LLM に尋ねる意味検査。回答は warn だけが読み、deny と終了コードには影響しない。
# 判定を動かすのは利用者が明示したときだけで、尋ねるのは Accepted の ADR の該当節だけ。
package adr

import data.adrdoc

meaning_questions := {
	"spec-detail": {
		"sections": ["Decision"],
		"ask": concat("", [
			"この ADR の Decision は、決定ではなく実装の現状を説明していますか。",
			"定数の値、関数名や構造体を追う処理の説明、画面の挙動、コードや設定の断片、",
			"別の場所が持つ一覧の転記があれば yes です。",
			"その詳細自体が決定 (他者が依存する保証、安全策、利用者に見える名前、却下した案) なら no です。",
		]),
	},
	"procedure": {
		"sections": ["Decision", "Consequences"],
		"ask": concat("", [
			"この ADR の Decision または Consequences は、決定とその帰結ではなく手順書になっていますか。",
			"移行や展開の手順、作業の順序、TODO リストがあれば yes です。",
		]),
	},
	"multiple-decisions": {
		"sections": ["Decision"],
		"ask": concat("", [
			"この ADR の Decision には、別々に覆せる独立した決定が複数混在していますか。",
			"1 つの決定とその構成要素や却下した案であれば no です。",
		]),
	},
}

questions contains {"id": id, "path": document.path, "line": line, "blocks": blocks, "ask": question.ask} if {
	some document in input.documents
	adrdoc.is_accepted(document)
	some id, question in meaning_questions
	line := min({h.line | some h in adrdoc.headings_at(document, 2); h.text == question.sections[0]})
	blocks := [block |
		some name in question.sections
		some block in adrdoc.section_blocks(document, name)
	]
	count(blocks) > 0
}

warn contains {"rule": judgment.id, "path": judgment.path, "line": judgment.evidence_line, "message": judgment.reason} if {
	some judgment in input.judgments
	meaning_questions[judgment.id]
	judgment.status == "ok"
	judgment.verdict == "yes"
}

# 尋ねたのに答えが得られなかった場合は、意味検査を通過したように見せない。
# not_run (未実行) は何も報告しない。未実行と通過の区別は実行入口の表示で示す。
warn contains {
	"rule": judgment.id,
	"path": judgment.path,
	"line": judgment.line,
	"message": sprintf("意味検査を判定できませんでした (%s): %s", [judgment.status, judgment.error]),
} if {
	some judgment in input.judgments
	meaning_questions[judgment.id]
	judgment.status in {"provider_failed", "invalid_answer"}
}
