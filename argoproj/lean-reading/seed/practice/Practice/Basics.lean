/-!
# 操作確認用の小さな例

本の内容とは別の、InfoView の見方を確かめるための例です。自由に書き換えてかまいません。
カーソルを各行に置くと、下の **Lean InfoView** に結果が出ます。
InfoView が見えないときは、画面上部の `∀` メニュー → "Toggle InfoView"。
-/

namespace Practice

-- 1. 型を見る: `#check` の行にカーソルを置く
#check Nat.add
#check fun (n : Nat) => n + 1

-- 2. 評価結果を見る: `#eval` の行にカーソルを置く（42 と出る）
def double (n : Nat) : Nat := n + n

#eval double 21

#eval [1, 2, 3].map double

-- 3. 証明途中のゴールを見る: `by` の下の各行にカーソルを置くと、
--    その時点で示すべき式（ゴール）が InfoView に出る
theorem double_eq_two_mul (n : Nat) : double n = 2 * n := by
  unfold double
  omega

-- 4. 自分で試す欄: `sorry` を消して証明を書いてみる（黄色の波線は sorry の警告）
example (a b : Nat) : a + b = b + a := by
  sorry

end Practice
