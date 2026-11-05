# ADR 文書を読むための補助関数。判定は持たず、deny/warn/questions は定義しない。
package adrdoc

required_sections := ["Status", "Context", "Decision", "Consequences"]

# 指定レベルの見出しを、blocks 内の位置つきで返す。
headings_at(document, level) := [{"index": index, "text": block.text, "line": block.line} |
	some index, block in document.blocks
	block.kind == "heading"
	block.level == level
]

# ファイル名先頭の 3 桁の番号。形式違いのファイルでは未定義になる。
adr_number(document) := to_number(prefix) if {
	regex.match(`^[0-9]{3}-`, document.name)
	prefix := substring(document.name, 0, 3)
}

# 指定した H2 のうち最初のものの直下にある blocks。次の H1/H2 で打ち切る。
section_blocks(document, name) := array.slice(document.blocks, start + 1, stop) if {
	starts := [h.index | some h in headings_at(document, 2); h.text == name]
	count(starts) > 0
	start := starts[0]
	later := [i |
		some i, block in document.blocks
		i > start
		block.kind == "heading"
		block.level <= 2
	]
	stop := min(array.concat(later, [count(document.blocks)]))
}

# Status 直下の先頭ブロックが段落のときだけ定義される。
status_paragraph(document) := paragraph if {
	paragraph := section_blocks(document, "Status")[0]
	paragraph.kind == "paragraph"
}

status_lines(document) := [trim_space(text) | some text in split(status_paragraph(document).text, "\n")]

status_value(document) := status_lines(document)[0]

is_accepted(document) if status_value(document) == "Accepted"

# Status 先頭段落にある置換参照。形が正しい行だけを拾う。
references(document) := {{"label": match[1], "target": to_number(match[2]), "line": status_paragraph(document).line + offset} |
	some offset, text in status_lines(document)
	some match in regex.find_all_string_submatch_n(`^(Superseded by|Partly replaced by) ADR-([0-9]{3})\.?$`, text, 1)
}
