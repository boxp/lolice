# ADR 番号の連番と、置換参照の先・向き・循環を検査する。
package adr

import data.adrdoc

known_numbers contains adrdoc.adr_number(document) if some document in input.documents

paths_by_number[number] := {document.path |
	some document in input.documents
	adrdoc.adr_number(document) == number
} if {
	some number in known_numbers
}

# 番号が重複していても壊れないよう、path 単位の有向グラフにする。
replaced_by[document.path] := {target_path |
	some reference in adrdoc.references(document)
	some target_path in paths_by_number[reference.target]
} if {
	some document in input.documents
}

deny contains {
	"rule": "reference-target",
	"path": document.path,
	"line": reference.line,
	"message": sprintf("%s ADR-%03d を指していますが、その ADR は存在しません", [reference.label, reference.target]),
} if {
	some document in input.documents
	some reference in adrdoc.references(document)
	not reference.target in known_numbers
}

deny contains {
	"rule": "reference-direction",
	"path": document.path,
	"line": reference.line,
	"message": sprintf("%s ADR-%03d は自分より後の番号を指してください", [reference.label, reference.target]),
} if {
	some document in input.documents
	some reference in adrdoc.references(document)
	reference.target <= adrdoc.adr_number(document)
}

deny contains {
	"rule": "replacement-cycle",
	"path": document.path,
	"line": reference.line,
	"message": sprintf("%s ADR-%03d は置換の循環を作っています", [reference.label, reference.target]),
} if {
	some document in input.documents
	some reference in adrdoc.references(document)
	document.path in graph.reachable(replaced_by, paths_by_number[reference.target])
}

deny contains {
	"rule": "duplicate-number",
	"path": path,
	"line": 1,
	"message": sprintf("ADR 番号 %03d が複数のファイルで使われています", [number]),
} if {
	some number, paths in paths_by_number
	count(paths) > 1
	some path in paths
}

deny contains {
	"rule": "number-sequence",
	"path": path,
	"line": 1,
	"message": "ADR 番号 000 は使いません。番号は 001 から始めてください",
} if {
	some path in paths_by_number[0]
}

deny contains {
	"rule": "number-sequence",
	"path": path,
	"line": 1,
	"message": sprintf("ADR 番号 %03d が欠番です。番号は 001 から欠番なく振ってください", [missing]),
} if {
	some missing in numbers.range(1, max(known_numbers))
	not missing in known_numbers
	next_number := min({number | some number in known_numbers; number > missing})
	some path in paths_by_number[next_number]
}
