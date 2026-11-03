# ファイル名・H1・必須セクションの有無と順序を検査する。
package adr

import data.adrdoc

deny contains {
	"rule": "file-name",
	"path": document.path,
	"line": 1,
	"message": "ファイル名は NNN-kebab-case.md の形にしてください",
} if {
	some document in input.documents
	not regex.match(`^[0-9]{3}-[a-z0-9]+(-[a-z0-9]+)*\.md$`, document.name)
}

deny contains {
	"rule": "title",
	"path": document.path,
	"line": 1,
	"message": sprintf("H1 はちょうど 1 個必要です (現在 %d 個)", [total]),
} if {
	some document in input.documents
	total := count(adrdoc.headings_at(document, 1))
	total != 1
}

deny contains {
	"rule": "title",
	"path": document.path,
	"line": heading.line,
	"message": sprintf("H1 は「%s: タイトル」の形にしてください", [expected]),
} if {
	some document in input.documents
	expected := sprintf("ADR-%03d", [adrdoc.adr_number(document)])
	some heading in adrdoc.headings_at(document, 1)
	not startswith(heading.text, sprintf("%s: ", [expected]))
}

deny contains {
	"rule": "title",
	"path": document.path,
	"line": heading.line,
	"message": "H1 の番号の後ろにタイトルを書いてください",
} if {
	some document in input.documents
	some heading in adrdoc.headings_at(document, 1)
	regex.match(`^ADR-[0-9]{3}:\s*$`, heading.text)
}

deny contains {
	"rule": "required-sections",
	"path": document.path,
	"line": 1,
	"message": sprintf("「## %s」はちょうど 1 個必要です (現在 %d 個)", [name, total]),
} if {
	some document in input.documents
	some name in adrdoc.required_sections
	total := count([h | some h in adrdoc.headings_at(document, 2); h.text == name])
	total != 1
}

deny contains {
	"rule": "section-order",
	"path": document.path,
	"line": later.line,
	"message": sprintf("「## %s」は「## %s」より前に置いてください", [later.text, earlier.text]),
} if {
	some document in input.documents
	found := [h | some h in adrdoc.headings_at(document, 2); h.text in adrdoc.required_sections]
	some position, later in found
	position > 0
	earlier := found[position - 1]
	section_rank[earlier.text] > section_rank[later.text]
}

section_rank[name] := rank if some rank, name in adrdoc.required_sections
