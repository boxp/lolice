"""ESO の target.template を Go template として妥当か検証する。
YAML として parse するだけでは、コメント中の {{ }} のような
「意図しないテンプレートアクション」を見逃す (実際に踏んだ)。"""
import sys, re, yaml, pathlib

path = sys.argv[1]
expected = set(sys.argv[2:])
d = yaml.safe_load(pathlib.Path(path).read_text())
tpl = d["spec"]["target"]["template"]["data"]["alertmanager.yaml"]
provided = {x["secretKey"] for x in d["spec"]["data"]}

ok = True
actions = [(i, m.group(0)) for i, l in enumerate(tpl.splitlines(), 1)
           for m in re.finditer(r"\{\{.*?\}\}", l)]
print("テンプレートアクション:")
for ln, a in actions:
    key = re.fullmatch(r"\{\{\s*\.(\w+)\s*\}\}", a)
    if not key:
        print("  %3d: %-28r  ← 想定外のアクション" % (ln, a)); ok = False
    elif key.group(1) not in provided:
        print("  %3d: %-28r  ← spec.data に %s が無い" % (ln, a, key.group(1))); ok = False
    else:
        print("  %3d: %-28r  OK" % (ln, a))

missing = expected - {re.fullmatch(r"\{\{\s*\.(\w+)\s*\}\}", a).group(1)
                      for _, a in actions if re.fullmatch(r"\{\{\s*\.(\w+)\s*\}\}", a)}
if missing:
    print("必須プレースホルダが未使用:", missing); ok = False

rendered = tpl
for k in provided:
    rendered = rendered.replace("{{ .%s }}" % k, "dummy-%s" % k)
if "{{" in rendered or "}}" in rendered:
    print("展開後に波括弧が残っている"); ok = False
try:
    cfg = yaml.safe_load(rendered)
    print("展開後 YAML: OK / receivers =", [r["name"] for r in cfg["receivers"]])
except Exception as e:
    print("展開後 YAML が不正:", e); ok = False

pathlib.Path("/tmp/claude-1000/-home-boxp/4861e90a-59a1-45dd-a94f-8c83bc789901/scratchpad/am-rendered2.yaml").write_text(rendered)
print("\n結果:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
