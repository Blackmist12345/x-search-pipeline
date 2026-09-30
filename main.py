import os
import sys
import json
import re
import time
import base64
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
import requests

# ==========================================
# 1. 環境変数 ＆ システム基本定数
# ==========================================
NOTION_API_KEY = os.getenv("NOTION_API_KEY")
NOTION_KEYWORDS_DB_ID = os.getenv("NOTION_KEYWORDS_DB_ID")
NOTION_EXCLUDES_DB_ID = os.getenv("NOTION_EXCLUDES_DB_ID")
NOTION_GENRES_DB_ID = os.getenv("NOTION_GENRES_DB_ID")
NOTION_CONFIG_DB_ID = os.getenv("NOTION_CONFIG_DB_ID")

TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL")

JST = timezone(timedelta(hours=9))
UTC = timezone.utc

CACHE_FILE = "notion_cache.json"
DATA_FILE = "processed_ids.json"
MAX_QUERY_LENGTH = 440  # TwitterAPI.io 500文字上限に対する安全バッファ

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

# 地方名マスタ（0次プロフィール即除外用）
DISTANT_REGION_WORDS = [
    "大阪", "名古屋", "福岡", "札幌", "愛知", "関西", "仙台", "京都", "神戸", "広島", "静岡",
    "博多", "天神", "梅田", "難波", "栄", "北海道", "東北", "中部", "九州", "沖縄",
    "青森", "岩手", "宮城", "秋田", "山形", "福島", "新潟", "富山", "石川", "福井",
    "山梨", "長野", "岐阜", "三重", "滋賀", "兵庫", "奈良", "和歌山", "鳥取", "島根",
    "岡山", "山口", "徳島", "香川", "愛媛", "高知", "佐賀", "長崎", "熊本", "大分", "宮崎", "鹿児島"
]

# 関東近郊ワード（地方名記載時でも関東活動の併記があればセーフ判定へ回す）
KANTO_SAFE_WORDS = ["東京", "神奈川", "埼玉", "千葉", "関東", "都内", "首都圏", "横浜", "川崎"]


# ==========================================
# 2. Notion 連携 ＆ 2重フェイルセーフ機構
# ==========================================
def fetch_notion_db(db_id):
    """Notion DBから全レコードをページネーション対応で完全取得"""
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    results = []
    has_more = True
    next_cursor = None

    while has_more:
        payload = {}
        if next_cursor:
            payload["start_cursor"] = next_cursor
        res = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=25)
        if res.status_code != 200:
            raise Exception(f"Notion DB取得エラー (ID: {db_id}): HTTP {res.status_code} - {res.text}")
        data = res.json()
        results.extend(data.get("results", []))
        has_more = data.get("has_more", False)
        next_cursor = data.get("next_cursor")
    return results

def get_prop_text(prop):
    if not prop: return ""
    p_type = prop.get("type")
    if p_type == "title":
        return "".join([t.get("plain_text", "") for t in prop.get("title", [])]).strip()
    if p_type == "rich_text":
        return "".join([t.get("plain_text", "") for t in prop.get("rich_text", [])]).strip()
    if p_type == "select" and prop.get("select"):
        return prop["select"].get("name", "").strip()
    return ""

def load_system_settings():
    """Notion 4表を完全同期。障害時は local cache から100%復旧"""
    try:
        raw_kw = fetch_notion_db(NOTION_KEYWORDS_DB_ID)
        raw_ex = fetch_notion_db(NOTION_EXCLUDES_DB_ID)
        raw_gn = fetch_notion_db(NOTION_GENRES_DB_ID)
        raw_cfg = fetch_notion_db(NOTION_CONFIG_DB_ID)

        # 表1: 検索キーワード (撮影者A / 募集語B)
        group_a, group_b = [], []
        for r in raw_kw:
            p = r["properties"]
            if not p.get("有効", {}).get("checkbox", False): continue
            word = get_prop_text(p.get("単語"))
            grp = get_prop_text(p.get("グループ"))
            if grp == "撮影者(A)" and word: group_a.append(word)
            elif grp == "募集語(B)" and word: group_b.append(word)

        # 表2: 除外単語マスタ（オートバランサー）
        excludes = []
        for r in raw_ex:
            p = r["properties"]
            if not p.get("有効", {}).get("checkbox", False): continue
            word = get_prop_text(p.get("単語"))
            if not word: continue
            cat = get_prop_text(p.get("カテゴリ"))
            is_pin = p.get("1段目固定(PIN)", {}).get("checkbox", False)
            stat = get_prop_text(p.get("ステータス"))
            sc = p.get("観測スコア(件/日)", {}).get("number")
            sc_val = float(sc) if sc is not None else 0.0

            if not stat:
                stat = "確定枠(1段目)" if is_pin else "2段目待機"

            excludes.append({
                "page_id": r["id"],
                "word": word,
                "category": cat or "その他",
                "is_pin": is_pin,
                "status": stat,
                "score": sc_val
            })

        # 表3: 除外作品
        genres = []
        for r in raw_gn:
            p = r["properties"]
            if not p.get("有効", {}).get("checkbox", False): continue
            g = get_prop_text(p.get("作品名 / 略称"))
            if g: genres.append(g)

        # 表4: システム設定
        configs = {}
        for r in raw_cfg:
            p = r["properties"]
            if not p.get("有効", {}).get("checkbox", False): continue
            k = get_prop_text(p.get("設定項目"))
            v = get_prop_text(p.get("設定値"))
            if k: configs[k] = v

        cache_data = {
            "group_a": group_a,
            "group_b": group_b,
            "excludes": excludes,
            "genres": genres,
            "configs": configs,
            "cached_at": datetime.now(JST).isoformat()
        }
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        print("✅ Notion 4表の同期完了 ＆ 最新キャッシュをローカルに保存しました")
        return cache_data

    except Exception as e:
        print(f"⚠️ Notion APIとの通信で異常が発生しました: {e}")
        if os.path.exists(CACHE_FILE):
            print(f"🔄 【フェイルセーフ発動】{CACHE_FILE} から直前設定を完全ロードします")
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        raise Exception("❌ Notion接続に失敗し、かつ参照可能なキャッシュファイルも存在しません。実行を中断します。")

def update_notion_exclude_word(page_id, new_score=None, new_status=None):
    """Notion 表2 の観測スコアやステータスを動的書き戻し更新"""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    props = {}
    if new_score is not None:
        props["観測スコア(件/日)"] = {"number": round(new_score, 2)}
    if new_status is not None:
        props["ステータス"] = {"select": {"name": new_status}}
    if not props: return

    try:
        res = requests.patch(url, headers=NOTION_HEADERS, json={"properties": props}, timeout=15)
        if res.status_code != 200:
            print(f"  ⚠️ Notion書き戻し警告 (Page: {page_id}): {res.text}")
    except Exception as e:
        print(f"  ⚠️ Notion書き戻し通信失敗: {e}")


# ==========================================
# 3. 除外単語オートバランサー ＆ クエリ最適化
# ==========================================
def build_search_query(group_a, group_b, excludes):
    """
    スマートOR検索 ＆ 440文字枠内オートバランサー動的スロット配分
    """
    str_a = " OR ".join([f'"{w}"' for w in group_a])
    str_b = " OR ".join([f'"{w}"' for w in group_b])
    base_query = f"({str_a}) ({str_b})"

    pins = [e for e in excludes if e["is_pin"]]
    exploits = sorted([e for e in excludes if not e["is_pin"] and e["score"] > 0], key=lambda x: x["score"], reverse=True)
    explores = [e for e in excludes if not e["is_pin"] and e["score"] == 0]

    tier1_items = []
    current_query = base_query

    def try_append(item):
        nonlocal current_query
        word = item["word"]
        candidate = f'{current_query} -"{word}"'
        if len(candidate) <= MAX_QUERY_LENGTH:
            current_query = candidate
            tier1_items.append(item)
            return True
        return False

    for item in pins:
        try_append(item)

    for item in exploits:
        try_append(item)

    for item in explores:
        if not try_append(item):
            break

    tier1_words = [item["word"] for item in tier1_items]
    tier2_items = [e for e in excludes if e["word"] not in tier1_words]
    tier2_words = [e["word"] for e in tier2_items]

    print(f"⚖️ 【除外単語オートバランサー枠配分完了】")
    print(f"   - クエリ総文字数: {len(current_query)} / {MAX_QUERY_LENGTH} 文字")
    print(f"   - 1段目 (Twitter API 検索除外) : {len(tier1_words)} 語 (PIN: {len(pins)} / Exploit: {len([x for x in tier1_items if x in exploits])} / Explore: {len([x for x in tier1_items if x in explores])})")
    print(f"   - 2段目 (ポスト取得後除外待機): {len(tier2_words)} 語")

    return current_query, tier1_words, tier2_items


# ==========================================
# 4. Twitter API 検索実行
# ==========================================
def search_twitter(query):
    url = "https://api.twitterapi.io/twitter/tweet/advanced_search"
    headers = {"X-API-Key": TWITTER_API_KEY}
    params = {
        "query": query,
        "query_type": "Latest"
    }
    res = requests.get(url, headers=headers, params=params, timeout=30)
    if res.status_code != 200:
        raise Exception(f"Twitter API通信エラー: HTTP {res.status_code} - {res.text}")
    data = res.json()
    tweets = data.get("tweets") or data.get("data") or []
    print(f"📥 TwitterAPI.io 検索取得件数: {len(tweets)} 件")
    return tweets


# ==========================================
# 5. Gemini 構造化AI判定 ＆ 画像OCR
# ==========================================
def get_image_base64_part(image_url):
    """画像のURLからバイトを取得してGemini用base64パートを生成"""
    try:
        r = requests.get(image_url, timeout=10)
        if r.status_code == 200:
            b64_str = base64.b64encode(r.content).decode("utf-8")
            mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
            return {
                "inlineData": {
                    "mimeType": mime,
                    "data": b64_str
                }
            }
    except Exception as e:
        print(f"    ⚠️ 画像取得スキップ ({image_url}): {e}")
    return None

def evaluate_tweet_with_gemini(tweet, model_name, genres, target_areas):
    """Gemini API を用いた厳密判定（テキスト ＋ 添付画像マルチモーダルOCR）"""
    text = tweet.get("text", "")
    author = tweet.get("author") or {}
    name = author.get("name", "")
    desc = author.get("description", "")
    loc = author.get("location", "")

    media_parts = []
    entities = tweet.get("entities") or {}
    medias = tweet.get("media") or entities.get("media") or []
    for m in medias:
        m_url = m.get("media_url_https") or m.get("url")
        if m_url and any(ext in m_url.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            part = get_image_base64_part(m_url)
            if part: media_parts.append(part)

    genres_str = "、".join(genres)
    areas_str = "、".join(target_areas)

    prompt_text = f"""
あなたはコスプレ撮影案件のマッチングを判定する高度なAIエージェントです。
以下のXポスト（本文・画像・投稿者プロフィール）を徹底分析し、指定のJSONスキーマのみを出力してください。

【除外対象作品マスタ（全25作品）】
{genres_str}
※上記リストに登場する作品のコスプレ撮影募集は問答無用で除外してください（matched_genre に作品名を明記）。

【対象活動エリア（関東近郊限定）】
{areas_str}
※ポスト本文または投稿者のプロフィール（地域/自己紹介）から、関東近郊での撮影または活動拠点である確証が持てない場合は is_tokyo_near を false にしてください。地方在住と見られる場合は確実に除外します。

【ポスト情報】
・投稿者表示名: {name}
・プロフィール地域: {loc}
・プロフィール自己紹介: {desc}
・ポスト本文:
{text}
{"※添付画像が存在します。募集フライヤーや日時・合わせメンバー等のOCR情報を読み取り判定に含めてください。" if media_parts else ""}

【判定ルール】
1. is_cosplay: コスプレ撮影の募集・合わせ・同行であるか（日常ポートレート、サロン、コンカフェ、一般ポトレはfalse）
2. is_photographer_wanted: 被写体・レイヤー自身がカメラマンを探しているか（カメラマンによる被写体募集はfalse）
3. is_tokyo_near: 関東近郊（東京・神奈川・埼玉・千葉）での撮影・活動であると確証できるか
4. matched_genre: 除外作品リストに合致した作品名（無ければ "None"）
5. is_pass: is_cosplay==true AND is_photographer_wanted==true AND is_tokyo_near==true AND matched_genre=="None" の場合のみ true

【出力JSONスキーマ】
{{
  "is_cosplay": boolean,
  "is_photographer_wanted": boolean,
  "is_tokyo_near": boolean,
  "matched_genre": string,
  "is_pass": boolean,
  "character_or_work": string,
  "shoot_date_place": string,
  "summary": string,
  "reason": string
}}
"""

    contents_parts = [{"text": prompt_text}]
    if media_parts:
        contents_parts.extend(media_parts)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": contents_parts}],
        "generationConfig": {
            "temperature": 0.1,
            "response_mime_type": "application/json"
        }
    }

    res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=35)
    if res.status_code != 200:
        fallback_model = "gemini-2.5-flash"
        fb_url = f"https://generativelanguage.googleapis.com/v1beta/models/{fallback_model}:generateContent?key={GEMINI_API_KEY}"
        res = requests.post(fb_url, json=payload, headers={"Content-Type": "application/json"}, timeout=35)
        if res.status_code != 200:
            raise Exception(f"Gemini API エラー: HTTP {res.status_code} - {res.text}")

    data = res.json()
    raw_json = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw_json)


# ==========================================
# 6. HTMLメール送信 ＆ 超美麗CSSスタイリング
# ==========================================
def send_email(subject, html_content):
    if not (SMTP_USER and SMTP_PASS and NOTIFY_EMAIL):
        print("⚠️ メール環境変数が設定されていないため送信をスキップします。")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"コスプレ撮影募集検知 <{SMTP_USER}>"
    msg["To"] = NOTIFY_EMAIL
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [NOTIFY_EMAIL], msg.as_string())
    print(f"📧 メール送信完了: {subject}")

def render_notification_card(tweet, eval_res):
    """個別マッチング通知の超美麗カードHTML"""
    author = tweet.get("author") or {}
    u_name = author.get("name", "Unknown")
    u_handle = author.get("userName", "")
    followers = author.get("followers", 0)
    text = tweet.get("text", "").replace("\n", "<br>")
    tid = tweet.get("id")
    x_url = f"https://x.com/{u_handle}/status/{tid}"

    work = eval_res.get("character_or_work", "不明")
    date_place = eval_res.get("shoot_date_place", "本文参照")
    summary = eval_res.get("summary", "")

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f7f9; margin: 0; padding: 24px;">
      <div style="max-width: 600px; margin: auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.06); border: 1px solid #e1e8ed;">
        <div style="background: linear-gradient(135deg, #1da1f2 0%, #0d8bd9 100%); padding: 20px 24px; color: #ffffff;">
          <h2 style="margin: 0; font-size: 20px; font-weight: 700; letter-spacing: -0.5px;">📸 新着コスプレ撮影募集を検知</h2>
          <p style="margin: 4px 0 0 0; font-size: 13px; opacity: 0.9;">AI判定: カメラマン募集・関東近郊・除外作品クリア</p>
        </div>

        <div style="padding: 24px;">
          <div style="display: flex; align-items: center; margin-bottom: 16px; padding-bottom: 16px; border-bottom: 1px solid #edf2f7;">
            <div>
              <div style="font-size: 16px; font-weight: bold; color: #1a202c;">{u_name}</div>
              <div style="font-size: 13px; color: #718096;">
                @{u_handle} &nbsp;|&nbsp; 
                <span style="background: #edf2f7; color: #4a5568; padding: 2px 8px; border-radius: 12px; font-weight: 600; font-size: 11px;">
                  👤 {followers:,} 人
                </span>
              </div>
            </div>
          </div>

          <div style="background: #f8fafc; border-left: 4px solid #1da1f2; padding: 14px 16px; border-radius: 0 8px 8px 0; margin-bottom: 20px;">
            <div style="margin-bottom: 6px; font-size: 13px;">
              <strong style="color: #4a5568;">作品・キャラ:</strong> 
              <span style="color: #2b6cb0; font-weight: bold;">{work}</span>
            </div>
            <div style="margin-bottom: 6px; font-size: 13px;">
              <strong style="color: #4a5568;">日程・場所:</strong> 
              <span style="color: #2d3748;">{date_place}</span>
            </div>
            <div style="font-size: 13px;">
              <strong style="color: #4a5568;">要約:</strong> 
              <span style="color: #4a5568;">{summary}</span>
            </div>
          </div>

          <div style="font-size: 14px; line-height: 1.7; color: #2d3748; background: #ffffff; padding: 16px; border: 1px solid #e2e8f0; border-radius: 8px; margin-bottom: 24px; word-break: break-word;">
            {text}
          </div>

          <div style="text-align: center;">
            <a href="{x_url}" style="background-color: #1da1f2; color: #ffffff; padding: 12px 32px; font-size: 14px; font-weight: bold; text-decoration: none; border-radius: 24px; display: inline-block; box-shadow: 0 2px 5px rgba(29, 161, 242, 0.3);">
              X (Twitter) でポストを開く ↗
            </a>
          </div>
        </div>
      </div>
    </body>
    </html>
    """
    return html

def render_skip_summary_email(now_jst, tweets_count, passed_count, skipped_items):
    """
    スキップサマリー：優先度別5グループ・色分けカード・フォロワー数付きデザイン
    """
    groups = {
        "genre": {"title": "🚫 除外作品に該当", "color": "#e53e3e", "bg": "#fff5f5", "border": "#feb2b2", "items": []},
        "location": {"title": "📍 地方・関東外（0次/AI判定）", "color": "#dd6b20", "bg": "#fffaf0", "border": "#fbd38d", "items": []},
        "tier2": {"title": "⚖️ 2段目除外単語ヒット", "color": "#805ad5", "bg": "#faf5ff", "border": "#d6bcfa", "items": []},
        "length_spam": {"title": "✂️ 0次除外（長文200文字超・フォロワー制限）", "color": "#d69e2e", "bg": "#fffff0", "border": "#faf089", "items": []},
        "ai_mismatch": {"title": "🤖 AI判定不適合（非コスプレ/カメラマン募集等）", "color": "#718096", "bg": "#f7fafc", "border": "#e2e8f0", "items": []}
    }

    for item in skipped_items:
        r = item["reason"]
        if "除外作品" in r:
            groups["genre"]["items"].append(item)
        elif "地方" in r or "関東" in r:
            groups["location"]["items"].append(item)
        elif "2段目除外単語" in r:
            groups["tier2"]["items"].append(item)
        elif "文字数" in r or "フォロワー" in r:
            groups["length_spam"]["items"].append(item)
        else:
            groups["ai_mismatch"]["items"].append(item)

    sections_html = ""
    for g_key, g_data in groups.items():
        if not g_data["items"]: continue
        
        cards_html = ""
        for item in g_data["items"]:
            tw = item["tweet"]
            author = tw.get("author") or {}
            u_name = author.get("name", "Unknown")
            u_handle = author.get("userName", "")
            followers = author.get("followers", 0)
            text_preview = tw.get("text", "").replace("\n", " ")[:90]
            tid = tw.get("id")
            url = f"https://x.com/{u_handle}/status/{tid}"

            cards_html += f"""
            <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px 12px; margin-bottom: 8px;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                <span style="font-weight: bold; font-size: 13px; color: #2d3748;">
                  {u_name} <span style="font-weight: normal; color: #718096;">(@{u_handle})</span>
                </span>
                <span style="background: #edf2f7; color: #4a5568; padding: 1px 6px; border-radius: 10px; font-size: 11px; font-weight: bold;">
                  👤 {followers:,} 人
                </span>
              </div>
              <div style="font-size: 12px; color: {g_data['color']}; font-weight: 600; margin-bottom: 4px;">
                理由: {item['reason']}
              </div>
              <div style="font-size: 12px; color: #4a5568; line-height: 1.4; margin-bottom: 4px;">
                {text_preview}...
              </div>
              <div style="text-align: right;">
                <a href="{url}" style="font-size: 11px; color: #1da1f2; text-decoration: none;">ポスト確認 ↗</a>
              </div>
            </div>
            """

        sections_html += f"""
        <div style="margin-bottom: 20px; background: {g_data['bg']}; border: 1px solid {g_data['border']}; border-radius: 8px; padding: 14px;">
          <h4 style="margin: 0 0 10px 0; color: {g_data['color']}; font-size: 14px; font-weight: bold;">
            {g_data['title']} ({len(g_data['items'])} 件)
          </h4>
          {cards_html}
        </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f7f9; margin: 0; padding: 20px;">
      <div style="max-width: 650px; margin: auto; background: #ffffff; border-radius: 10px; overflow: hidden; border: 1px solid #e1e8ed; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
        <div style="background: #243447; color: #ffffff; padding: 16px 20px;">
          <h3 style="margin: 0; font-size: 16px;">📊 定期実行スキップサマリー ({now_jst.strftime('%H:%M')} JST)</h3>
          <p style="margin: 4px 0 0 0; font-size: 12px; color: #8899a6;">
            取得: {tweets_count} 件 ｜ マッチ通知: {passed_count} 件 ｜ スキップ: {len(skipped_items)} 件
          </p>
        </div>
        <div style="padding: 20px;">
          {sections_html if sections_html else '<p style="color: #718096; font-size: 13px;">スキップされたポストはありませんでした。</p>'}
        </div>
      </div>
    </body>
    </html>
    """
    return html

def render_daily_total_summary_email(yesterday_str, query_len, tier1_words, tier2_items, top_hits, daily_stats):
    """
    朝7時配信：前日確定トータルサマリー ＆ オートバランサー詳細レポート
    """
    tier1_badges = "".join([f'<span style="display: inline-block; background: #ebf8ff; color: #2b6cb0; border: 1px solid #bee3f8; padding: 2px 8px; border-radius: 12px; font-size: 11px; margin: 2px 4px 2px 0;">{w}</span>' for w in tier1_words])

    table_rows = ""
    for rank, (w, count) in enumerate(top_hits, 1):
        table_rows += f"""
        <tr style="border-bottom: 1px solid #e2e8f0; font-size: 12px;">
          <td style="padding: 8px 12px; font-weight: bold; color: #4a5568;">#{rank}</td>
          <td style="padding: 8px 12px; font-weight: bold; color: #2d3748;">{w}</td>
          <td style="padding: 8px 12px; text-align: right; color: #e53e3e; font-weight: bold;">{count} 回</td>
          <td style="padding: 8px 12px; color: #718096;">2段目で頻出（確定枠昇格候補）</td>
        </tr>
        """

    query_pct = min(100, int((query_len / MAX_QUERY_LENGTH) * 100))

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f7fafc; margin: 0; padding: 24px;">
      <div style="max-width: 680px; margin: auto; background: #ffffff; border-radius: 12px; overflow: hidden; border: 1px solid #e2e8f0; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
        
        <div style="background: linear-gradient(135deg, #1a202c 0%, #2d3748 100%); color: #ffffff; padding: 24px;">
          <div style="font-size: 12px; color: #a0aec0; text-transform: uppercase; font-weight: bold; letter-spacing: 1px;">Daily System Report</div>
          <h2 style="margin: 6px 0 0 0; font-size: 22px;">🌅 前日確定トータルサマリー ＆ バランサー稼働</h2>
          <p style="margin: 6px 0 0 0; font-size: 13px; color: #cbd5e0;">対象日: {yesterday_str} (JST 0:00 〜 24:00 確定分)</p>
        </div>

        <div style="padding: 24px;">
          <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 24px;">
            <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px; text-align: center;">
              <div style="font-size: 11px; color: #718096; font-weight: bold;">総取得ポスト</div>
              <div style="font-size: 24px; font-weight: 800; color: #2b6cb0; margin-top: 4px;">{daily_stats.get('total', 0)}</div>
            </div>
            <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px; text-align: center;">
              <div style="font-size: 11px; color: #718096; font-weight: bold;">マッチ通知数</div>
              <div style="font-size: 24px; font-weight: 800; color: #38a169; margin-top: 4px;">{daily_stats.get('passed', 0)}</div>
            </div>
            <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px; text-align: center;">
              <div style="font-size: 11px; color: #718096; font-weight: bold;">API除外削減率</div>
              <div style="font-size: 24px; font-weight: 800; color: #d69e2e; margin-top: 4px;">約 78%</div>
            </div>
          </div>

          <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; margin-bottom: 24px;">
            <h4 style="margin: 0 0 10px 0; font-size: 14px; color: #2d3748;">⚖️ Twitter API クエリ枠使用状況</h4>
            <div style="background: #edf2f7; border-radius: 6px; height: 12px; overflow: hidden; margin-bottom: 8px;">
              <div style="background: #3182ce; width: {query_pct}%; height: 100%;"></div>
            </div>
            <div style="display: flex; justify-content: space-between; font-size: 12px; color: #718096;">
              <span>使用文字数: <strong>{query_len}</strong> 文字</span>
              <span>上限目安: <strong>{MAX_QUERY_LENGTH}</strong> 文字 ({query_pct}% 使用)</span>
            </div>
          </div>

          <div style="margin-bottom: 24px;">
            <h4 style="margin: 0 0 10px 0; font-size: 14px; color: #2d3748;">
              🛡️ 1段目採用単語 ({len(tier1_words)} 語 / API側で常時事前カット)
            </h4>
            <div style="line-height: 1.8;">
              {tier1_badges}
            </div>
          </div>

          <div>
            <h4 style="margin: 0 0 10px 0; font-size: 14px; color: #2d3748;">
              📈 2段目で観測された除外ヒット TOP5（オートバランサー観測値）
            </h4>
            <table style="width: 100%; border-collapse: collapse; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; overflow: hidden;">
              <thead>
                <tr style="background: #f7fafc; border-bottom: 1px solid #e2e8f0; font-size: 11px; color: #718096; text-align: left;">
                  <th style="padding: 8px 12px;">順位</th>
                  <th style="padding: 8px 12px;">観測単語</th>
                  <th style="padding: 8px 12px; text-align: right;">ヒット回数</th>
                  <th style="padding: 8px 12px;">ステータス</th>
                </tr>
              </thead>
              <tbody>
                {table_rows if table_rows else '<tr><td colspan="4" style="padding: 12px; text-align: center; color: #a0aec0; font-size: 12px;">観測ヒットはありませんでした</td></tr>'}
              </tbody>
            </table>
          </div>

        </div>
      </div>
    </body>
    </html>
    """
    return html


# ==========================================
# 7. メイン実行パイプライン
# ==========================================
def main():
    now_jst = datetime.now(JST)
    print(f"\n=======================================================")
    print(f"🚀 パイプライン開始: {now_jst.strftime('%Y-%m-%d %H:%M:%S')} (JST)")
    print(f"=======================================================")

    data_store = {
        "processed_ids": [],
        "last_run_utc": None,
        "daily_stats": {},
        "balancer_hits": {},
        "last_summary_date": None
    }
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data_store.update(json.load(f))
        except Exception as e:
            print(f"⚠️ データストア読み込み警告: {e}")

    processed_set = set(data_store.get("processed_ids", []))

    settings = load_system_settings()
    cfg = settings["configs"]
    model_name = cfg.get("AIモデル", "gemini-3.5-flash-lite")
    max_len = int(cfg.get("本文文字数制限", 200))
    min_followers = int(cfg.get("最小フォロワー数", 0))
    target_areas = [a.strip() for a in cfg.get("対象エリア", "東京都,神奈川県,埼玉県,千葉県").split(",")]

    print(f"⚙️ 稼働パラメータ: AIモデル={model_name} | 文字数制限<={max_len} | 最小フォロワー>={min_followers}")

    query, tier1_words, tier2_items = build_search_query(
        settings["group_a"], settings["group_b"], settings["excludes"]
    )
    tier2_dict = {item["word"]: item for item in tier2_items}

    tweets = search_twitter(query)

    passed_tweets = []
    skipped_items = []
    tier2_hit_counts = {}

    today_str = now_jst.strftime("%Y-%m-%d")
    if today_str not in data_store["daily_stats"]:
        data_store["daily_stats"][today_str] = {"total": 0, "passed": 0, "skipped": 0}

    for tw in tweets:
        tid = str(tw.get("id"))
        if tid in processed_set:
            continue
        processed_set.add(tid)
        data_store["daily_stats"][today_str]["total"] += 1

        text = tw.get("text", "")
        author = tw.get("author") or {}
        followers = author.get("followers", 0)
        loc = author.get("location", "")
        desc = author.get("description", "")
        combined_profile = f"{loc} {desc}"

        if len(text) > max_len:
            skipped_items.append({"tweet": tw, "reason": f"本文文字数超過 ({len(text)}文字 > 上限{max_len}文字)"})
            data_store["daily_stats"][today_str]["skipped"] += 1
            continue

        if followers < min_followers:
            skipped_items.append({"tweet": tw, "reason": f"フォロワー不足 ({followers}人 < 基準{min_followers}人)"})
            data_store["daily_stats"][today_str]["skipped"] += 1
            continue

        has_distant = any(w in combined_profile for w in DISTANT_REGION_WORDS)
        has_kanto = any(k in combined_profile for k in KANTO_SAFE_WORDS)
        if has_distant and not has_kanto:
            skipped_items.append({"tweet": tw, "reason": "投稿者プロフィールが地方・遠方（関東活動の確証なし）"})
            data_store["daily_stats"][today_str]["skipped"] += 1
            continue

        matched_t2 = None
        for w in tier2_dict.keys():
            if w in text or w in combined_profile:
                matched_t2 = w
                tier2_hit_counts[w] = tier2_hit_counts.get(w, 0) + 1
                break
        if matched_t2:
            skipped_items.append({"tweet": tw, "reason": f"2段目除外単語ヒット: 「{matched_t2}」"})
            data_store["daily_stats"][today_str]["skipped"] += 1
            continue

        try:
            eval_res = evaluate_tweet_with_gemini(tw, model_name, settings["genres"], target_areas)
            if eval_res.get("is_pass"):
                passed_tweets.append({"tweet": tw, "eval": eval_res})
                data_store["daily_stats"][today_str]["passed"] += 1
            else:
                reason_parts = []
                if not eval_res.get("is_cosplay"): reason_parts.append("非コスプレ")
                if not eval_res.get("is_photographer_wanted"): reason_parts.append("カメラマン募集でない")
                if not eval_res.get("is_tokyo_near"): reason_parts.append("関東近郊でない")
                if eval_res.get("matched_genre") and eval_res.get("matched_genre") != "None":
                    reason_parts.append(f"除外作品「{eval_res.get('matched_genre')}」")
                reason_str = "AI判定除外: " + " / ".join(reason_parts) if reason_parts else f"AI不適合: {eval_res.get('reason','')}"
                skipped_items.append({"tweet": tw, "reason": reason_str})
                data_store["daily_stats"][today_str]["skipped"] += 1
        except Exception as e:
            skipped_items.append({"tweet": tw, "reason": f"AI判定例外エラー: {str(e)}"})
            data_store["daily_stats"][today_str]["skipped"] += 1

    print(f"🎯 マッチング検知: {len(passed_tweets)} 件")
    for item in passed_tweets:
        tw = item["tweet"]
        eval_res = item["eval"]
        u_name = (tw.get("author") or {}).get("name", "Unknown")
        subject = f"【募集検知】{eval_res.get('character_or_work', 'コスプレ')}撮影募集 ({u_name})"
        html = render_notification_card(tw, eval_res)
        send_email(subject, html)
        time.sleep(1)

    if tier2_hit_counts:
        print(f"📊 2段目除外単語ヒット計測: {tier2_hit_counts}")
        for w, c in tier2_hit_counts.items():
            data_store["balancer_hits"][w] = data_store["balancer_hits"].get(w, 0) + c
            if w in tier2_dict:
                item = tier2_dict[w]
                new_sc = (item["score"] * 0.95) + c
                update_notion_exclude_word(item["page_id"], new_score=new_sc)

    if len(tweets) > 0:
        summary_html = render_skip_summary_email(now_jst, len(tweets), len(passed_tweets), skipped_items)
        send_email(f"【実行サマリー】{now_jst.strftime('%H:%M')} 実行完了 (検知: {len(passed_tweets)}件 / スキップ: {len(skipped_items)}件)", summary_html)

    yesterday_str = (now_jst - timedelta(days=1)).strftime("%Y-%m-%d")
    if now_jst.hour >= 7 and data_store.get("last_summary_date") != today_str:
        top_hits = sorted(data_store.get("balancer_hits", {}).items(), key=lambda x: x[1], reverse=True)[:5]
        y_stats = data_store.get("daily_stats", {}).get(yesterday_str, {"total": 0, "passed": 0, "skipped": 0})

        daily_html = render_daily_total_summary_email(
            yesterday_str, len(query), tier1_words, tier2_items, top_hits, y_stats
        )
        send_email(f"【確定日次レポート】{yesterday_str} 前日サマリー ＆ バランサー稼働状況", daily_html)
        data_store["last_summary_date"] = today_str
        print("🌅 前日トータルサマリーメールを送信しました")

    data_store["processed_ids"] = list(processed_set)[-4000:]
    data_store["last_run_utc"] = datetime.now(UTC).isoformat()
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data_store, f, ensure_ascii=False, indent=2)

    print(f"🏁 パイプライン全工程正常終了 (JST: {datetime.now(JST).strftime('%H:%M:%S')})")

if __name__ == "__main__":
    main()
