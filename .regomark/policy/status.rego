# Status セクションの形を検査する。Proposed は値として認めない。
package adr

import data.adrdoc

deny contains {
	"rule": "status-paragraph",
	"path": document.path,
	"line": heading.line,
	"message": "「## Status」の直下は状態を書いた段落にしてください",
} if {
	some document in input.documents
	some heading in adrdoc.headings_at(document, 2)
	heading.text == "Status"
	not adrdoc.status_paragraph(document)
}

deny contains {
	"rule": "status-value",
	"path": document.path,
	"line": adrdoc.status_paragraph(document).line,
	"message": sprintf("Status「%s」は Accepted / Rejected / Deprecated / Superseded by ADR-NNN のいずれかにしてください", [value]),
} if {
	some document in input.documents
	value := adrdoc.status_value(document)
	not value in {"Accepted", "Rejected", "Deprecated"}
	not regex.match(`^Superseded by ADR-[0-9]{3}\.?$`, value)
}

deny contains {
	"rule": "status-lines",
	"path": document.path,
	"line": adrdoc.status_paragraph(document).line + offset,
	"message": sprintf("Status の段落に続けて書けるのは「Partly replaced by ADR-NNN」だけです (「%s」)", [text]),
} if {
	some document in input.documents
	some offset, text in adrdoc.status_lines(document)
	offset > 0
	not regex.match(`^Partly replaced by ADR-[0-9]{3}\.?$`, text)
}

deny contains {
	"rule": "reference-placement",
	"path": document.path,
	"line": block.line + offset,
	"message": "他の ADR への置換参照は Status の先頭段落に書いてください",
} if {
	some document in input.documents
	blocks := adrdoc.section_blocks(document, "Status")
	some position, block in blocks
	not is_status_head(block, position)
	some offset, text in split(block.text, "\n")
	regex.match(`(Superseded by|Partly replaced by) ADR-[0-9]{3}`, text)
}

is_status_head(block, 0) if block.kind == "paragraph"
