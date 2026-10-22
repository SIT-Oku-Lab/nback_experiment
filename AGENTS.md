# ブランチ運用ルール

参照: https://qiita.com/yuto-vj/items/9fea43402728d5e4d9de

- `main`: 本番・安定版。初期化後は直接commit/pushせず、PR経由で反映する。
- `develop`: 開発統合用。通常の作業ブランチは最新のdevelopから作成する。
- `feature/<内容>`: 新機能。1機能につき1ブランチ。
- `fix/<内容>`: 軽微な修正・バグ対応。
- `hotfix/<内容>`: 本番障害など緊急対応。必要に応じmainから作成し、developにも反映する。
- 作業後は作業ブランチをpushし、develop向けPRを作成する。ブランチ名は目的が分かる名前とし、小さく早めに統合する。

初回のみ、空リポジトリのmainに本ルールを配置し、そのコミットからdevelopを作成する。プロジェクト本体はfeature/initial-importからPRで取り込む。
