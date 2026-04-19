# `upwork_to_notion.py` 実行手順

Upwork の検索結果 HTML を読み取り、Notion のデータソースに案件を登録・更新するスクリプトの使い方です。

---

## 1. 前提条件

- **Python 3** がインストールされていること
- **Notion** で [インテグレーション](https://www.notion.so/my-integrations)（Internal integration）を作成し、**シークレットトークン**を取得していること
- 対象の **Notion ページ／データベース**に、そのインテグレーションを **共有（Invite）** してアクセス権を付与していること

---

## 2. 作業ディレクトリ

リポジトリ（またはスクリプトがあるフォルダ）に移動します。

```powershell
cd "C:\Users\mimiz\OneDrive\Desktop\upwork-notion-workflow"
```

以降のコマンドは、このフォルダをカレントにした状態で実行してください（`.env` は **カレントディレクトリの `.env`** を読みます）。

---

## 3. 仮想環境（推奨）

### 3.1 作成

```powershell
python -m venv .venv
```

### 3.2 有効化（PowerShell）

```powershell
.\.venv\Scripts\Activate.ps1
```

プロンプト先頭に `(.venv)` が付けば有効です。

「スクリプトの実行が無効」と出る場合は、一度だけ次を実行してから再度有効化します。

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 3.3 依存パッケージのインストール

```powershell
python -m pip install --upgrade pip
python -m pip install beautifulsoup4 requests
```

### 3.4 無効化

```powershell
deactivate
```

---

## 4. 環境変数（`.env`）

プロジェクト直下に **`.env`** を置きます。スクリプト起動時に自動で読み込みます（追加の `pip` パッケージは不要です）。

### 4.1 必須

| 変数 | 説明 |
|------|------|
| `NOTION_TOKEN` | Notion インテグレーションのシークレット（`secret_...` または `ntn_...` など） |

### 4.2 Notion への書き込み先（いずれか）

| 変数 / オプション | 説明 |
|-------------------|------|
| `NOTION_DATASOURCE_ID` または `--datasource-id` | **推奨**。Notion API `2025-09-03` 以降の **データソース ID** |
| `NOTION_DATABASE_ID` または `--database-id` | **データベース（コンテナ）ID**。スクリプトが中のデータソースを解決して使います |
| `NOTION_PARENT_PAGE_ID` + `--create-db` | 親ページの下に **新規データベース＋初期データソース**を作成してから取り込み |

### 4.3 任意

| 変数 | 説明 |
|------|------|
| `UPWORK_DATABASE_TITLE` | `--create-db` 時の DB タイトル（未設定時は `Upwork案件候補`） |
| `DEBUG` | `1` にするとデバッグログを stderr に出力 |

### 4.4 `.env` の例

```dotenv
NOTION_TOKEN=your_token_here
NOTION_DATASOURCE_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
# または
# NOTION_DATABASE_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# 新規作成するときのみ
# NOTION_PARENT_PAGE_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

UPWORK_DATABASE_TITLE=Upwork案件候補
DEBUG=0
```

### 4.5 `.env` の注意

- **UTF-8 で保存**することを推奨します（メモ帳の UTF-16 だけだと読み取りに失敗することがあります）。
- ファイル名が **`.env.txt` になっていない**か確認してください（拡張子表示を ON にすると分かりやすいです）。
- **OS やシェルで既に設定されている環境変数**は、`.env` より優先されます（上書きしません）。

---

## 5. 入力 HTML

- `--input-dir` で指定するフォルダ直下の **`*.html` のみ**が対象です（サブフォルダは見ません）。
- パース対象は **Upwork 検索結果のジョブタイル**（`article[data-test="JobTile"]` など）を含む HTML です。詳細はスクリプト内 `UpworkParser` を参照してください。

HTML を `input` フォルダに置く例:

```
upwork-notion-workflow/
  input/
    job1.html
    job2.html
```

---

## 6. 実行コマンド

### 6.1 パースだけ確認（Notion に触れない）

```powershell
python .\upwork_to_notion.py --input-dir .\input --dry-run
```

### 6.2 単一ファイルを指定

```powershell
python .\upwork_to_notion.py --html .\input\job1.html --dry-run
```

### 6.3 既存のデータソースに取り込み（`.env` に ID を書いた場合）

```powershell
python .\upwork_to_notion.py --input-dir .\input
```

### 6.4 コマンドラインでデータソース ID を指定

```powershell
python .\upwork_to_notion.py --input-dir .\input --datasource-id YOUR_DATASOURCE_ID
```

### 6.5 コマンドラインでデータベース（コンテナ）ID を指定

```powershell
python .\upwork_to_notion.py --input-dir .\input --database-id YOUR_DATABASE_ID
```

### 6.6 親ページの下に新規 DB を作成してから取り込み

`.env` に `NOTION_PARENT_PAGE_ID` を設定し、親ページをインテグレーションに共有したうえで:

```powershell
python .\upwork_to_notion.py --input-dir .\input --create-db
```

成功すると標準出力に **`Created database:`** と **`Created data source:`** が表示されます。以降の運用では、表示された **データソース ID** を `NOTION_DATASOURCE_ID` に保存すると扱いやすいです。

### 6.7 パース結果を JSON に保存

```powershell
python .\upwork_to_notion.py --input-dir .\input --dry-run --dump-json .\parsed_jobs.json
```

---

## 7. よくあるエラーと対処

| 症状 | 考えられる原因 |
|------|----------------|
| `No HTML files found` | `--html` / `--input-dir` が未指定、またはフォルダに `.html` がない |
| `Environment variable is required: NOTION_TOKEN` | `.env` が空・未保存・別フォルダで実行・ファイル名が `.env.txt` など |
| `Provide --create-db or --datasource-id ...` | 書き込み先の ID が未設定 |
| Notion API **404** / `object_not_found` | 対象 DB／ページに **インテグレーションを共有**していない、または **ID の取り違え**（親ページ ID とデータソース ID を混同しない） |
| `ModuleNotFoundError: requests` | 仮想環境で `pip install beautifulsoup4 requests` を実行していない |

---

## 8. コマンドライン引数一覧（参考）

| 引数 | 説明 |
|------|------|
| `--html` | 単一の HTML ファイル |
| `--input-dir` | `*.html` を列挙するディレクトリ |
| `--dry-run` | パースのみ（Notion API を呼ばない） |
| `--create-db` | `NOTION_PARENT_PAGE_ID` 配下に新規 DB を作成 |
| `--database-id` | 既存データベース（コンテナ）ID |
| `--datasource-id` | 既存データソース ID（推奨） |
| `--dump-json` | パース結果を指定パスに JSON 出力 |

