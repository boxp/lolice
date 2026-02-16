# doc/ と docs/ の統合計画

## 背景
リポジトリ内に `doc/` と `docs/` の2つのドキュメントディレクトリが存在し、参照が分散していた。

## 実施内容

### 1. ファイル移動
- `doc/project-spec.md` → `docs/project-spec.md`
- `doc/project-structure.md` → `docs/project-structure.md`
- `doc/project_doc/longhorn-s3-backup/` → `docs/project_docs/longhorn-s3-backup/`
- `doc/project_doc/migrate-to-app-of-apps/` → `docs/project_docs/migrate-to-app-of-apps/`
- `doc/project_doc/add-k8s-manifest-validate-action/` → `docs/project_docs/add-k8s-manifest-validate-action/`

### 2. 参照更新
- `CLAUDE.md`: `/doc/` → `/docs/` (リポジトリ構造、ガイドライン、必読ドキュメントセクション)
- `docs/project-structure.md`: `doc/` → `docs/`、`project_doc/` → `project_docs/` (ツリー構造とセクション見出し)
- `docs/project-spec.md`: `doc/` → `docs/` (ディレクトリ構造セクション)

### 3. ディレクトリ削除
- `doc/` ディレクトリを完全に削除（空ディレクトリも含む）

## 重複・衝突
- `doc/project_doc/` と `docs/project_docs/` のサブディレクトリに重複なし
- 安全に統合可能
