import os
import sys
import json
import time
import re
import requests
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai
from google.genai import types

MAX_QUERY_LENGTH = 440
NOTION_CACHE_FILE = "notion_cache.json"

# ==========================================
# Notion 4表 連携 ＆ 3重フェイルセーフ
# ==========================================
def fetch_notion_db_records(db_id, api_key):
    if not db_id or not api_key: return []
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    headers = {"Authorization": f"Bearer {api_key}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    results, has_more, next_cursor = [], True, None
    while has_more:
        payload = {"start_cursor": next_cursor} if next_cursor else {}
        res = requests.post(url, headers=headers, json=payload, timeout=20)
        if res.status_code != 200:
            raise Exception(f"Notion DB取得失敗 [{db_id}]: HTTP {res.status_code} - {res.text}")
        data = res.json()
        results.extend(data.get("results", []))
        has_more = data.get("has_more", False)
        next_cursor = data.get("next_cursor")
    return results

def get_notion_prop_str(prop):
    if not prop: return ""
    p_type = prop.get("type")
    if p_type == "title": return "".join([t.get("plain_text", "") for t in prop.get("title", [])]).strip()
    if p_type == "rich_text": return "".join([t.get("plain_text", "") for t in prop.get("rich_text", [])]).strip()
    if p_type == "select" and prop.get("select"): return prop["select"].get("name", "").strip()
    return ""

def load_integrated_config():
    api_key = os.environ.get("NOTION_API_KEY")
    kw_id, ex_id, gn_id, cfg_id = os.environ.get("NOTION_KEYWORDS_DB_ID"), os.environ.get("NOTION_EXCLUDES_DB_ID"), os.environ.get("NOTION_GENRES_DB_ID"), os.environ.get("NOTION_CONFIG_DB_ID")
    
    api_error_msg = None
    if not (api_key and kw_id and ex_id and gn_id and cfg_id):
        missing = []
        if not api_key: missing.append("NOTION_API_KEY")
        if not kw_id: missing.append("NOTION_KEYWORDS_DB_ID")
        if not ex_id: missing.append("NOTION_EXCLUDES_DB_ID")
        if not gn_id: missing.append("NOTION_GENRES_DB_ID")
        if not cfg_id: missing.append("NOTION_CONFIG_DB_ID")
        api_error_msg = f"環境変数不足: {', '.join(missing)}"
        print(f"⚠️ Notion環境変数が不足しています: {api_error_msg}")
    else:
        try:
            print("🔄 Notion 4表から最新設定を同期中...")
            raw_kw = fetch_notion_db_records(kw_id, api_key)
            raw_ex = fetch_notion_db_records(ex_id, api_key)
            raw_gn = fetch_notion_db_records(gn_id, api_key)
            raw_cfg = fetch_notion_db_records(cfg_id, api_key)

            group_a, group_b = [], []
            for r in raw_kw:
                p = r["properties"]
                if not p.get("有効", {}).get("checkbox", False): continue
                word = get_notion_prop_str(p.get("単語"))
                grp = get_notion_prop_str(p.get("グループ"))
                if grp == "撮影者(A)" and word: group_a.append(word)
                elif grp == "募集語(B)" and word: group_b.append(word)

            excludes = []
            for r in raw_ex:
                p = r["properties"]
                if not p.get("有効", {}).get("checkbox", False): continue
                word = get_notion_prop_str(p.get("単語"))
                if not word: continue
                cat = get_notion_prop_str(p.get("カテゴリ"))
                is_pin = p.get("1段目固定(PIN)", {}).get("checkbox", False)
                stat = get_notion_prop_str(p.get("ステータス"))
                sc = p.get("観測スコア(件/日)", {}).get("number")
                excludes.append({
                    "page_id": r["id"], "word": word, "category": cat or "その他",
                    "is_pin": is_pin, "status": stat or ("確定枠(1段目)" if is_pin else "2段目待機"),
                    "score": float(sc) if sc is not None else 0.0
                })

            genres = [get_notion_prop_str(r["properties"].get("作品名 / 略称")) for r in raw_gn if r["properties"].get("有効", {}).get("checkbox", False) and get_notion_prop_str(r["properties"].get("作品名 / 略称"))]
            configs = {get_notion_prop_str(r["properties"].get("設定項目")): get_notion_prop_str(r["properties"].get("設定値")) for r in raw_cfg if r["properties"].get("有効", {}).get("checkbox", False)}

            display_keywords = [f"{a} AND {b}" for a in group_a for b in group_b]
            target_areas = [a.strip() for a in configs.get("対象エリア", "東京都,神奈川県,埼玉県,千葉県").split(",")]

            result = {
                "group_a": group_a, "group_b": group_b, "display_keywords": display_keywords,
                "excludes": excludes, "genres": genres, "ai_model": configs.get("AIモデル", "gemini-3.5-flash-lite"),
                "max_text_length": int(configs.get("本文文字数制限", 200)), "min_followers_count": int(configs.get("最小フォロワー数", 0)),
                "target_areas": target_areas, "cached_at": datetime.now(timezone.utc).isoformat(),
                "config_source": "notion_api",
                "config_status_badge": "🟢 Notion API 正常取得 (リアルタイム反映)",
                "config_status_detail": f"表1: {len(group_a)+len(group_b)}語 / 表2: {len(excludes)}語 / 表3: {len(genres)}作品 / 表4: {len(configs)}項目 を同期"
            }
            with open(NOTION_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print("✅ Notion 4表同期成功＆キャッシュ更新完了")
            return result
        except Exception as e:
            api_error_msg = str(e)
            print(f"⚠️ Notion API同期失敗: {e}")

    if os.path.exists(NOTION_CACHE_FILE):
        try:
            print(f"🔄 {NOTION_CACHE_FILE} から直前キャッシュをロードします。")
            with open(NOTION_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
                if isinstance(cached, dict) and "group_a" in cached:
                    cached_at = cached.get("cached_at", "日時不明")
                    cached["config_source"] = "cache"
                    cached["config_status_badge"] = "🟡 Notion API失敗 ➔ 直前キャッシュ復旧"
                    cached["config_status_detail"] = f"キャッシュ日時: {cached_at} (Notion失敗: {api_error_msg or '詳細不明'})"
                    return cached
        except Exception as ce:
            print(f"⚠️ キャッシュ読み込み失敗: {ce}")

    print("⚠️ config.json から縮退運転を開始します。")
    with open("config.json", "r", encoding="utf-8") as f:
        old_cfg = json.load(f)
        return {
            "group_a": ["カメラマン", "撮影してくださる", "撮影してくれる", "撮ってくださる", "撮ってくれる"],
            "group_b": ["募集", "探しています", "さがしています", "急募", "ゆるぼ", "ゆる募", "いらっしゃいませんか", "いませんか"],
            "display_keywords": old_cfg.get("display_keywords", []),
            "excludes": [{"page_id": None, "word": w, "category": "旧単語", "is_pin": True, "status": "確定枠(1段目)", "score": 0.0} for w in old_cfg.get("blacklist_words", [])],
            "genres": ["忍たま乱太郎", "刀剣乱舞", "あんさんぶるスターズ", "桃源暗鬼", "イナズマイレブン", "ツイステッドワンダーランド", "ドクターストーン", "アイドリッシュセブン", "ヒプノシスマイク", "東京リベンジャーズ", "ワールドトリガー", "呪術廻戦", "ブルーロック", "A3!", "ゴールデンカムイ", "ペルソナ5", "ペルソナ4", "ペルソナ3", "鬼灯の冷徹", "銀魂", "HUNTER×HUNTER", "ワンピース", "ポケモン", "ディズニー", "鬼滅の刃"],
            "ai_model": "gemini-3.5-flash-lite", "max_text_length": old_cfg.get("max_text_length", 200),
            "min_followers_count": old_cfg.get("min_followers_count", 0), "target_areas": old_cfg.get("target_areas", ["東京都", "神奈川県", "埼玉県", "千葉県"]),
            "config_source": "local_fallback",
            "config_status_badge": "🔴 Notion/キャッシュ不可 ➔ config.json 縮退運転",
            "config_status_detail": f"ローカル初期設定で稼働中 (Notion失敗: {api_error_msg or 'Notion API未実行'})"
        }

def build_auto_balancer_query(group_a, group_b, excludes):
    str_a = " OR ".join([f'"{w}"' for w in group_a])
    str_b = " OR ".join([f'"{w}"' for w in group_b])
    base_query = f"(({str_a}) ({str_b}))"

    pins = [e for e in excludes if e.get("is_pin")]
    exploits = sorted([e for e in excludes if not e.get("is_pin") and e.get("score", 0) > 0], key=lambda x: x.get("score", 0), reverse=True)
    explores = [e for e in excludes if not e.get("is_pin") and e.get("score", 0) == 0]

    tier1_items, current_query = [], base_query
    def try_append(item):
        nonlocal current_query
        candidate = f'{current_query} -"{item["word"]}"'
        if len(candidate) <= MAX_QUERY_LENGTH:
            current_query = candidate
            tier1_items.append(item)
            return True
        return False

    for item in pins: try_append(item)
    for item in exploits: try_append(item)
    for item in explores:
        if not try_append(item): break

    tier1_words = [item["word"] for item in tier1_items]
    tier2_items = [e for e in excludes if e["word"] not in tier1_words]
    print(f"⚖️ 【除外単語オートバランサー】クエリ長: {len(current_query)} / {MAX_QUERY_LENGTH} 文字")
    print(f"   - 1段目 (クエリ除外) : {len(tier1_words)} 語 (PIN: {len(pins)} / Exploit: {len([x for x in tier1_items if x in exploits])} / Explore: {len([x for x in tier1_items if x in explores])})")
    print(f"   - 2段目 (取得後待機) : {len(tier2_items)} 語")
    return current_query, tier1_words, tier2_items

def update_notion_exclude_scores(tier2_items, tier2_hit_counts):
    api_key = os.environ.get("NOTION_API_KEY")
    if not api_key: return
    tier2_map = {item["word"]: item for item in tier2_items if item.get("page_id")}
    for word, hits in tier2_hit_counts.items():
        if word in tier2_map:
            item = tier2_map[word]
            new_score = round((item.get("score", 0.0) * 0.95) + hits, 2)
            url = f"https://api.notion.com/v1/pages/{item['page_id']}"
            headers = {"Authorization": f"Bearer {api_key}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
            try: requests.patch(url, headers=headers, json={"properties": {"観測スコア(件/日)": {"number": new_score}}}, timeout=10)
            except Exception as e: print(f"⚠️ Notionスコア書き戻しエラー ({word}): {e}")

# ==========================================
# 永続化ストレージ (processed_ids.json)
# ==========================================
def load_processed_ids(filepath="processed_ids.json"):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return (set(data.get("processed_ids", [])), data.get("last_search_start_time"), data.get("last_search_end_time"), data.get("last_daily_summary_date"), data.get("daily_history", {}), data.get("balancer_hits", {}))
                elif isinstance(data, list):
                    return set(data), None, None, None, {}, {}
        except Exception as e: print(f"processed_ids.json 読み込み失敗 ({e})。新規作成します。")
    return set(), None, None, None, {}, {}

def save_processed_ids(processed_ids, last_search_start_time=None, last_search_end_time=None, last_daily_summary_date=None, daily_history=None, balancer_hits=None, filepath="processed_ids.json", max_ids=5000):
    try:
        ids_list = list(processed_ids)[-max_ids:]
        cleaned_history = {}
        if daily_history and isinstance(daily_history, dict):
            now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
            valid_dates = {(now_jst - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)}
            cleaned_history = {k: v for k, v in daily_history.items() if k in valid_dates}
        data = {
            "processed_ids": ids_list, "last_search_start_time": last_search_start_time,
            "last_search_end_time": last_search_end_time, "last_daily_summary_date": last_daily_summary_date,
            "daily_history": cleaned_history, "balancer_hits": balancer_hits or {}
        }
        with open(filepath, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e: print(f"processed_ids.json 保存失敗: {e}")

def check_env_vars():
    missing = [var for var in ["TWITTERAPI_KEY", "GEMINI_API_KEY", "GMAIL_USER", "GMAIL_APP_PASS", "TO_EMAIL"] if not os.environ.get(var)]
    if missing: raise ValueError(f"必須環境変数が不足しています: {', '.join(missing)}")

def send_email_with_retry(msg, max_retries=3):
    for attempt in range(max_retries):
        try:
            with smtplib.SMTP('smtp.gmail.com', 587, timeout=30) as server:
                server.starttls()
                server.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASS"])
                server.send_message(msg)
            return True
        except Exception as e:
            wait_time = 2 ** (attempt + 1)
            print(f"SMTPメール送信エラー (試行 {attempt + 1}/{max_retries}): {e}。{wait_time}秒後再試行...")
            if attempt < max_retries - 1: time.sleep(wait_time)
            else: raise e

def optimize_image_url(url):
    if "pbs.twimg.com/media/" in url: return f"{url.split('?')[0]}?format=jpg&name=large"
    return url

def extract_media_urls(tweet_dict):
    urls = []
    if not isinstance(tweet_dict, dict): return urls
    media_list = (tweet_dict.get("extendedEntities", {}).get("media", []) or tweet_dict.get("entities", {}).get("media", []) or tweet_dict.get("media", []) or tweet_dict.get("mediaDetails", []) or tweet_dict.get("photos", []))
    for m in media_list:
        if isinstance(m, str) and m.startswith("http"): urls.append(optimize_image_url(m))
        elif isinstance(m, dict) and m.get("type", "photo") == "photo":
            u = m.get("media_url_https") or m.get("url") or m.get("media_url")
            if u: urls.append(optimize_image_url(u))
    return list(dict.fromkeys(urls))

def detect_matched_keyword(full_text, display_keywords):
    text_lower = full_text.lower()
    for kw in display_keywords:
        kw_clean = kw.replace('"', '').replace('#', '').strip()
        if " and " in kw.lower() or " AND " in kw:
            parts = [p.replace('"', '').strip().lower() for p in re.split(r'\s+(?:and|AND)\s+', kw)]
            if all(p in text_lower for p in parts): return kw_clean
        elif kw_clean.lower() in text_lower: return kw_clean
    return "カメラマン AND 募集"

# ==========================================
# Twitter API 検索 ＆ 多層フィルタリング
# ==========================================
def fetch_tweets_from_twitterapi_io(query_str, display_keywords, tier2_items, max_text_len, min_followers, processed_ids, search_start_time, search_end_time, is_test_mode=False):
    api_key = os.environ.get("TWITTERAPI_KEY")
    url = "https://api.twitterapi.io/twitter/tweet/advanced_search"
    headers = {"X-API-Key": api_key}
    raw_tweets_all, seen_tweet_ids = [], set()
    since_stamp, until_stamp = int(search_start_time.timestamp()), int(search_end_time.timestamp())

    print(f"検索時間枠 (UTC): {search_start_time.isoformat()} 〜 {search_end_time.isoformat()}")
    print("スマートOR統合検索を実行中 (オートバランサー枠配分クエリ)...")
    
    cursor, page = None, 1
    while True:
        full_query = f"{query_str} since_time:{since_stamp} until_time:{until_stamp}"
        params = {"query": full_query, "queryType": "Latest"}
        if cursor: params["cursor"] = cursor

        tweets_raw, has_next, next_cursor = [], False, None
        for attempt in range(3):
            try:
                res = requests.get(url, headers=headers, params=params, timeout=30)
                if res.status_code == 200:
                    data = res.json()
                    tweets_raw = data.get("tweets", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    has_next = data.get("has_next_page", False) if isinstance(data, dict) else False
                    next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
                    for tw in tweets_raw:
                        tw_id = str(tw.get("id"))
                        if tw_id not in seen_tweet_ids:
                            seen_tweet_ids.add(tw_id)
                            raw_tweets_all.append(tw)
                    break
                elif res.status_code == 429: time.sleep(2 ** (attempt + 1))
                else: break
            except requests.RequestException:
                if attempt < 2: time.sleep(2 ** (attempt + 1))
                else: break

        print(f" ➔ ページ {page} 取得完了 (取得ツイート: {len(tweets_raw)}件 / 累計: {len(raw_tweets_all)}件)")
        if not has_next or not next_cursor or not tweets_raw: break
        cursor = next_cursor
        page += 1
        time.sleep(0.5)

    raw_total_count = len(raw_tweets_all)
    filtered_tweets = []
    tier2_words = [item["word"] for item in tier2_items]
    tier2_hit_counts = {}

    for tweet in raw_tweets_all:
        tweet_id = str(tweet.get("id"))
        if not is_test_mode and tweet_id in processed_ids: continue

        created_at_str = tweet.get("createdAt")
        if created_at_str:
            try:
                created_at = datetime.strptime(created_at_str, "%a %b %d %H:%M:%S %z %Y") if " +0000 " in created_at_str else datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                if created_at < search_start_time: continue
            except Exception: pass

        author = tweet.get("author", {})
        followers_count = author.get("followers", 0) or author.get("followers_count", 0)
        if followers_count < min_followers: continue

        text_raw = tweet.get("text", "")
        if len(text_raw) > max_text_len:
            print(f" ➔ 本文文字数超過 ({len(text_raw)}文字 > {max_text_len}文字) により0次除外 (ID: {tweet_id})")
            continue

        author_name = author.get("name", "")
        author_desc = author.get("description", "")
        author_loc = author.get("location", "")

        quoted = next((tweet.get(k) for k in ["quoted_tweet", "quotedTweet", "quoted_status", "quotedStatus"] if tweet.get(k) and isinstance(tweet.get(k), dict)), None)
        quoted_text = (quoted.get("text", "") or quoted.get("full_text", "")) if quoted else ""
        quoted_id = str(quoted.get("id") or quoted.get("id_str") or "") if quoted else ""
        quoted_images = extract_media_urls(quoted) if quoted else []

        reply_parent = next((tweet.get(k) for k in ["in_reply_to_status", "inReplyToTweet", "reply_parent", "parent_tweet"] if tweet.get(k) and isinstance(tweet.get(k), dict)), None)
        reply_text = (reply_parent.get("text", "") or reply_parent.get("full_text", "")) if reply_parent else ""
        reply_id = str(reply_parent.get("id") or reply_parent.get("id_str") or "") if reply_parent else ""
        reply_images = extract_media_urls(reply_parent) if reply_parent else []

        full_text_combined = f"{text_raw}\n{quoted_text}\n{reply_text}"

        # 2段目除外単語チェック（ポスト本文・引用・リプライのみ対象とし、プロフィール誤爆を防止）
        matched_t2_word = next((w for w in tier2_words if w in full_text_combined), None)
        if matched_t2_word:
            tier2_hit_counts[matched_t2_word] = tier2_hit_counts.get(matched_t2_word, 0) + 1
            print(f" ➔ 2段目除外単語ヒット ('{matched_t2_word}') によりスキップ (ID: {tweet_id})")
            continue

        matched_specific_kw = detect_matched_keyword(full_text_combined, display_keywords)
        body_images = extract_media_urls(tweet)
        combined_images = list(dict.fromkeys(quoted_images + reply_images + body_images))

        filtered_tweets.append({
            "id": tweet_id, "text": text_raw, "author_name": author_name, "author_followers": followers_count,
            "author_location": author_loc, "author_description": author_desc, "image_urls": combined_images[:1],
            "matched_keyword": matched_specific_kw, "quoted_text": quoted_text, "quoted_id": quoted_id,
            "reply_text": reply_text, "reply_id": reply_id
        })

    return filtered_tweets, raw_total_count, tier2_hit_counts

# ==========================================
# Gemini 構造化AI判定 ＆ 画像OCR
# ==========================================
def analyze_tweet_with_ai(ai_client, tweet, target_areas, genres, ai_model="gemini-3.5-flash-lite"):
    target_areas_str, genres_str = "、".join(target_areas), "、".join(genres)
    image_urls = tweet.get("image_urls", [])

    parts = []
    for idx, url in enumerate(image_urls[:1]):
        try:
            img_resp = requests.get(url, timeout=15)
            if img_resp.status_code == 200:
                ct = img_resp.headers.get("Content-Type", "")
                mime = ct.split(";")[0] if ct.startswith("image/") else 'image/jpeg'
                parts.append(types.Part.from_bytes(data=img_resp.content, mime_type=mime))
        except Exception as e: print(f"画像取得エラー ({url}): {e}")

    has_images = len(parts) > 0
    prompt_conditions = f"""
【抽出・判定条件】
1. ocr_text: 画像内に日時・場所・募集条件などの重要要項がある場合、要点のみを短文（100文字以内）で抽出（ない場合は "なし"）。
2. location: 撮影場所または投稿者の活動拠点を特定（特定できない場合は "場所不明"）。
3. is_tokyo_near: 撮影場所または投稿者の活動地域が「{target_areas_str}」のいずれかであると確証できる場合は true、それ以外（地方在住や遠方撮影）または「場所不明」は false。
4. is_cosplay: コスプレ撮影（またはコスプレ併せ・イベント）は true、ポートレート・ライブ・物撮り・日常・一般イベントは false。
5. shooting_type: なんの撮影かを分類（コスプレの場合は作品名・キャラ名記載）。
6. is_excluded_genre: コスプレ撮影の場合、除外対象ジャンル（全{len(genres)}作品）に該当するか厳格に判定（略称・隠語・キャラ名含む）。
   【除外対象作品】: {genres_str}
7. is_looking_for_photographer: カメラマン・撮影者・同行者を募集していれば true、募集していない（被写体のみ募集等）は false。
8. is_official_or_job: 企業イベント公式カメラマンや企業求人募集は true、個人募集は false。
9. is_noise: ゲーム募集・音楽ライブ・非撮影ノイズは true、それ以外は false。
"""
    text_content = f"【投稿者プロフィール】 地域: {tweet.get('author_location','')} / 自己紹介: {tweet.get('author_description','')}\n【ポスト本文】\n{tweet['text']}"
    if tweet.get("quoted_text"): text_content += f"\n\n【引用元ポスト本文】\n{tweet['quoted_text']}"
    if tweet.get("reply_text"): text_content += f"\n\n【リプライ元ポスト本文】\n{tweet['reply_text']}"

    prompt = f"以下の投稿者情報、ポスト本文および添付画像を総合解析し、指定JSON構造で抽出してください。\n\n{text_content}\n\n{prompt_conditions}"
    contents = parts + [prompt] if has_images else [prompt]

    response_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "ocr_text": types.Schema(type=types.Type.STRING), "location": types.Schema(type=types.Type.STRING),
            "is_tokyo_near": types.Schema(type=types.Type.BOOLEAN), "is_cosplay": types.Schema(type=types.Type.BOOLEAN),
            "shooting_type": types.Schema(type=types.Type.STRING), "is_excluded_genre": types.Schema(type=types.Type.BOOLEAN),
            "is_looking_for_photographer": types.Schema(type=types.Type.BOOLEAN), "is_official_or_job": types.Schema(type=types.Type.BOOLEAN),
            "is_noise": types.Schema(type=types.Type.BOOLEAN)
        },
        required=["ocr_text", "location", "is_tokyo_near", "is_cosplay", "shooting_type", "is_excluded_genre", "is_looking_for_photographer", "is_official_or_job", "is_noise"]
    )

    response, fallback_text = None, False
    for attempt in range(5):
        try:
            curr_c = [f"以下のポスト本文を解析し指定JSONで抽出してください。画像はエラーのため除外。\n\n{text_content}\n\n{prompt_conditions}"] if (fallback_text and has_images) else contents
            response = ai_client.models.generate_content(
                model=ai_model, contents=curr_c,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=response_schema)
            )
            break
        except Exception as e:
            err = str(e)
            if has_images and not fallback_text and any(k in err for k in ["400", "INVALID_ARGUMENT"]):
                fallback_text = True
                continue
            if any(k in err for k in ["429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE"]):
                m = re.search(r'retry in (\d+(\.\d+)?)s', err)
                time.sleep(int(float(m.group(1))) + 5 if m else (2 ** (attempt + 1) * 5))
            else: raise e

    if not response: raise RuntimeError("AIリトライ上限到達")
    input_tokens = getattr(response.usage_metadata, 'prompt_token_count', 0) if hasattr(response, 'usage_metadata') else 0
    output_tokens = getattr(response.usage_metadata, 'candidates_token_count', 0) if hasattr(response, 'usage_metadata') else 0

    try: analysis = json.loads(response.text.strip())
    except Exception: analysis = {"ocr_text": "なし", "location": "場所不明", "is_tokyo_near": False, "is_cosplay": False, "shooting_type": "不明", "is_excluded_genre": False, "is_looking_for_photographer": True, "is_official_or_job": False, "is_noise": False}

    loc_tag = analysis.get("location", "場所不明")
    if not analysis.get("is_tokyo_near", False) and loc_tag != "場所不明": loc_tag += " (対象外エリア)"

    return {
        "tweet_id": tweet["id"], "author_name": tweet.get("author_name", "Unknown"), "author_followers": tweet["author_followers"],
        "tweet_text": tweet["text"], "image_url": "\n  ".join(image_urls) if image_urls else "なし", "ocr_text": analysis.get("ocr_text", "なし"),
        "location": loc_tag, "raw_location": analysis.get("location", "場所不明"), "is_tokyo_near": analysis.get("is_tokyo_near", False),
        "is_cosplay": analysis.get("is_cosplay", False), "shooting_type": analysis.get("shooting_type", "不明"),
        "is_excluded_genre": analysis.get("is_excluded_genre", False), "is_looking_for_photographer": analysis.get("is_looking_for_photographer", True),
        "is_official_or_job": analysis.get("is_official_or_job", False), "is_noise": analysis.get("is_noise", False),
        "input_tokens": input_tokens, "output_tokens": output_tokens, "matched_keyword": tweet.get("matched_keyword", "不明"),
        "quoted_text": tweet.get("quoted_text", ""), "quoted_id": tweet.get("quoted_id", ""),
        "reply_text": tweet.get("reply_text", ""), "reply_id": tweet.get("reply_id", "")
    }

# ==========================================
# メール送信 ＆ 美麗HTML
# ==========================================
def send_single_email(item, is_test_mode=False, test_hours=0.0):
    msg = MIMEMultipart()
    msg['From'] = os.environ["GMAIL_USER"]
    msg['To'] = os.environ["TO_EMAIL"]
    
    shooting_type, location, followers, matched_kw = item.get("shooting_type", "撮影募集"), item.get("location", "場所不明"), item.get("author_followers", 0), item.get('matched_keyword', '不明')
    test_disp = "15分" if test_hours == 0.25 else ("30分" if test_hours == 0.5 else f"{int(test_hours) if test_hours.is_integer() else test_hours}時間")
    msg['Subject'] = f"{'【テスト実行(' + test_disp + ')/X募集】' if is_test_mode else '【X募集】'}{location}│{shooting_type} ({followers:,}人)"

    web_url = f"https://x.com/i/status/{item.get('tweet_id', '')}"
    tweet_text_clean = item.get('tweet_text', '').replace('\n', ' ').strip()
    preheader_text = f"[ワード:{matched_kw}] 「{tweet_text_clean[:70]}」"

    test_banner = f'<div style="background-color: #fff3cd; color: #856404; padding: 10px; border-radius: 6px; margin-bottom: 15px; font-weight: bold; text-align: center; border: 1px solid #ffeeba;">手動テスト実行 (直近{test_disp} 重複除外なし)</div>' if is_test_mode else ""
    extra_btns = ""
    if item.get("quoted_id"): extra_btns += f'<div style="margin-top: 10px;"><a href="https://x.com/i/status/{item.get("quoted_id")}" class="btn-secondary" target="_blank">引用元ポストをXで開く</a></div>'
    if item.get("reply_id"): extra_btns += f'<div style="margin-top: 10px;"><a href="https://x.com/i/status/{item.get("reply_id")}" class="btn-secondary" target="_blank">リプライ元（親ポスト）をXで開く</a></div>'

    html = f"""
    <!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #f4f5f7; color: #333; margin: 0; padding: 15px; }}
    .card {{ background: #fff; max-width: 580px; margin: 0 auto; border: 1px solid #e1e4e8; border-radius: 8px; padding: 20px; box-shadow: 0 4px 10px rgba(0,0,0,0.05); }}
    .header {{ font-size: 16px; font-weight: bold; color: #0366d6; background: #e2f0fd; padding: 10px 14px; border-radius: 6px; margin-bottom: 15px; border-left: 4px solid #0366d6; }}
    .badge {{ display: inline-block; background: #e2f0fd; color: #0366d6; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; margin-right: 5px; }}
    .meta-list {{ list-style: none; padding: 0; margin: 15px 0; font-size: 14px; line-height: 1.6; }}
    .ocr-box {{ background: #fafbfc; border-left: 4px solid #0366d6; padding: 12px; font-size: 13px; color: #586069; margin: 15px 0; word-break: break-all; border-radius: 0 4px 4px 0; }}
    .body-text {{ font-size: 14px; line-height: 1.6; color: #24292e; background: #fafbfc; padding: 15px; border: 1px solid #e1e4e8; border-radius: 6px; word-break: break-all; }}
    .btn {{ display: inline-block; background: #1da1f2; color: #fff !important; text-decoration: none; padding: 12px 28px; border-radius: 6px; font-weight: bold; font-size: 15px; }}
    .btn-secondary {{ display: inline-block; background: #657786; color: #fff !important; text-decoration: none; padding: 8px 16px; border-radius: 6px; font-weight: bold; font-size: 12px; }}
    </style></head><body>
    <div style="display:none;font-size:1px;color:#fff;line-height:1px;max-height:0px;max-width:0px;opacity:0;overflow:hidden;">{preheader_text}</div>
    <div class="card">
      {test_banner}
      <div class="header">{location} │ {shooting_type}</div>
      <ul class="meta-list">
        <li><strong>・投稿者フォロワー数:</strong> {followers:,} 人</li>
        <li><strong>・ヒット検索ワード:</strong> <span class="badge" style="background:#fff5b1; color:#b06000;">{matched_kw}</span></li>
        <li><strong>・撮影種別:</strong> <span class="badge">{shooting_type}</span></li>
        <li><strong>・撮影場所:</strong> <span class="badge">{location}</span> (都内近郊: <span class="badge" style="background:#e6ffed; color:#28a745;">{'○' if item.get('is_tokyo_near') else '×'}</span>)</li>
      </ul>
      <div class="ocr-box"><strong>・画像内重要文字 (OCR):</strong><div style="margin-top:5px;">{item.get('ocr_text', 'なし').replace('<','&lt;').replace('>','&gt;').replace(chr(10),'<br>')}</div></div>
      <div style="color:#999; letter-spacing:1px; font-size:11px; margin:15px 0; text-align:center; font-weight:bold;">{"-" * 50}</div>
      <div class="body-text">{item.get('tweet_text', '').replace('<','&lt;').replace('>','&gt;').replace(chr(10),'<br>')}</div>
      <div style="text-align:center; margin-top:25px;">
        <a href="{web_url}" class="btn" target="_blank">X (Twitter) で投稿を見る</a>
        {extra_btns}
      </div>
    </div></body></html>
    """
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    send_email_with_retry(msg)

def send_daily_total_summary_email(daily_stats, target_date_str, display_keywords, query_len, tier1_words, tier2_items, balancer_hits):
    msg = MIMEMultipart()
    msg['From'] = os.environ["GMAIL_USER"]
    msg['To'] = os.environ["TO_EMAIL"]
    msg['Subject'] = f"【X撮影募集】前日トータルサマリー ＆ バランサー稼働 ({target_date_str})"

    fetched, raw_count, sent, skipped, err = daily_stats.get("fetched_count", 0), daily_stats.get("raw_tweets_count", 0), daily_stats.get("sent_count", 0), daily_stats.get("skipped_count", 0), daily_stats.get("error_count", 0)
    in_tok, out_tok = daily_stats.get("input_tokens", 0), daily_stats.get("output_tokens", 0)
    total_tokens = in_tok + out_tok

    gemini_usd = ((in_tok / 1_000_000) * 0.30) + ((out_tok / 1_000_000) * 2.50)
    gemini_jpy = gemini_usd * 155.0
    twitter_credits = raw_count * 15
    twitter_usd = (twitter_credits / 1_000_000) * 10.0
    twitter_jpy = twitter_usd * 155.0
    total_jpy = gemini_jpy + twitter_jpy

    monthly_twitter_jpy = twitter_jpy * 30
    monthly_gemini_jpy = gemini_jpy * 30
    monthly_total_jpy = total_jpy * 30

    kw_stats = daily_stats.get("keyword_stats", {})
    sorted_kw = sorted([(k, v) for k, v in kw_stats.items() if v.get("fetched", 0) > 0], key=lambda x: (x[1].get("fetched", 0), x[1].get("sent", 0)), reverse=True)
    top10_html = "".join([f"<li style='margin-bottom:6px; font-size:13px;'><strong>{i+1}位 【{kw}】</strong>: 取得: <strong>{s['fetched']:,}</strong> / 送信: <span style='color:#28a745; font-weight:bold;'>{s['sent']:,}</span> / スキップ: {s['skipped']:,}</li>" for i, (kw, s) in enumerate(sorted_kw[:10])]) or "<li style='color:#586069; font-size:13px;'>・前日のヒットはありませんでした。</li>"
    all_kws_html = "".join([f"<li style='margin-bottom:4px; font-size:12.5px; color:#444d56;'>・<strong>【{kw}】</strong>: 取得: {kw_stats.get(kw,{}).get('fetched',0):,} │ 送信: {kw_stats.get(kw,{}).get('sent',0):,} │ スキップ: {kw_stats.get(kw,{}).get('skipped',0):,}</li>" for kw in display_keywords])

    top_hits = sorted(balancer_hits.items(), key=lambda x: x[1], reverse=True)[:5]
    balancer_table_rows = "".join([f"<tr style='border-bottom:1px solid #e1e4e8; font-size:12.5px;'><td style='padding:6px 10px; font-weight:bold;'>#{r}</td><td style='padding:6px 10px; font-weight:bold;'>{w}</td><td style='padding:6px 10px; text-align:right; color:#cb2431; font-weight:bold;'>{c} 回</td><td style='padding:6px 10px; color:#586069;'>2段目頻出</td></tr>" for r, (w, c) in enumerate(top_hits, 1)]) or "<tr><td colspan='4' style='padding:10px; text-align:center; color:#586069; font-size:12px;'>特筆すべき観測ヒットなし</td></tr>"
    tier1_badges = "".join([f"<span style='display:inline-block; background:#e2f0fd; color:#0366d6; padding:2px 6px; border-radius:4px; font-size:11px; font-weight:bold; margin:2px 4px 2px 0;'>{w}</span>" for w in tier1_words])

    html = f"""
    <!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #f4f5f7; color: #333; margin: 0; padding: 15px; }}
    .card {{ background: #fff; max-width: 600px; margin: 0 auto; border: 1px solid #e1e4e8; border-radius: 8px; padding: 20px; box-shadow: 0 4px 10px rgba(0,0,0,0.05); }}
    .header {{ font-size: 18px; font-weight: bold; color: #24292e; margin-bottom: 15px; border-bottom: 2px solid #0366d6; padding-bottom: 10px; }}
    .section-title {{ font-size: 14px; font-weight: bold; color: #24292e; margin-top: 20px; margin-bottom: 10px; background: #f6f8fa; padding: 6px 12px; border-radius: 4px; }}
    .stat-box-container {{ display: flex; justify-content: space-between; margin: 15px 0; }}
    .stat-box {{ flex: 1; background: #fafbfc; border: 1px solid #e1e4e8; border-radius: 6px; padding: 10px 4px; text-align: center; margin: 0 3px; }}
    .stat-num {{ font-size: 16px; font-weight: bold; color: #0366d6; margin-top: 4px; }}
    .meta-list {{ list-style: none; padding: 0; margin: 10px 0; font-size: 13px; line-height: 1.6; }}
    </style></head><body>
    <div class="card">
      <div class="header">前日トータルサマリー ({target_date_str})</div>
      <div class="section-title">■ 前日 24 時間の累計ポスト処理数</div>
      <div class="stat-box-container">
        <div class="stat-box"><div style="font-size:10px; color:#586069;">総取得数</div><div class="stat-num">{fetched:,}</div></div>
        <div class="stat-box" style="border-color:#34d058;"><div style="font-size:10px; color:#28a745;">総通知数</div><div class="stat-num" style="color:#28a745;">{sent:,}</div></div>
        <div class="stat-box"><div style="font-size:10px; color:#586069;">総スキップ</div><div class="stat-num" style="color:#6a737d;">{skipped:,}</div></div>
        <div class="stat-box" style="border-color:#f97583;"><div style="font-size:10px; color:#cb2431;">総エラー</div><div class="stat-num" style="color:#cb2431;">{err:,}</div></div>
      </div>
      <div class="section-title">■ 除外単語オートバランサー 稼働レポート</div>
      <ul class="meta-list">
        <li>・<strong>クエリ文字数:</strong> {query_len} / {MAX_QUERY_LENGTH} 文字</li>
        <li>・<strong>1段目採用 (API事前カット):</strong> {len(tier1_words)} 語</li>
        <li style="margin-top:4px;">{tier1_badges}</li>
        <li style="margin-top:8px;">・<strong>2段目待機単語数:</strong> {len(tier2_items)} 語</li>
      </ul>
      <strong style="font-size:12.5px;">▼ 2段目観測ヒット急増 TOP5:</strong>
      <table style="width:100%; border-collapse:collapse; margin-top:6px; border:1px solid #e1e4e8; background:#fafbfc;">
        <thead><tr style="background:#f1f8ff; font-size:11px; text-align:left; color:#0366d6;"><th style="padding:6px 10px;">順位</th><th style="padding:6px 10px;">単語</th><th style="padding:6px 10px; text-align:right;">観測数</th><th style="padding:6px 10px;">判定</th></tr></thead>
        <tbody>{balancer_table_rows}</tbody>
      </table>
      <div class="section-title">■ API消費量 ＆ 概算費用</div>
      <ul class="meta-list">
        <li>・<strong>TwitterAPI.io:</strong> {raw_count * 15:,} credits ({raw_count:,}件) ➔ 約 <strong>{twitter_jpy:.2f} 円</strong></li>
        <li>・<strong>Gemini AI:</strong> {total_tokens:,} tokens ➔ 約 <strong>{gemini_jpy:.2f} 円</strong></li>
        <li style="margin-top:6px; border-top:1px dashed #e1e4e8; padding-top:6px;">★ <strong>前日24時間合計: 約 <span style="color:#0366d6; font-size:15px; font-weight:bold;">{total_jpy:.2f} 円</span></strong></li>
        <li style="margin-top:4px;">★ <strong>月間換算試算: 約 <span style="color:#d73a49; font-size:15px; font-weight:bold;">{monthly_total_jpy:.2f} 円 / 月</span></strong></li>
      </ul>
      <div class="section-title">■ 前日ヒット数 TOP 10</div>
      <ul style="padding-left:20px; font-size:13px; line-height:1.6;">{top10_html}</ul>
      <details style="margin-top:14px; border:1px solid #e1e4e8; border-radius:6px; padding:10px; background:#fafbfc;">
        <summary style="font-size:13.5px; font-weight:bold; color:#0366d6; cursor:pointer;">検索単語ごとの全内訳 (全{len(display_keywords)}パターン)</summary>
        <ul style="padding-left:18px; margin-top:10px; line-height:1.5;">{all_kws_html}</ul>
      </details>
    </div></body></html>
    """
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    send_email_with_retry(msg)

def send_summary_email(summary_data, is_test_mode=False, test_hours=0.0):
    msg = MIMEMultipart()
    msg['From'] = os.environ["GMAIL_USER"]
    msg['To'] = os.environ["TO_EMAIL"]
    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    sent_count, fetched_count = summary_data["sent_count"], summary_data["fetched_count"]
    raw_tweets_count = summary_data.get("raw_tweets_count", fetched_count)
    test_disp = "15分" if test_hours == 0.25 else ("30分" if test_hours == 0.5 else f"{int(test_hours) if test_hours.is_integer() else test_hours}時間")
    msg['Subject'] = f"{'【テスト実行(' + test_disp + ')/X撮影募集】' if is_test_mode else '【X撮影募集】'}実行完了サマリー (通知: {sent_count}件 / 取得: {fetched_count}件)"

    in_tok, out_tok = summary_data["input_tokens"], summary_data["output_tokens"]
    total_tokens = in_tok + out_tok
    gemini_jpy = (((in_tok / 1_000_000) * 0.30) + ((out_tok / 1_000_000) * 2.50)) * 155.0
    twitter_credits = raw_tweets_count * 15
    twitter_jpy = ((twitter_credits / 1_000_000) * 10.0) * 155.0
    total_jpy = gemini_jpy + twitter_jpy

    period_hours = summary_data.get("period_hours", 1.0) or 1.0
    monthly_mul = (24.0 / period_hours) * 30.0
    monthly_jpy = total_jpy * monthly_mul

    display_kws = summary_data.get("display_keywords", [])
    kw_order = {kw: i for i, kw in enumerate(display_kws)}
    skipped_tweets = summary_data.get("skipped_tweets", [])

    cfg_badge = summary_data.get("config_status_badge", "不明")
    cfg_detail = summary_data.get("config_status_detail", "")
    cfg_source = summary_data.get("config_source", "")

    if "🟢" in cfg_badge or cfg_source == "notion_api":
        badge_style = "background:#e6ffed; color:#22863a; border:1px solid #34d058; padding:2px 8px; border-radius:4px; font-size:12px; font-weight:bold;"
    elif "🟡" in cfg_badge or cfg_source == "cache":
        badge_style = "background:#fff5b1; color:#735c0f; border:1px solid #d99b00; padding:2px 8px; border-radius:4px; font-size:12px; font-weight:bold;"
    else:
        badge_style = "background:#ffeef0; color:#cb2431; border:1px solid #d73a49; padding:2px 8px; border-radius:4px; font-size:12px; font-weight:bold;"

    grouped_skipped = {
        "【要確認・併せ募集】コスプレ併せ・撮影 (カメラマン募集あり・場所不明/都外判定)": [],
        "【一般撮影】ポートレート・個人撮影 (カメラマン募集あり)": [],
        "【企業・公式・求人】公式イベント / 企業雇用・スタッフ募集": [],
        "【除外ジャンル】指定除外作品 (25作品該当)": [],
        "【完全ノイズ・対象外】ゲーム募集 / 音楽ライブ / 都外確定 / カメラマン非募集": []
    }
    others = []
    for item in skipped_tweets:
        gk = item.get("group_key")
        if gk in grouped_skipped: grouped_skipped[gk].append(item)
        else: others.append(item)

    group_configs = [
        ("【要確認・併せ募集】コスプレ併せ・撮影 (カメラマン募集あり・場所不明/都外判定)", "#e36209", "#fff8f2"),
        ("【一般撮影】ポートレート・個人撮影 (カメラマン募集あり)", "#0366d6", "#f1f8ff"),
        ("【企業・公式・求人】公式イベント / 企業雇用・スタッフ募集", "#6f42c1", "#fbf0fc"),
        ("【除外ジャンル】指定除外作品 (25作品該当)", "#d73a49", "#ffeef0"),
        ("【完全ノイズ・対象外】ゲーム募集 / 音楽ライブ / 都外確定 / カメラマン非募集", "#6a737d", "#f6f8fa")
    ]

    skipped_html, total_idx, has_printed = "", 1, False
    for group_name, border_color, bg_color in group_configs:
        items = grouped_skipped[group_name]
        if items:
            items.sort(key=lambda x: (kw_order.get(x.get("matched_keyword", "不明"), 999), x.get("shooting_type", "不明"), x.get("url", "")))
            if has_printed: skipped_html += f'<div style="text-align:center; color:#0366d6; margin:18px 0; font-size:13px; font-weight:bold;">{"=" * 30}</div>'
            skipped_html += f'<div style="border-left:4px solid {border_color}; background-color:{bg_color}; padding:14px; margin-bottom:14px; border-radius:6px;"><h4 style="margin:0 0 12px 0; color:#24292e; font-size:14.5px; font-weight:bold;">■ {group_name} ({len(items)}件):</h4>'
            for item in items:
                t_safe = item.get('text', '').replace('<','&lt;').replace('>','&gt;').replace('\n','<br>')
                skipped_html += f"""
                <div style="background:#fff; border:1px solid #e1e4e8; border-radius:6px; padding:12px; margin-bottom:12px; font-size:13px; line-height:1.5;">
                  <div style="display:flex; justify-content:space-between; margin-bottom:6px;">
                    <div><strong>[{total_idx}]</strong> <span style="background:#e2f0fd; color:#0366d6; padding:2px 6px; border-radius:4px; font-size:12px; font-weight:bold;">{item.get('matched_keyword','不明')}</span> <span style="background:#edf2f7; color:#4a5568; padding:2px 6px; border-radius:4px; font-size:11px; font-weight:bold; margin-left:4px;">👤 {item.get('author_followers',0):,} 人</span></div>
                    <a href="{item.get('url','#')}" style="color:#0366d6; text-decoration:none; font-weight:bold; font-size:13px;" target="_blank">投稿を見る</a>
                  </div>
                  <div style="background:#fff9f0; border-left:3px solid {border_color}; padding:6px 10px; margin:6px 0; border-radius:0 4px 4px 0; font-size:12.5px;">
                    <strong>スキップ理由:</strong> <span style="color:#cb2431; font-weight:bold;">{item.get('detailed_reason', item.get('reason','不明'))}</span><br>
                    <strong>AI判定:</strong> 撮影種別: <strong>{item.get('shooting_type','不明')}</strong> │ 場所: <strong>{item.get('location','場所不明')}</strong><br>
                    <strong>画像OCR:</strong> {item.get('ocr_text','なし')}
                  </div>
                  <div style="margin-top:6px; color:#444d56; font-size:12.5px; background:#fafbfc; padding:8px; border-radius:4px; word-break:break-all;">{t_safe}</div>
                </div>"""
                total_idx += 1
            skipped_html += "</div>"
            has_printed = True

    if others:
        if has_printed: skipped_html += f'<div style="text-align:center; color:#0366d6; margin:18px 0; font-size:13px; font-weight:bold;">{"=" * 30}</div>'
        skipped_html += f'<div style="border-left:4px solid #d73a49; background-color:#ffeef0; padding:14px; margin-bottom:14px; border-radius:6px;"><h4 style="margin:0 0 12px 0; color:#24292e; font-size:14.5px; font-weight:bold;">■【その他】({len(others)}件):</h4>'
        for item in others:
            t_safe = item.get('text', '').replace('<','&lt;').replace('>','&gt;').replace('\n','<br>')
            skipped_html += f"""
            <div style="background:#fff; border:1px solid #e1e4e8; border-radius:6px; padding:12px; margin-bottom:12px; font-size:13px; line-height:1.5;">
              <strong>[{total_idx}]</strong> <span style="background:#e1e4e8; padding:2px 6px; border-radius:3px; font-size:12px;">{item.get('matched_keyword','不明')}</span> 
              <span style="background:#edf2f7; color:#4a5568; padding:2px 6px; border-radius:4px; font-size:11px; font-weight:bold; margin-left:4px;">👤 {item.get('author_followers',0):,} 人</span>
              <span style="color:#d73a49; font-weight:bold; font-size:12.5px; margin-left:4px;">({item.get('reason','不明')})</span>
              <a href="{item.get('url','#')}" style="color:#0366d6; text-decoration:none; font-weight:bold; font-size:13px; margin-left:8px;" target="_blank">投稿を見る</a>
              <div style="margin-top:6px; color:#586069; font-size:12.5px;">{t_safe}</div>
            </div>"""
            total_idx += 1
        skipped_html += "</div>"

    if not skipped_tweets: skipped_html = '<div style="font-size:13px; color:#586069; padding:8px 0;">・スキップされたポストはありません。</div>'
    test_banner = f'<div style="background-color:#fff3cd; color:#856404; padding:10px; border-radius:6px; margin-bottom:15px; font-weight:bold; text-align:center; border:1px solid #ffeeba;">手動テスト実行結果 (直近{test_disp} 重複除外なし)</div>' if is_test_mode else ""

    html = f"""
    <!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #f4f5f7; color: #333; margin: 0; padding: 15px; }}
    .card {{ background: #fff; max-width: 600px; margin: 0 auto; border: 1px solid #e1e4e8; border-radius: 8px; padding: 20px; box-shadow: 0 4px 10px rgba(0,0,0,0.05); }}
    .header {{ font-size: 18px; font-weight: bold; color: #24292e; margin-bottom: 15px; border-bottom: 2px solid #e1e4e8; padding-bottom: 10px; }}
    .section-title {{ font-size: 14.5px; font-weight: bold; color: #24292e; margin-top: 18px; margin-bottom: 10px; background: #f6f8fa; padding: 7px 12px; border-radius: 4px; }}
    .stat-box-container {{ display: flex; justify-content: space-between; margin: 12px 0; }}
    .stat-box {{ flex: 1; background: #fafbfc; border: 1px solid #e1e4e8; border-radius: 6px; padding: 10px 4px; text-align: center; margin: 0 3px; }}
    .stat-num {{ font-size: 17px; font-weight: bold; color: #0366d6; margin-top: 3px; }}
    .meta-list {{ list-style: none; padding: 0; margin: 10px 0; font-size: 13.5px; line-height: 1.6; }}
    </style></head><body>
    <div class="card">
      {test_banner}
      <div class="header">【X撮影募集】実行完了サマリー</div>
      <ul class="meta-list">
        <li><strong>■ 実行日時 (JST):</strong> {now_jst.strftime("%Y-%m-%d %H:%M:%S")}</li>
        <li><strong>・検索対象期間 (JST):</strong> {summary_data.get('target_period_start','不明')} 〜 {summary_data.get('target_period_end','不明')} ({period_hours:.2f}時間分)</li>
        <li><strong>・処理所要時間:</strong> {summary_data.get('duration','不明')}</li>
        <li style="margin-top:6px; padding-top:6px; border-top:1px dashed #e1e4e8;">
          <strong>・Notion設定参照:</strong> <span style="{badge_style}">{cfg_badge}</span>
          <div style="font-size:12px; color:#586069; margin-top:4px; margin-left:14px;">└ {cfg_detail}</div>
        </li>
      </ul>
      <div class="section-title">■ 全体ポスト処理件数</div>
      <div class="stat-box-container">
        <div class="stat-box"><div style="font-size:10.5px; color:#586069;">新規取得</div><div class="stat-num">{fetched_count:,}</div></div>
        <div class="stat-box" style="border-color:#34d058;"><div style="font-size:10.5px; color:#28a745;">個別通知</div><div class="stat-num" style="color:#28a745;">{sent_count:,}</div></div>
        <div class="stat-box"><div style="font-size:10.5px; color:#586069;">スキップ</div><div class="stat-num" style="color:#6a737d;">{summary_data['skipped_count']:,}</div></div>
        <div class="stat-box" style="border-color:#f97583;"><div style="font-size:10.5px; color:#cb2431;">エラー</div><div class="stat-num" style="color:#cb2431;">{summary_data['error_count']:,}</div></div>
      </div>
      <div class="section-title">■ API消費量 ＆ 概算コスト (今回)</div>
      <ul class="meta-list" style="padding-left:10px;">
        <li>・<strong>TwitterAPI.io:</strong> {twitter_credits:,} credits ({raw_tweets_count:,}件) ➔ 約 <strong>{twitter_jpy:.2f} 円</strong></li>
        <li>・<strong>Gemini AI:</strong> {total_tokens:,} tokens ➔ 約 <strong>{gemini_jpy:.2f} 円</strong></li>
        <li style="margin-top:6px; border-top:1px dashed #e1e4e8; padding-top:6px;">★ <strong>今回合計: 約 <span style="color:#0366d6; font-size:15px; font-weight:bold;">{total_jpy:.2f} 円</span></strong></li>
        <li style="margin-top:6px;">★ <strong>月間換算試算: 約 <span style="color:#d73a49; font-size:16px; font-weight:bold;">{monthly_jpy:.2f} 円 / 月</span></strong></li>
      </ul>
      <details style="margin-top:18px; border:1px solid #e1e4e8; border-radius:6px; padding:12px; background:#fafbfc;">
        <summary style="font-size:14.5px; font-weight:bold; color:#cb2431; cursor:pointer;">スキップされたポスト一覧 (計 {len(skipped_tweets)} 件)</summary>
        <div style="margin-top:14px;">{skipped_html}</div>
      </details>
    </div></body></html>
    """
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    send_email_with_retry(msg)

# ==========================================
# メイン実行関数
# ==========================================
def main():
    start_time_epoch = time.time()
    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_str = now_jst.strftime("%Y-%m-%d")
    yesterday_str = (now_jst - timedelta(days=1)).strftime("%Y-%m-%d")

    run_mode = os.environ.get("RUN_MODE", "").strip().lower()
    test_hours_str = os.environ.get("TEST_HOURS", "0")

    if "--test" in sys.argv:
        is_test_mode = True
        test_hours = 2.0
    elif "--test-hours" in sys.argv:
        is_test_mode = True
        try: test_hours = float(sys.argv[sys.argv.index("--test-hours") + 1])
        except Exception: test_hours = 2.0
    elif run_mode == "test":
        is_test_mode = True
        try: test_hours = float(test_hours_str)
        except ValueError: test_hours = 1.0
    elif run_mode == "production":
        is_test_mode = False
        test_hours = 0.0
    else:
        try: test_hours = float(test_hours_str)
        except ValueError: test_hours = 0.0
        is_test_mode = test_hours > 0.0

    print(f"🚀 稼働モード: {'【テスト実行】(遡り: ' + str(test_hours) + '時間 / ID保存なし)' if is_test_mode else '【本番実行】(差分取得 / ID永続保存あり)'}")
    check_env_vars()

    cfg_data = load_integrated_config()
    group_a, group_b = cfg_data["group_a"], cfg_data["group_b"]
    display_kws = cfg_data["display_keywords"]
    excludes, genres, ai_model = cfg_data["excludes"], cfg_data["genres"], cfg_data["ai_model"]
    max_text_len, min_followers, target_areas = cfg_data["max_text_length"], cfg_data["min_followers_count"], cfg_data["target_areas"]
    config_status_badge = cfg_data.get("config_status_badge", "不明")
    config_status_detail = cfg_data.get("config_status_detail", "")
    config_source = cfg_data.get("config_source", "")

    processed_ids, last_start_str, last_end_str, last_daily_summary_date, daily_history, balancer_hits = load_processed_ids("processed_ids.json")
    query_str, tier1_words, tier2_items = build_auto_balancer_query(group_a, group_b, excludes)

    # 朝7:00以降の初回実行時に前日確定トータルサマリー配信
    if not is_test_mode and now_jst.hour >= 7 and last_daily_summary_date != yesterday_str and yesterday_str in daily_history:
        try:
            print(f"前日 ({yesterday_str}) のトータルサマリーメールを送信中...")
            send_daily_total_summary_email(daily_history[yesterday_str], yesterday_str, display_kws, len(query_str), tier1_words, tier2_items, balancer_hits)
            last_daily_summary_date = yesterday_str
            print("前日トータルサマリー送信完了")
        except Exception as e: print(f"前日トータルサマリー送信エラー: {e}")

    now_utc = datetime.now(timezone.utc)
    search_end_time = now_utc.replace(microsecond=0)
    search_end_time_str = search_end_time.isoformat().replace("+00:00", "Z")

    if is_test_mode:
        search_start_time = now_utc - timedelta(hours=test_hours)
    else:
        if last_end_str:
            try: search_start_time = datetime.fromisoformat(last_end_str.replace("Z", "+00:00") if last_end_str.endswith("Z") else last_end_str)
            except Exception: search_start_time = now_utc - timedelta(minutes=130)
        else: search_start_time = now_utc - timedelta(minutes=130)
        if search_start_time < now_utc - timedelta(hours=24): search_start_time = now_utc - timedelta(hours=24)

    search_start_time = search_start_time.replace(microsecond=0)
    search_start_time_str = search_start_time.isoformat().replace("+00:00", "Z")

    target_start_str = (search_start_time + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
    target_end_str = (search_end_time + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
    period_hours = max((search_end_time - search_start_time).total_seconds() / 3600.0, 0.01)

    ai_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    tweets, raw_total_count, tier2_hit_counts = fetch_tweets_from_twitterapi_io(query_str, display_kws, tier2_items, max_text_len, min_followers, processed_ids, search_start_time, search_end_time, is_test_mode=is_test_mode)
    fetched_count = len(tweets)
    print(f"処理対象 of 新規ポスト: {fetched_count} 件 (API生取得: {raw_total_count} 件)")

    # オートバランサーの観測集計＆Notion更新
    for w, c in tier2_hit_counts.items(): balancer_hits[w] = balancer_hits.get(w, 0) + c
    if tier2_hit_counts: update_notion_exclude_scores(tier2_items, tier2_hit_counts)

    sent_count, skipped_count, error_count = 0, 0, 0
    total_input_tokens, total_output_tokens = 0, 0
    skipped_tweets = []
    clean_kws = [k.replace('"', '').replace('#', '').strip() for k in display_kws]
    keyword_stats = {kw: {"fetched": 0, "sent": 0, "skipped": 0, "error": 0} for kw in clean_kws}
    keyword_stats["不明"] = {"fetched": 0, "sent": 0, "skipped": 0, "error": 0}

    for tweet in tweets:
        kw = tweet.get("matched_keyword", "不明")
        if kw in keyword_stats: keyword_stats[kw]["fetched"] += 1

    for i, tweet in enumerate(tweets, 1):
        kw = tweet.get("matched_keyword", "不明")
        try:
            print(f"[{i}/{fetched_count}] Tweet ID: {tweet['id']} ({kw}) を解析中...")
            analyzed = analyze_tweet_with_ai(ai_client, tweet, target_areas, genres, ai_model=ai_model)
            total_input_tokens += analyzed.get("input_tokens", 0)
            total_output_tokens += analyzed.get("output_tokens", 0)

            is_looking = analyzed.get("is_looking_for_photographer", True)
            is_tokyo_near = analyzed.get("is_tokyo_near", False)
            is_cosplay = analyzed.get("is_cosplay", False)
            is_excluded = analyzed.get("is_excluded_genre", False)
            is_official = analyzed.get("is_official_or_job", False)
            is_noise = analyzed.get("is_noise", False)

            shooting_type = analyzed.get("shooting_type", "不明")
            loc_name = analyzed.get("raw_location", "場所不明")

            is_valid = (is_looking and is_tokyo_near and is_cosplay and not is_excluded and not is_official and not is_noise)
            if not is_valid:
                skipped_count += 1
                group_key, reason, detailed = "", "", ""
                if is_cosplay and is_looking and not is_excluded and not is_official and not is_noise and not is_tokyo_near:
                    group_key = "【要確認・併せ募集】コスプレ併せ・撮影 (カメラマン募集あり・場所不明/都外判定)"
                    reason = "エリア対象外/場所不明"
                    detailed = f"コスプレ撮影ですが対象エリア外または場所不明 (判定: {loc_name})"
                elif not is_cosplay and is_looking and not is_official and not is_noise:
                    group_key = "【一般撮影】ポートレート・個人撮影 (カメラマン募集あり)"
                    reason = "撮影種別対象外 (コスプレ以外)"
                    detailed = f"コスプレ以外の個人撮影 ({shooting_type}) / 判定場所: {loc_name}"
                elif is_official:
                    group_key = "【企業・公式・求人】公式イベント / 企業雇用・スタッフ募集"
                    reason = "企業・公式求人"
                    detailed = f"公式イベントカメラマンまたは企業・求人募集 ({shooting_type})"
                elif is_excluded:
                    group_key = "【除外ジャンル】指定除外作品 (25作品該当)"
                    reason = "除外ジャンル該当"
                    detailed = f"除外対象ジャンルに該当 ({shooting_type})"
                else:
                    group_key = "【完全ノイズ・対象外】ゲーム募集 / 音楽ライブ / 都外確定 / カメラマン非募集"
                    reason = "非撮影ノイズ" if is_noise else ("カメラマン非募集" if not is_looking else "エリア対象外")
                    detailed = f"ゲーム募集・非撮影ノイズ ({shooting_type})" if is_noise else ("カメラマンを募集していません" if not is_looking else f"対象エリア外 (判定: {loc_name})")

                print(f" ➔ スキップ: {detailed}")
                skipped_tweets.append({
                    "text": tweet["text"], "url": f"https://x.com/i/status/{tweet['id']}",
                    "matched_keyword": kw, "author_followers": tweet.get("author_followers", 0),
                    "reason": reason, "detailed_reason": detailed, "shooting_type": shooting_type,
                    "location": analyzed.get("location", "場所不明"), "ocr_text": analyzed.get("ocr_text", "なし"),
                    "group_key": group_key
                })
                if not is_test_mode:
                    processed_ids.add(tweet["id"])
                    save_processed_ids(processed_ids, search_start_time_str, search_end_time_str, last_daily_summary_date, daily_history, balancer_hits)
                if kw in keyword_stats: keyword_stats[kw]["skipped"] += 1
                continue

            print(" ➔ マッチ検知！メール送信中...")
            send_single_email(analyzed, is_test_mode=is_test_mode, test_hours=test_hours)
            sent_count += 1
            if not is_test_mode:
                processed_ids.add(tweet["id"])
                save_processed_ids(processed_ids, search_start_time_str, search_end_time_str, last_daily_summary_date, daily_history, balancer_hits)
            if kw in keyword_stats: keyword_stats[kw]["sent"] += 1

        except Exception as e:
            print(f" ➔ エラー発生 (Tweet ID: {tweet.get('id')}): {e}")
            error_count += 1
            if kw in keyword_stats: keyword_stats[kw]["error"] += 1

    duration_sec = time.time() - start_time_epoch
    duration_str = f"{int(duration_sec // 60)}分{int(duration_sec % 60)}秒" if duration_sec >= 60 else f"{int(duration_sec)}秒"

    if not is_test_mode:
        if today_str not in daily_history:
            daily_history[today_str] = {"fetched_count": 0, "raw_tweets_count": 0, "sent_count": 0, "skipped_count": 0, "error_count": 0, "input_tokens": 0, "output_tokens": 0, "keyword_stats": {}}
        daily_history[today_str]["fetched_count"] += fetched_count
        daily_history[today_str]["raw_tweets_count"] = daily_history[today_str].get("raw_tweets_count", 0) + raw_total_count
        daily_history[today_str]["sent_count"] += sent_count
        daily_history[today_str]["skipped_count"] += skipped_count
        daily_history[today_str]["error_count"] += error_count
        daily_history[today_str]["input_tokens"] += total_input_tokens
        daily_history[today_str]["output_tokens"] += total_output_tokens
        for kw, s in keyword_stats.items():
            if kw not in daily_history[today_str]["keyword_stats"]: daily_history[today_str]["keyword_stats"][kw] = {"fetched": 0, "sent": 0, "skipped": 0, "error": 0}
            daily_history[today_str]["keyword_stats"][kw]["fetched"] += s["fetched"]
            daily_history[today_str]["keyword_stats"][kw]["sent"] += s["sent"]
            daily_history[today_str]["keyword_stats"][kw]["skipped"] += s["skipped"]
            daily_history[today_str]["keyword_stats"][kw]["error"] += s["error"]

    summary_data = {
        "fetched_count": fetched_count, "raw_tweets_count": raw_total_count, "sent_count": sent_count,
        "skipped_count": skipped_count, "error_count": error_count, "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens, "skipped_tweets": skipped_tweets,
        "duration": duration_str, "display_keywords": display_kws,
        "target_period_start": target_start_str, "target_period_end": target_end_str, "period_hours": period_hours,
        "config_status_badge": config_status_badge, "config_status_detail": config_status_detail, "config_source": config_source
    }

    if not is_test_mode:
        save_processed_ids(processed_ids, search_start_time_str, search_end_time_str, last_daily_summary_date, daily_history, balancer_hits)

    try:
        print("実行サマリーメール送信中...")
        send_summary_email(summary_data, is_test_mode=is_test_mode, test_hours=test_hours)
        print("サマリーメール送信完了")
    except Exception as e: print(f"サマリーメール送信エラー: {e}")

    print(f"🏁 全工程完了: 検知通知 {sent_count} 件 / 取得 {fetched_count} 件 (所要時間: {duration_str})")

if __name__ == "__main__":
    main()
