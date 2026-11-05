package adr_test

import data.adr

# 正常な ADR の blocks を作る。Status 段落の本文だけを差し替えられる。
blocks_for(number, status) := [
	{"kind": "heading", "level": 1, "line": 1, "end_line": 1, "text": sprintf("ADR-%s: 例", [number]), "lang": ""},
	{"kind": "heading", "level": 2, "line": 3, "end_line": 3, "text": "Status", "lang": ""},
	{"kind": "paragraph", "level": 0, "line": 5, "end_line": 5, "text": status, "lang": ""},
	{"kind": "heading", "level": 2, "line": 7, "end_line": 7, "text": "Context", "lang": ""},
	{"kind": "paragraph", "level": 0, "line": 9, "end_line": 9, "text": "背景", "lang": ""},
	{"kind": "heading", "level": 2, "line": 11, "end_line": 11, "text": "Decision", "lang": ""},
	{"kind": "paragraph", "level": 0, "line": 13, "end_line": 13, "text": "決定", "lang": ""},
	{"kind": "heading", "level": 2, "line": 15, "end_line": 15, "text": "Consequences", "lang": ""},
	{"kind": "paragraph", "level": 0, "line": 17, "end_line": 17, "text": "帰結", "lang": ""},
]

document_for(number, status) := {
	"path": sprintf("docs/adr/%s-example.md", [number]),
	"name": sprintf("%s-example.md", [number]),
	"blocks": blocks_for(number, status),
	"links": [],
}

denied_rules(documents) := {violation.rule | some violation in denied} if {
	denied := adr.deny with input as {"documents": documents, "judgments": []}
}

test_valid_chain_has_no_deny if {
	count(denied_rules([
		document_for("001", "Superseded by ADR-002"),
		document_for("002", "Accepted\nPartly replaced by ADR-003"),
		document_for("003", "Deprecated"),
		document_for("004", "Rejected"),
	])) == 0
}

test_file_name_denied if {
	document := object.union(document_for("001", "Accepted"), {"name": "001_Bad.md"})
	"file-name" in denied_rules([document])
}

test_title_number_mismatch_denied if {
	document := object.union(document_for("001", "Accepted"), {"blocks": blocks_for("009", "Accepted")})
	"title" in denied_rules([document])
}

test_title_without_text_denied if {
	blocks := array.concat(
		[{"kind": "heading", "level": 1, "line": 1, "end_line": 1, "text": "ADR-001: ", "lang": ""}],
		array.slice(blocks_for("001", "Accepted"), 1, 9),
	)
	"title" in denied_rules([object.union(document_for("001", "Accepted"), {"blocks": blocks})])
}

test_missing_section_denied if {
	blocks := array.slice(blocks_for("001", "Accepted"), 0, 7)
	"required-sections" in denied_rules([object.union(document_for("001", "Accepted"), {"blocks": blocks})])
}

test_section_order_denied if {
	original := blocks_for("001", "Accepted")
	blocks := array.flatten([array.slice(original, 0, 3), array.slice(original, 5, 7), array.slice(original, 3, 5), array.slice(original, 7, 9)])
	"section-order" in denied_rules([object.union(document_for("001", "Accepted"), {"blocks": blocks})])
}

test_proposed_denied if {
	"status-value" in denied_rules([document_for("001", "Proposed")])
}

test_status_lines_denied if {
	"status-lines" in denied_rules([document_for("001", "Accepted\n補足")])
}

test_reference_target_denied if {
	"reference-target" in denied_rules([document_for("001", "Superseded by ADR-002")])
}

test_reference_direction_denied if {
	"reference-direction" in denied_rules([document_for("001", "Accepted"), document_for("002", "Superseded by ADR-001")])
}

test_replacement_cycle_denied if {
	"replacement-cycle" in denied_rules([document_for("001", "Superseded by ADR-002"), document_for("002", "Superseded by ADR-001")])
}

test_number_gap_denied if {
	"number-sequence" in denied_rules([document_for("001", "Accepted"), document_for("003", "Accepted")])
}

test_number_zero_denied if {
	"number-sequence" in denied_rules([document_for("000", "Accepted"), document_for("001", "Accepted")])
}

test_duplicate_number_denied if {
	other := object.union(document_for("001", "Accepted"), {"path": "docs/adr/001-other.md", "name": "001-other.md"})
	"duplicate-number" in denied_rules([document_for("001", "Accepted"), other])
}

test_broken_link_denied_only_when_accepted if {
	link := {"dest": "../x", "text": "x", "line": 18, "target": "x", "kind": "missing"}
	"broken-link" in denied_rules([object.union(document_for("001", "Accepted"), {"links": [link]})])
	not "broken-link" in denied_rules([object.union(document_for("001", "Rejected"), {"links": [link]})])
}

test_questions_only_for_accepted if {
	count(adr.questions) == 3 with input as {"documents": [document_for("001", "Accepted")], "judgments": []}
	count(adr.questions) == 0 with input as {"documents": [document_for("001", "Rejected")], "judgments": []}
}

test_question_blocks_exclude_context if {
	questions := adr.questions with input as {"documents": [document_for("001", "Accepted")], "judgments": []}
	every question in questions {
		every block in question.blocks {
			block.text != "背景"
		}
	}
}

judgment_with(fields) := object.union(
	{"id": "spec-detail", "path": "docs/adr/001-example.md", "line": 11, "status": "ok", "verdict": "yes", "reason": "理由", "evidence_line": 13, "error": ""},
	fields,
)

warned_with(judgment) := warned if {
	warned := adr.warn with input as {"documents": [document_for("001", "Accepted")], "judgments": [judgment]}
}

test_yes_verdict_warns if count(warned_with(judgment_with({}))) == 1

test_no_verdict_is_silent if count(warned_with(judgment_with({"verdict": "no"}))) == 0

test_not_run_is_silent if count(warned_with(judgment_with({"status": "not_run", "verdict": ""}))) == 0

test_provider_failed_warns if count(warned_with(judgment_with({"status": "provider_failed", "verdict": "", "error": "e"}))) == 1

test_invalid_answer_warns if count(warned_with(judgment_with({"status": "invalid_answer", "verdict": "", "error": "e"}))) == 1

test_deny_ignores_judgments if {
	input_value := {"documents": [document_for("001", "Accepted")], "judgments": [judgment_with({})]}
	count(adr.deny) == 0 with input as input_value
}
