import os
import sys
import re
import time
import requests

NOTION_API_KEY = os.getenv("NOTION_API_KEY")
PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID")

if not NOTION_API_KEY:
    NOTION_API_KEY = input("Notion API Key (ntn_...): ").strip()
if not PARENT_PAGE_ID:
    raw_parent = input("Notion 親ページID または 親ページURL: ").strip()
    # 末尾の32桁16進数を正確に抽出
    clean_str = raw_parent.split("?")[0].replace("-", "")
    match = re.search(r"([0-9a-fA-F]{32})", clean_str)
    if match:
        PARENT_PAGE_ID = match.group(1)
    else:
        PARENT_PAGE_ID = clean_str[-32:]

print(f"\n🔑 認識した親ページID: {PARENT_PAGE_ID}")

HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

def create_database(title, properties):
    url = "https://api.notion.com/v1/databases"
    payload = {
        "parent": {"type": "page_id", "page_id": PARENT_PAGE_ID},
        "title": [{"type": "text", "text": {"content": title}}],
        "properties": properties
    }
    res = requests.post(url, headers=HEADERS, json=payload)
    if res.status_code != 200:
        print(f"❌ データベース作成失敗 [{title}]: {res.text}")
        sys.exit(1)
    db_id = res.json()["id"]
    print(f"✅ 作成完了: {title} (ID: {db_id})")
    return db_id

def insert_row(db_id, properties):
    url = "https://api.notion.com/v1/pages"
    payload = {
        "parent": {"database_id": db_id},
        "properties": properties
    }
    res = requests.post(url, headers=HEADERS, json=payload)
    if res.status_code != 200:
        print(f"  ⚠️ 行追加エラー: {res.text}")
    time.sleep(0.25)  # Notion API レートリミット回避

print("\n🚀 Notion 4表の自動作成＆データ投入を開始します...\n")

# ==========================================
# 1. 【表1】検索キーワード (13件)
# ==========================================
db1_props = {
    "単語": {"title": {}},
    "グループ": {
        "select": {
            "options": [
                {"name": "撮影者(A)", "color": "blue"},
                {"name": "募集語(B)", "color": "green"}
            ]
        }
    },
    "有効": {"checkbox": {}},
    "備考": {"rich_text": {}}
}
db1_id = create_database("【表1】検索キーワード", db1_props)

db1_initial = [
    # 撮影者(A)
    ("カメラマン", "撮影者(A)", True, "基本ワード"),
    ("撮影してくださる", "撮影者(A)", True, "丁寧語"),
    ("撮影してくれる", "撮影者(A)", True, "一般語"),
    ("撮ってくださる", "撮影者(A)", True, "丁寧語"),
    ("撮ってくれる", "撮影者(A)", True, "一般語"),
    # 募集語(B)
    ("募集", "募集語(B)", True, "基本語"),
    ("探しています", "募集語(B)", True, "丁寧語"),
    ("さがしています", "募集語(B)", True, "ひらがな"),
    ("急募", "募集語(B)", True, "高緊急度"),
    ("ゆるぼ", "募集語(B)", True, "ひらがな"),
    ("ゆる募", "募集語(B)", True, "漢字混じり"),
    ("いらっしゃいませんか", "募集語(B)", True, "丁寧問いかけ"),
    ("いませんか", "募集語(B)", True, "問いかけ")
]

print("➡️ 表1 初期データ投入中...")
for word, grp, act, note in db1_initial:
    insert_row(db1_id, {
        "単語": {"title": [{"text": {"content": word}}]},
        "グループ": {"select": {"name": grp}},
        "有効": {"checkbox": act},
        "備考": {"rich_text": [{"text": {"content": note}}]}
    })

# ==========================================
# 2. 【表2】除外単語マスタ (98件)
# ==========================================
db2_props = {
    "単語": {"title": {}},
    "カテゴリ": {
        "select": {
            "options": [
                {"name": "地方・都市名", "color": "orange"},
                {"name": "サロン・他撮影", "color": "purple"},
                {"name": "求人・バイト", "color": "yellow"},
                {"name": "物販・譲渡", "color": "brown"},
                {"name": "配信・ゲーム", "color": "gray"},
                {"name": "ネガティブ", "color": "red"},
                {"name": "アイドル・夜職", "color": "pink"},
                {"name": "イベント", "color": "blue"},
                {"name": "その他", "color": "default"}
            ]
        }
    },
    "有効": {"checkbox": {}},
    "1段目固定(PIN)": {"checkbox": {}},
    "ステータス": {
        "select": {
            "options": [
                {"name": "確定枠(1段目)", "color": "green"},
                {"name": "探索中", "color": "yellow"},
                {"name": "2段目待機", "color": "default"}
            ]
        }
    },
    "観測スコア(件/日)": {"number": {"format": "number"}},
    "備考": {"rich_text": {}}
}
db2_id = create_database("【表2】除外単語マスタ", db2_props)

db2_initial = [
    # 地方・都市名 (PIN固定: 主要遠方都市・地方)
    ("大阪", "地方・都市名", True, True, "確定枠(1段目)", 15.0, "遠方完全除外"),
    ("名古屋", "地方・都市名", True, True, "確定枠(1段目)", 10.0, "遠方完全除外"),
    ("福岡", "地方・都市名", True, True, "確定枠(1段目)", 8.0, "遠方完全除外"),
    ("札幌", "地方・都市名", True, True, "確定枠(1段目)", 5.0, "遠方完全除外"),
    ("愛知", "地方・都市名", True, True, "確定枠(1段目)", 10.0, "遠方完全除外"),
    ("関西", "地方・都市名", True, True, "確定枠(1段目)", 12.0, "遠方完全除外"),
    # 地方・都市名 (2段目待機: 各主要都市・広域地方・全都道府県)
    ("仙台", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("京都", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("神戸", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("広島", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("静岡", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("博多", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("天神", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("梅田", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("難波", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("栄", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("北海道", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("東北", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("中部", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("九州", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("沖縄", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("青森", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("岩手", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("宮城", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("秋田", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("山形", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("福島", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("茨城", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("栃木", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("群馬", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("新潟", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("富山", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("石川", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("福井", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("山梨", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("長野", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("岐阜", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("三重", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("滋賀", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("兵庫", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("奈良", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("和歌山", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("鳥取", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("島根", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("岡山", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("山口", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("徳島", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("香川", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("愛媛", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("高知", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("佐賀", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("長崎", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("熊本", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("大分", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("宮崎", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    ("鹿児島", "地方・都市名", True, False, "2段目待機", 0.0, ""),
    # サロン・他撮影 (9件)
    ("サロンモデル", "サロン・他撮影", True, False, "2段目待機", 0.0, "美容室系除外"),
    ("サロモ", "サロン・他撮影", True, False, "2段目待機", 0.0, ""),
    ("ヘアカタ", "サロン・他撮影", True, False, "2段目待機", 0.0, ""),
    ("カットモデル", "サロン・他撮影", True, False, "2段目待機", 0.0, ""),
    ("マツエク", "サロン・他撮影", True, False, "2段目待機", 0.0, ""),
    ("前撮り", "サロン・他撮影", True, False, "2段目待機", 0.0, "ブライダル等"),
    ("フォトウェディング", "サロン・他撮影", True, False, "2段目待機", 0.0, ""),
    ("MV撮影", "サロン・他撮影", True, False, "2段目待機", 0.0, ""),
    ("撮影会枠", "サロン・他撮影", True, False, "2段目待機", 0.0, ""),
    # 求人・バイト (7件)
    ("時給", "求人・バイト", True, False, "2段目待機", 0.0, "求人スパム除外"),
    ("日給", "求人・バイト", True, False, "2段目待機", 0.0, ""),
    ("月給", "求人・バイト", True, False, "2段目待機", 0.0, ""),
    ("業務委託", "求人・バイト", True, False, "2段目待機", 0.0, ""),
    ("シフト", "求人・バイト", True, False, "2段目待機", 0.0, ""),
    ("正社員", "求人・バイト", True, False, "2段目待機", 0.0, ""),
    ("副業", "求人・バイト", True, False, "2段目待機", 0.0, ""),
    # 物販・譲渡 (7件)
    ("物撮り", "物販・譲渡", True, False, "2段目待機", 0.0, ""),
    ("商品撮影", "物販・譲渡", True, False, "2段目待機", 0.0, ""),
    ("売り子募集", "物販・譲渡", True, False, "2段目待機", 0.0, ""),
    ("衣装譲渡", "物販・譲渡", True, False, "2段目待機", 0.0, "コスプレ譲渡"),
    ("お譲り", "物販・譲渡", True, False, "2段目待機", 0.0, ""),
    ("買取", "物販・譲渡", True, False, "2段目待機", 0.0, ""),
    ("ウィッグ譲渡", "物販・譲渡", True, False, "2段目待機", 0.0, ""),
    # 配信・ゲーム / VR (6件)
    ("VTuber", "配信・ゲーム", True, False, "2段目待機", 0.0, ""),
    ("クラン", "配信・ゲーム", True, False, "2段目待機", 0.0, ""),
    ("VRChat", "配信・ゲーム", True, False, "2段目待機", 0.0, "VR撮影"),
    ("ぶいちゃ", "配信・ゲーム", True, False, "2段目待機", 0.0, "VR撮影"),
    ("VRC", "配信・ゲーム", True, False, "2段目待機", 0.0, "VR撮影"),
    ("インスタンス", "配信・ゲーム", True, False, "2段目待機", 0.0, "VR空間"),
    # ネガティブ・愚痴 (6件)
    ("クソ", "ネガティブ", True, False, "2段目待機", 0.0, "愚痴ポスト除外"),
    ("下手", "ネガティブ", True, False, "2段目待機", 0.0, ""),
    ("死ね", "ネガティブ", True, False, "2段目待機", 0.0, ""),
    ("ゴミ", "ネガティブ", True, False, "2段目待機", 0.0, ""),
    ("晒し", "ネガティブ", True, False, "2段目待機", 0.0, ""),
    ("愚痴", "ネガティブ", True, False, "2段目待機", 0.0, ""),
    # アイドル・夜職 (4件)
    ("チェキ", "アイドル・夜職", True, False, "2段目待機", 0.0, ""),
    ("チェキスタ", "アイドル・夜職", True, False, "2段目待機", 0.0, ""),
    ("対バン", "アイドル・夜職", True, False, "2段目待機", 0.0, ""),
    ("チケット取り置き", "アイドル・夜職", True, False, "2段目待機", 0.0, ""),
    # イベント・学校行事 (4件)
    ("アコスタ", "イベント", True, False, "2段目待機", 0.0, ""),
    ("ラグコス", "イベント", True, False, "2段目待機", 0.0, ""),
    ("コスサミ", "イベント", True, False, "2段目待機", 0.0, ""),
    ("運動会", "イベント", True, False, "2段目待機", 0.0, ""),
    # その他 (1件)
    ("児童養護施設", "その他", True, False, "2段目待機", 0.0, "")
]

print(f"➡️ 表2 初期データ投入中（全{len(db2_initial)}件）...")
for word, cat, act, pin, stat, sc, note in db2_initial:
    insert_row(db2_id, {
        "単語": {"title": [{"text": {"content": word}}]},
        "カテゴリ": {"select": {"name": cat}},
        "有効": {"checkbox": act},
        "1段目固定(PIN)": {"checkbox": pin},
        "ステータス": {"select": {"name": stat}},
        "観測スコア(件/日)": {"number": sc},
        "備考": {"rich_text": [{"text": {"content": note}}]}
    })

# ==========================================
# 3. 【表3】除外対象作品 (25作品)
# ==========================================
db3_props = {
    "作品名 / 略称": {"title": {}},
    "有効": {"checkbox": {}},
    "備考": {"rich_text": {}}
}
db3_id = create_database("【表3】除外対象作品", db3_props)

db3_initial = [
    ("呪術廻戦", True, "除外作品"),
    ("ブルーロック", True, "除外作品"),
    ("忍たま乱太郎", True, "除外作品"),
    ("東京リベンジャーズ", True, "除外作品"),
    ("刀剣乱舞", True, "除外作品 (とうらぶ)"),
    ("あんさんぶるスターズ", True, "除外作品 (あんスタ)"),
    ("桃源暗鬼", True, "除外作品"),
    ("イナズマイレブン", True, "除外作品 (イナイレ)"),
    ("ツイステッドワンダーランド", True, "除外作品 (ツイステ)"),
    ("ドクターストーン", True, "除外作品 (Dr.STONE)"),
    ("アイドリッシュセブン", True, "除外作品 (アイナナ)"),
    ("ヒプノシスマイク", True, "除外作品 (ヒプマイ)"),
    ("ワールドトリガー", True, "除外作品 (ワートリ)"),
    ("A3!", True, "除外作品 (エースリー)"),
    ("ゴールデンカムイ", True, "除外作品 (金カム)"),
    ("ペルソナ5", True, "除外作品 (P5)"),
    ("ペルソナ4", True, "除外作品 (P4)"),
    ("ペルソナ3", True, "除外作品 (P3)"),
    ("鬼灯の冷徹", True, "除外作品 (鬼火の冷徹含む)"),
    ("銀魂", True, "除外作品"),
    ("HUNTER×HUNTER", True, "除外作品 (ハンターハンター)"),
    ("ワンピース", True, "除外作品 (ONE PIECE)"),
    ("ポケモン", True, "除外作品 (ポケットモンスター)"),
    ("ディズニー", True, "除外作品 (Dハロ/ツイステ等関連)"),
    ("鬼滅の刃", True, "除外作品 (きめつ)")
]

print(f"➡️ 表3 初期データ投入中（全{len(db3_initial)}作品）...")
for genre, act, note in db3_initial:
    insert_row(db3_id, {
        "作品名 / 略称": {"title": [{"text": {"content": genre}}]},
        "有効": {"checkbox": act},
        "備考": {"rich_text": [{"text": {"content": note}}]}
    })

# ==========================================
# 4. 【表4】システム設定 (4件)
# ==========================================
db4_props = {
    "設定項目": {"title": {}},
    "設定値": {"rich_text": {}},
    "有効": {"checkbox": {}}
}
db4_id = create_database("【表4】システム設定", db4_props)

db4_initial = [
    ("AIモデル", "gemini-3.5-flash-lite", True),
    ("本文文字数制限", "200", True),
    ("最小フォロワー数", "0", True),
    ("対象エリア", "東京都,神奈川県,埼玉県,千葉県", True)
]

print("➡️ 表4 初期データ投入中...")
for key, val, act in db4_initial:
    insert_row(db4_id, {
        "設定項目": {"title": [{"text": {"content": key}}]},
        "設定値": {"rich_text": [{"text": {"content": val}}]},
        "有効": {"checkbox": act}
    })

# ==========================================
# 完了出力
# ==========================================
print("\n" + "="*60)
print("🎉 Notion 4表の作成 ＆ 初期データ全件の投入が完了しました！")
print("以下の4つのIDを GitHub Secrets に登録してください：\n")
print(f"NOTION_KEYWORDS_DB_ID : {db1_id.replace('-', '')}")
print(f"NOTION_EXCLUDES_DB_ID : {db2_id.replace('-', '')}")
print(f"NOTION_GENRES_DB_ID   : {db3_id.replace('-', '')}")
print(f"NOTION_CONFIG_DB_ID   : {db4_id.replace('-', '')}")
print("="*60 + "\n")
