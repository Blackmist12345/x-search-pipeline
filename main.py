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

def load_config(config_path="config.json"):
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"設定ファイル {config_path} が見つかりません。")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_processed_ids(filepath="processed_ids.json"):
    default_ids = set()
    default_start = None
    default_end = None
    default_summary_date = None
    default_history = {}

    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return (
                        set(data.get("processed_ids", [])),
                        data.get("last_search_start_time", None),
                        data.get("last_search_end_time", None),
                        data.get("last_daily_summary_date", None),
                        data.get("daily_history", {})
                    )
                elif isinstance(data, list):
                    return set(data), default_start, default_end, default_summary_date, default_history
        except Exception as e:
            print(f"processed_ids.json の読み込み失敗 ({e})。新規セットを作成します。")
            return default_ids, default_start, default_end, default_summary_date, default_history
    return default_ids, default_start, default_end, default_summary_date, default_history

def save_processed_ids(processed_ids, last_search_start_time=None, last_search_end_time=None, 
                       last_daily_summary_date=None, daily_history=None, filepath="processed_ids.json", max_ids=5000):
    try:
        ids_list = list(processed_ids)
        if len(ids_list) > max_ids:
            ids_list = ids_list[-max_ids:]
        
        cleaned_history = {}
        if daily_history and isinstance(daily_history, dict):
            now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
            valid_dates = {(now_jst - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)}
            for k, v in daily_history.items():
                if k in valid_dates:
                    cleaned_history[k] = v

        data = {
            "processed_ids": ids_list,
            "last_search_start_time": last_search_start_time,
            "last_search_end_time": last_search_end_time,
            "last_daily_summary_date": last_daily_summary_date,
            "daily_history": cleaned_history
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"processed_ids.json の保存に失敗しました: {e}")

def check_env_vars():
    required_vars = ["TWITTERAPI_KEY", "GEMINI_API_KEY", "GMAIL_USER", "GMAIL_APP_PASS", "TO_EMAIL"]
    missing = [var for var in required_vars if not os.environ.get(var)]
    if missing:
        raise ValueError(f"以下の必須環境変数が設定されていません: {', '.join(missing)}")

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
            print(f"SMTPメール送信エラー (試行 {attempt + 1}/{max_retries}): {e}。{wait_time}秒後に再試行します...")
            if attempt < max_retries - 1:
                time.sleep(wait_time)
            else:
                raise e

def optimize_image_url(url):
    if "pbs.twimg.com/media/" in url:
        base_url = url.split("?")[0]
        return f"{base_url}?format=jpg&name=large"
    return url

def extract_media_urls(tweet_dict):
    urls = []
    if not isinstance(tweet_dict, dict):
        return urls

    media_list = (
        tweet_dict.get("extendedEntities", {}).get("media", []) or
        tweet_dict.get("entities", {}).get("media", []) or
        tweet_dict.get("media", []) or
        tweet_dict.get("mediaDetails", []) or
        tweet_dict.get("photos", [])
    )
    
    for m in media_list:
        if isinstance(m, str) and m.startswith("http"):
            urls.append(optimize_image_url(m))
        elif isinstance(m, dict):
            m_type = m.get("type", "photo")
            if m_type == "photo":
                m_url = m.get("media_url_https") or m.get("url") or m.get("media_url")
                if m_url:
                    urls.append(optimize_image_url(m_url))

    return list(dict.fromkeys(urls))

def detect_matched_keyword(full_text, display_keywords):
    text_lower = full_text.lower()
    for kw in display_keywords:
        kw_clean = kw.replace('"', '').replace('#', '').strip()
        if " and " in kw.lower() or " AND " in kw:
            parts = [p.replace('"', '').strip().lower() for p in re.split(r'\s+(?:and|AND)\s+', kw)]
            if all(p in text_lower for p in parts):
                return kw_clean
        else:
            if kw_clean.lower() in text_lower:
                return kw_clean
    return "カメラマン AND 募集"

def fetch_tweets_from_twitterapi_io(config, processed_ids, search_start_time, search_end_time, is_test_mode=False):
    api_key = os.environ.get("TWITTERAPI_KEY")
    url = "https://api.twitterapi.io/twitter/tweet/advanced_search"
    headers = {"X-API-Key": api_key}

    keywords = config.get("search_keywords", [])
    display_keywords = config.get("display_keywords", [])
    if not keywords:
        print("検索キーワード(search_keywords)が設定されていません。")
        return [], 0

    raw_tweets_all = []
    seen_tweet_ids = set()

    since_stamp = int(search_start_time.timestamp())
    until_stamp = int(search_end_time.timestamp())

    print(f"検索時間枠 (UTC): {search_start_time.isoformat()} 〜 {search_end_time.isoformat()}")
    print("スマートOR統合検索を実行中 (上限なし完全網羅ページネーション)...")
    
    for kw in keywords:
        cursor = None
        page = 1
        while True:
            query_str = f"{kw} since_time:{since_stamp} until_time:{until_stamp}"
            params = {"query": query_str, "queryType": "Latest"}
            if cursor:
                params["cursor"] = cursor

            tweets_raw = []
            has_next = False
            next_cursor = None

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    response = requests.get(url, headers=headers, params=params, timeout=30)
                    if response.status_code == 200:
                        data = response.json()
                        if isinstance(data, dict):
                            tweets_raw = data.get("tweets", [])
                            has_next = data.get("has_next_page", False)
                            next_cursor = data.get("next_cursor")
                        elif isinstance(data, list):
                            tweets_raw = data
                            has_next = False
                        
                        for tw in tweets_raw:
                            tw_id = str(tw.get("id"))
                            if tw_id not in seen_tweet_ids:
                                seen_tweet_ids.add(tw_id)
                                raw_tweets_all.append(tw)
                        break
                    elif response.status_code == 429:
                        wait_time = 2 ** (attempt + 1)
                        print(f"TwitterAPI.io 連打制限検知。{wait_time}秒待機して再試行...")
                        time.sleep(wait_time)
                    else:
                        print(f"TwitterAPI.io エラー: {response.status_code}")
                        break
                except requests.RequestException as e:
                    wait_time = 2 ** (attempt + 1)
                    print(f"TwitterAPI.io リクエストエラー (試行 {attempt + 1}/{max_retries}): {e}")
                    if attempt < max_retries - 1:
                        time.sleep(wait_time)
                    else:
                        break

            print(f" ➔ ページ {page} 取得完了 (取得ツイート: {len(tweets_raw)}件 / 累計: {len(raw_tweets_all)}件)")

            if not has_next or not next_cursor or not tweets_raw:
                break

            cursor = next_cursor
            page += 1
            time.sleep(0.5)

    raw_total_count = len(raw_tweets_all)
    filtered_tweets = []
    blacklist = config.get("blacklist_words", [])
    max_text_len = config.get("max_text_length", 1000)
    min_followers = config.get("min_followers_count", 0)

    for tweet in raw_tweets_all:
        tweet_id = str(tweet.get("id"))

        if not is_test_mode and tweet_id in processed_ids:
            continue

        created_at_str = tweet.get("createdAt")
        if created_at_str:
            try:
                if " +0000 " in created_at_str:
                    created_at = datetime.strptime(created_at_str, "%a %b %d %H:%M:%S %z %Y")
                else:
                    created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                
                if created_at < search_start_time:
                    continue
            except Exception as e:
                print(f"日時解析スキップ ({tweet_id}): {e}")

        author = tweet.get("author", {})
        followers_count = author.get("followers", 0) or author.get("followers_count", 0)
        if followers_count < min_followers:
            continue

        text_raw = tweet.get("text", "")

        # 本文文字数チェック (1000文字超はAI呼出前スキップ)
        if len(text_raw) > max_text_len:
            print(f" ➔ 本文{max_text_len}文字超過 ({len(text_raw)}文字) によりAI呼出前スキップ (ID: {tweet_id})")
            continue

        # 引用元の抽出
        quoted = None
        for key in ["quoted_tweet", "quotedTweet", "quoted_status", "quotedStatus"]:
            q = tweet.get(key)
            if q and isinstance(q, dict):
                quoted = q
                break

        quoted_text = ""
        quoted_id = ""
        quoted_images = []
        if quoted:
            quoted_id = str(quoted.get("id") or quoted.get("id_str") or "")
            quoted_text = quoted.get("text", "") or quoted.get("full_text", "")
            quoted_images = extract_media_urls(quoted)

        # リプライ元（親ポスト）の抽出
        reply_parent = None
        for key in ["in_reply_to_status", "inReplyToTweet", "reply_parent", "parent_tweet"]:
            rp = tweet.get(key)
            if rp and isinstance(rp, dict):
                reply_parent = rp
                break

        reply_text = ""
        reply_id = ""
        reply_images = []
        if reply_parent:
            reply_id = str(reply_parent.get("id") or reply_parent.get("id_str") or "")
            reply_text = reply_parent.get("text", "") or reply_parent.get("full_text", "")
            reply_images = extract_media_urls(reply_parent)

        full_text_combined = f"{text_raw}\n{quoted_text}\n{reply_text}"

        # 地域＆非コスプレ単語ブラックリスト合算除外 (0次フィルタ)
        matched_bad_word = None
        for bad_word in blacklist:
            if bad_word in full_text_combined:
                matched_bad_word = bad_word
                break
        
        if matched_bad_word:
            print(f" ➔ 0次ブラックリストワード検知 ('{matched_bad_word}') によりAI呼出前スキップ (ID: {tweet_id})")
            continue

        matched_specific_kw = detect_matched_keyword(full_text_combined, display_keywords)

        # 画像URL優先度: 引用元/親ポスト（フライヤー）を最優先にし、コスト削減のため厳選1枚のみAIに入力
        body_images = extract_media_urls(tweet)
        combined_images = []
        for url in quoted_images + reply_images + body_images:
            if url not in combined_images:
                combined_images.append(url)

        filtered_tweets.append({
            "id": tweet_id,
            "text": text_raw,
            "author_followers": followers_count,
            "image_urls": combined_images[:1], # 最重要1枚に厳選
            "matched_keyword": matched_specific_kw,
            "quoted_text": quoted_text,
            "quoted_id": quoted_id,
            "reply_text": reply_text,
            "reply_id": reply_id
        })

    return filtered_tweets, raw_total_count

def analyze_tweet_with_ai(ai_client, tweet, config):
    target_areas_str = "、".join(config.get("target_areas", ["東京都", "神奈川県", "埼玉県", "千葉県"]))
    image_urls = tweet.get("image_urls", [])

    parts = []
    for idx, url in enumerate(image_urls[:1]): # 厳選1枚
        try:
            img_resp = requests.get(url, timeout=15)
            if img_resp.status_code == 200:
                content_type = img_resp.headers.get("Content-Type", "")
                mime_type = content_type.split(";")[0] if content_type.startswith("image/") else 'image/jpeg'
                parts.append(types.Part.from_bytes(data=img_resp.content, mime_type=mime_type))
            else:
                print(f"画像{idx + 1}枚目のダウンロードスキップ (HTTP {img_resp.status_code})")
        except Exception as e:
            print(f"画像{idx + 1}枚目の取得エラー (Tweet ID: {tweet['id']}): {e}")

    has_images = len(parts) > 0

    prompt_conditions = f"""
【抽出・判定条件】
1. ocr_text: 画像内に日時・場所・参加費・募集条件などの重要要項が書かれている場合、画像を正確に読み取った上で要点のみを短文（100文字以内）で抽出してください（画像がない場合や不要な文字は "なし" としてください）。
2. location: 撮影場所を特定してください（都道府県・市区町村・スタジオ名・イベント名など）。場所が特定または推定できない場合は "場所不明" としてください。
3. is_tokyo_near: 撮影場所が「{target_areas_str}」のいずれかである場合は true、それ以外または「場所不明」の場合は false としてください。
4. is_cosplay: 撮影内容が「コスプレ撮影（またはコスプレ併せ・コスプレイベント等）」である場合は true、それ以外のポートレート撮影・ライブ撮影・物撮り・日常撮影・一般イベントなどの場合は false としてください。
5. shooting_type: なんの撮影であるかを分類・回答してください。なお、コスプレの撮影の場合はポスト本文・画像から『作品名 / キャラクター名』（例: 『コスプレ撮影（原神 / フリーナ）』『コスプレ併せ（チェンソーマン）』など）を特定してください。
6. is_excluded_genre: コスプレ撮影の場合、以下の除外対象ジャンル（全19作品）に該当するか厳格に判定してください。正式名称だけでなく、略称・隠語・絵文字（例: 忍たま, 刀剣/とうらぶ, あんスタ, 桃源暗鬼, イナイレ, ツイステ, ドクスト, アイナナ, ヒプマイ, 東リベ/東卍, ワートリ, 呪術, ブルロ, A3!, 金カム/ゴールデンカムイ, ペルソナ/P5/P4/P3, 鬼灯の冷徹/鬼火の冷徹 等）・キャラ名・作品固有用語（本丸, 審神者, ES, NRC, 科学王国, ナナライ, ディビジョン, マイキー, ボーダー, 領域展開, エゴイスト, 満開開花, 刺青人皮, 怪盗団, 閻魔大王 等）も含めて調査・特定し、該当する場合は true、該当しない場合は false としてください。
   【除外対象19作品】: 「忍たま乱太郎」「刀剣乱舞」「あんさんぶるスターズ」「桃源暗鬼」「イナズマイレブン」「ツイステッドワンダーランド」「ドクターストーン」「アイドリッシュセブン」「ヒプノシスマイク」「東京リベンジャーズ」「ワールドトリガー」「呪術廻戦」「ブルーロック」「A3!」「ゴールデンカムイ」「ペルソナ5」「ペルソナ4」「ペルソナ3」「鬼灯の冷徹（鬼火の冷徹）」
7. is_looking_for_photographer: 【本体ポスト】【引用元ポスト】【リプライ元ポスト】のいずれかで、カメラマン・撮影者・同行者を募集（または歓迎）していれば true、募集していない（被写体/レイヤーのみ募集等）場合は false としてください。
8. is_official_or_job: アコスタ、ココフリ等の企業イベント公式カメラマン募集、または企業・スタジオ等の求人・雇用契約・業務委託募集であれば true、個人の募集であれば false としてください。
9. is_noise: ゲームのフレンド募集・ギルド募集、音楽ライブ/対バン撮影、または写真撮影と無関係なノイズであれば true、それ以外は false としてください。
"""

    text_content = f"【ポスト本文】\n{tweet['text']}"
    if tweet.get("quoted_text"):
        text_content += f"\n\n【引用元ポスト本文】\n{tweet['quoted_text']}"
    if tweet.get("reply_text"):
        text_content += f"\n\n【リプライ元ポスト本文】\n{tweet['reply_text']}"

    if has_images:
        prompt = f"""以下のポスト本文（および引用元/リプライ元ポスト本文）と最高画質添付画像を総合解析し、指定のJSON構造で抽出してください。\n\n{text_content}\n\n{prompt_conditions}"""
        contents = parts + [prompt]
    else:
        prompt = f"""以下のポスト本文（および引用元/リプライ元ポスト本文）を解析し、指定のJSON構造で抽出してください。\n\n{text_content}\n\n{prompt_conditions}"""
        contents = [prompt]

    response_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "ocr_text": types.Schema(type=types.Type.STRING, description="画像内の重要要件のみ（最大100文字）。ない場合は 'なし'"),
            "location": types.Schema(type=types.Type.STRING, description="撮影場所。不明な場合は '場所不明'"),
            "is_tokyo_near": types.Schema(type=types.Type.BOOLEAN, description="撮影場所が対象エリアの場合は true、それ以外は false"),
            "is_cosplay": types.Schema(type=types.Type.BOOLEAN, description="コスプレ撮影であれば true、ポートレートやライブ等は false"),
            "shooting_type": types.Schema(type=types.Type.STRING, description="撮影種別（コスプレの場合は作品名・キャラ名記載）"),
            "is_excluded_genre": types.Schema(type=types.Type.BOOLEAN, description="除外対象ジャンルに該当する場合は true、それ以外は false"),
            "is_looking_for_photographer": types.Schema(type=types.Type.BOOLEAN, description="カメラマン募集内容であれば true、それ以外は false"),
            "is_official_or_job": types.Schema(type=types.Type.BOOLEAN, description="公式イベントカメラマンや企業求人の場合は true、個人の募集は false"),
            "is_noise": types.Schema(type=types.Type.BOOLEAN, description="ゲームフレンド募集や音楽ライブなど非撮影/ノイズの場合は true、それ以外は false")
        },
        required=["ocr_text", "location", "is_tokyo_near", "is_cosplay", "shooting_type", "is_excluded_genre", "is_looking_for_photographer", "is_official_or_job", "is_noise"]
    )

    max_retries = 5
    response = None
    fallback_to_text_only = False

    for attempt in range(max_retries):
        try:
            current_contents = contents
            if fallback_to_text_only and has_images:
                fallback_prompt = f"""以下のポスト本文（および引用元/リプライ元ポスト本文）を解析し、指定のJSON構造で抽出してください。画像はエラーのため除外されました。\n\n{text_content}\n\n{prompt_conditions}"""
                current_contents = [fallback_prompt]
                print(f" ➔ [自動フォールバック] 画像データを除外してテキストのみで再解析を実行中...")

            response = ai_client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=current_contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=response_schema
                )
            )
            break
        except Exception as e:
            err_msg = str(e)
            
            if has_images and not fallback_to_text_only and any(k in err_msg for k in ["400", "INVALID_ARGUMENT", "Unable to process input image"]):
                print(f" ➔ Gemini APIの画像解析エラー。画像を切り離して再試行します... ({err_msg[:60]})")
                fallback_to_text_only = True
                continue

            if any(k in err_msg for k in ["429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "502", "504"]):
                if "GenerateRequestsPerDay" in err_msg or "limit: 0" in err_msg:
                    print("Gemini APIの1日あたりの上限（Daily Quota）に達しました。")
                    raise e

                match = re.search(r'retry in (\d+(\.\d+)?)s', err_msg)
                wait_time = int(float(match.group(1))) + 5 if match else (2 ** (attempt + 1) * 5)
                print(f"Gemini API混雑/制限検知 ({err_msg[:40]}...)。{wait_time}秒待機して再試行します... ({attempt + 1}/{max_retries})")
                time.sleep(wait_time)
            else:
                print(f"Gemini API 呼び出しエラー詳細: {e}")
                raise e

    if not response:
        raise RuntimeError("API呼び出しのリトライ上限に達しました")

    input_tokens = 0
    output_tokens = 0
    if hasattr(response, 'usage_metadata') and response.usage_metadata:
        input_tokens = getattr(response.usage_metadata, 'prompt_token_count', 0) or 0
        output_tokens = getattr(response.usage_metadata, 'candidates_token_count', 0) or 0

    try:
        analysis = json.loads(response.text.strip())
    except json.JSONDecodeError:
        analysis = {
            "ocr_text": "なし" if not has_images else "解析エラー",
            "location": "場所不明",
            "is_tokyo_near": False,
            "is_cosplay": False,
            "shooting_type": "不明",
            "is_excluded_genre": False,
            "is_looking_for_photographer": True,
            "is_official_or_job": False,
            "is_noise": False
        }

    location_tag = analysis.get("location", "場所不明")
    if not analysis.get("is_tokyo_near", False) and location_tag != "場所不明":
        location_tag += " (対象外エリア)"

    formatted_image_urls = "\n  ".join(image_urls) if image_urls else "なし"

    return {
        "tweet_id": tweet["id"],
        "author_followers": tweet["author_followers"],
        "tweet_text": tweet["text"],
        "image_url": formatted_image_urls,
        "ocr_text": analysis.get("ocr_text", "なし"),
        "location": location_tag,
        "raw_location": analysis.get("location", "場所不明"),
        "is_tokyo_near": analysis.get("is_tokyo_near", False),
        "is_cosplay": analysis.get("is_cosplay", False),
        "shooting_type": analysis.get("shooting_type", "不明"),
        "is_excluded_genre": analysis.get("is_excluded_genre", False),
        "is_looking_for_photographer": analysis.get("is_looking_for_photographer", True),
        "is_official_or_job": analysis.get("is_official_or_job", False),
        "is_noise": analysis.get("is_noise", False),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "matched_keyword": tweet.get("matched_keyword", "不明"),
        "quoted_text": tweet.get("quoted_text", ""),
        "quoted_id": tweet.get("quoted_id", ""),
        "reply_text": tweet.get("reply_text", ""),
        "reply_id": tweet.get("reply_id", "")
    }

def send_single_email(item, is_test_mode=False, test_hours=0.0):
    msg = MIMEMultipart()
    msg['From'] = os.environ["GMAIL_USER"]
    msg['To'] = os.environ["TO_EMAIL"]
    
    shooting_type = item.get("shooting_type", "撮影募集")
    location = item.get("location", "場所不明")
    followers = item.get("author_followers", 0)
    matched_kw = item.get('matched_keyword', '不明')
    
    test_hours_display = "15分" if test_hours == 0.25 else ("30分" if test_hours == 0.5 else f"{int(test_hours) if test_hours.is_integer() else test_hours}時間")
    subject_prefix = f"【テスト実行({test_hours_display})/X募集】" if is_test_mode else "【X募集】"
    msg['Subject'] = f"{subject_prefix}{location}│{shooting_type} ({followers:,}人)"

    tokyo_near_str = "○" if item.get("is_tokyo_near") else "×"
    tweet_id = item.get("tweet_id", "")
    quoted_id = item.get("quoted_id", "")
    reply_id = item.get("reply_id", "")

    web_url = f"https://x.com/i/status/{tweet_id}" if tweet_id else "#"

    tweet_text_raw = item.get('tweet_text', '')
    tweet_text_clean = tweet_text_raw.replace('\n', ' ').replace('\r', '').strip()
    tweet_text_html = tweet_text_raw.replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br>')
    ocr_text_html = item.get('ocr_text', 'なし').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br>')

    preheader_text = f"[ワード:{matched_kw}] 「{tweet_text_clean[:70]}」"

    test_banner_html = ""
    if is_test_mode:
        test_banner_html = f"""
        <div style="background-color: #fff3cd; color: #856404; padding: 10px; border-radius: 6px; margin-bottom: 15px; font-weight: bold; text-align: center; border: 1px solid #ffeeba;">
          これは手動テスト実行による通知です（直近{test_hours_display}を重複除外なしで取得）
        </div>
        """

    extra_btns_html = ""
    if quoted_id:
        quoted_web_url = f"https://x.com/i/status/{quoted_id}"
        extra_btns_html += f"""
        <div style="margin-top: 10px;">
          <a href="{quoted_web_url}" class="btn-secondary" target="_blank">引用元ポストをXで開く</a>
        </div>
        """
    if reply_id:
        reply_web_url = f"https://x.com/i/status/{reply_id}"
        extra_btns_html += f"""
        <div style="margin-top: 10px;">
          <a href="{reply_web_url}" class="btn-secondary" target="_blank">リプライ元（親ポスト）をXで開く</a>
        </div>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <style>
        body {{
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
          background-color: #f4f5f7;
          color: #333333;
          margin: 0;
          padding: 15px;
        }}
        .card {{
          background-color: #ffffff;
          max-width: 580px;
          margin: 0 auto;
          border: 1px solid #e1e4e8;
          border-radius: 8px;
          padding: 20px;
          box-shadow: 0 4px 10px rgba(0,0,0,0.05);
        }}
        .header {{
          font-size: 16px;
          font-weight: bold;
          color: #0366d6;
          background-color: #e2f0fd;
          padding: 10px 14px;
          border-radius: 6px;
          margin-bottom: 15px;
          border-left: 4px solid #0366d6;
        }}
        .badge {{
          display: inline-block;
          background-color: #e2f0fd;
          color: #0366d6;
          padding: 4px 8px;
          border-radius: 4px;
          font-size: 12px;
          font-weight: bold;
          margin-right: 5px;
          margin-bottom: 5px;
        }}
        .badge-near {{
          background-color: #e6ffed;
          color: #28a745;
        }}
        .meta-list {{
          list-style: none;
          padding: 0;
          margin: 15px 0;
          font-size: 14px;
          line-height: 1.6;
        }}
        .meta-list li {{
          margin-bottom: 8px;
          color: #24292e;
        }}
        .ocr-box {{
          background-color: #fafbfc;
          border-left: 4px solid #0366d6;
          padding: 12px;
          font-size: 13px;
          color: #586069;
          margin: 15px 0;
          word-break: break-all;
          border-radius: 0 4px 4px 0;
        }}
        .divider-thin {{
          color: #999999;
          letter-spacing: 1px;
          font-size: 11px;
          margin: 15px 0;
          text-align: center;
          font-weight: bold;
        }}
        .body-text {{
          font-size: 14px;
          line-height: 1.6;
          color: #24292e;
          background-color: #fafbfc;
          padding: 15px;
          border: 1px solid #e1e4e8;
          border-radius: 6px;
          word-break: break-all;
        }}
        .btn-container {{
          text-align: center;
          margin-top: 25px;
        }}
        .btn {{
          display: inline-block;
          background-color: #1da1f2;
          color: #ffffff !important;
          text-decoration: none;
          padding: 12px 28px;
          border-radius: 6px;
          font-weight: bold;
          font-size: 15px;
          box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .btn-secondary {{
          display: inline-block;
          background-color: #657786;
          color: #ffffff !important;
          text-decoration: none;
          padding: 8px 16px;
          border-radius: 6px;
          font-weight: bold;
          font-size: 12px;
        }}
      </style>
    </head>
    <body>
      <div style="display:none;font-size:1px;color:#ffffff;line-height:1px;max-height:0px;max-width:0px;opacity:0;overflow:hidden;mso-hide:all;">
        {preheader_text}
      </div>
      <div style="display:none;font-size:1px;line-height:1px;max-height:0px;max-width:0px;opacity:0;overflow:hidden;mso-hide:all;">
        &nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;
      </div>

      <div class="card">
        {test_banner_html}
        <div class="header">{location} │ {shooting_type}</div>
        <ul class="meta-list">
          <li><strong>・投稿者フォロワー数:</strong> {followers:,} 人</li>
          <li><strong>・ヒットした検索ワード:</strong> <span class="badge" style="background-color:#fff5b1; color:#b06000;">{matched_kw}</span></li>
          <li><strong>・撮影種別:</strong> <span class="badge">{shooting_type}</span></li>
          <li><strong>・撮影場所:</strong> <span class="badge">{location}</span> (都内近郊: <span class="badge badge-near">{tokyo_near_str}</span>)</li>
        </ul>
        
        <div class="ocr-box">
          <strong>・画像内重要文字 (OCR):</strong><br>
          <div style="margin-top: 5px;">{ocr_text_html}</div>
        </div>
        
        <div class="divider-thin">{"-" * 50}</div>
        
        <div class="body-text">
          {tweet_text_html}
        </div>
        
        <div class="btn-container">
          <a href="{web_url}" class="btn" target="_blank">X (Twitter) で投稿を見る</a>
          {extra_btns_html}
        </div>
      </div>
    </body>
    </html>
    """

    msg.attach(MIMEText(html_content, 'html', 'utf-8'))
    send_email_with_retry(msg)

def send_daily_total_summary_email(daily_stats, target_date_str, display_keywords):
    msg = MIMEMultipart()
    msg['From'] = os.environ["GMAIL_USER"]
    msg['To'] = os.environ["TO_EMAIL"]
    msg['Subject'] = f"【X撮影募集】前日トータルサマリー ({target_date_str})"

    fetched_count = daily_stats.get("fetched_count", 0)
    raw_tweets_count = daily_stats.get("raw_tweets_count", fetched_count)
    sent_count = daily_stats.get("sent_count", 0)
    skipped_count = daily_stats.get("skipped_count", 0)
    error_count = daily_stats.get("error_count", 0)
    input_tokens = daily_stats.get("input_tokens", 0)
    output_tokens = daily_stats.get("output_tokens", 0)
    total_tokens = input_tokens + output_tokens

    # Gemini 3.5 Flash-Lite 最新公式単価 (入力: $0.30/1M, 出力: $2.50/1M)
    gemini_usd = ((input_tokens / 1_000_000) * 0.30) + ((output_tokens / 1_000_000) * 2.50)
    gemini_jpy = gemini_usd * 155.0

    # TwitterAPI.io 公式単価 (1件=15クレジット, 100万クレジット=$10)
    twitter_credits = raw_tweets_count * 15
    twitter_usd = (twitter_credits / 1_000_000) * 10.0
    twitter_jpy = twitter_usd * 155.0

    total_usd = gemini_usd + twitter_usd
    total_jpy = gemini_jpy + twitter_jpy

    # 1ヶ月 (30日) 換算
    monthly_twitter_credits = twitter_credits * 30
    monthly_twitter_jpy = twitter_jpy * 30
    monthly_twitter_usd = twitter_usd * 30

    monthly_gemini_tokens = total_tokens * 30
    monthly_gemini_jpy = gemini_jpy * 30
    monthly_gemini_usd = gemini_usd * 30

    monthly_total_jpy = total_jpy * 30
    monthly_total_usd = total_usd * 30

    kw_stats = daily_stats.get("keyword_stats", {})

    # TOP 10 ランキング作成 (取得件数順)
    sorted_kw_list = sorted(
        [(k, v) for k, v in kw_stats.items() if v.get("fetched", 0) > 0],
        key=lambda x: (x[1].get("fetched", 0), x[1].get("sent", 0)),
        reverse=True
    )

    top10_html = ""
    for idx, (kw, s) in enumerate(sorted_kw_list[:10]):
        rank_str = f"{idx+1}位"
        top10_html += f"""
        <li style="margin-bottom: 6px; font-size: 13px;">
          <strong>{rank_str} 【{kw}】</strong>: 取得: <strong>{s['fetched']:,}</strong> / 送信: <span style="color:#28a745; font-weight:bold;">{s['sent']:,}</span> / スキップ: {s['skipped']:,}
        </li>
        """

    if not top10_html:
        top10_html = '<li style="color:#586069; font-size:13px;">・前日のヒットはありませんでした。</li>'

    # 全84パターンの定義順一覧
    all_kws_html = ""
    for kw in display_keywords:
        s = kw_stats.get(kw, {"fetched": 0, "sent": 0, "skipped": 0, "error": 0})
        all_kws_html += f"""
        <li style="margin-bottom: 4px; font-size: 12.5px; color:#444d56;">
          ・<strong>【{kw}】</strong>: 取得: {s['fetched']:,} │ 送信: {s['sent']:,} │ スキップ: {s['skipped']:,}
        </li>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <style>
        body {{
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
          background-color: #f4f5f7;
          color: #333333;
          margin: 0;
          padding: 15px;
        }}
        .card {{
          background-color: #ffffff;
          max-width: 600px;
          margin: 0 auto;
          border: 1px solid #e1e4e8;
          border-radius: 8px;
          padding: 20px;
          box-shadow: 0 4px 10px rgba(0,0,0,0.05);
        }}
        .header {{
          font-size: 18px;
          font-weight: bold;
          color: #24292e;
          margin-bottom: 15px;
          border-bottom: 2px solid #0366d6;
          padding-bottom: 10px;
        }}
        .section-title {{
          font-size: 14px;
          font-weight: bold;
          color: #24292e;
          margin-top: 20px;
          margin-bottom: 10px;
          background-color: #f6f8fa;
          padding: 6px 12px;
          border-radius: 4px;
        }}
        .stat-box-container {{
          display: flex;
          justify-content: space-between;
          margin: 15px 0;
        }}
        .stat-box {{
          flex: 1;
          background-color: #fafbfc;
          border: 1px solid #e1e4e8;
          border-radius: 6px;
          padding: 10px 4px;
          text-align: center;
          margin: 0 3px;
        }}
        .stat-num {{
          font-size: 16px;
          font-weight: bold;
          color: #0366d6;
          margin-top: 4px;
        }}
        .meta-list {{
          list-style: none;
          padding: 0;
          margin: 10px 0;
          font-size: 13px;
          line-height: 1.6;
        }}
        .meta-list li {{
          margin-bottom: 6px;
          color: #24292e;
        }}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="header">前日トータルサマリー ({target_date_str})</div>
        <div style="font-size:13px; color:#586069; margin-bottom:15px;">
          前日 24 時間に収集・処理された実績および API 消費金額の総計です。
        </div>

        <div class="section-title">■ 前日 24 時間の累計ポスト処理数</div>
        <div class="stat-box-container">
          <div class="stat-box">
            <div style="font-size: 10px; color: #586069;">総取得数</div>
            <div class="stat-num">{fetched_count:,}</div>
          </div>
          <div class="stat-box" style="border-color: #34d058;">
            <div style="font-size: 10px; color: #28a745;">総通知数</div>
            <div class="stat-num" style="color: #28a745;">{sent_count:,}</div>
          </div>
          <div class="stat-box">
            <div style="font-size: 10px; color: #586069;">総スキップ</div>
            <div class="stat-num" style="color: #6a737d;">{skipped_count:,}</div>
          </div>
          <div class="stat-box" style="border-color: #f97583;">
            <div style="font-size: 10px; color: #cb2431;">総エラー</div>
            <div class="stat-num" style="color: #cb2431;">{error_count:,}</div>
          </div>
        </div>

        <div class="section-title">■ 前日実績の API 消費量 ＆ 概算費用</div>
        <ul class="meta-list">
          <li>・<strong>TwitterAPI.io:</strong> {twitter_credits:,} credits ({raw_tweets_count:,}件) ➔ 約 <strong>{twitter_jpy:.2f} 円</strong> (${twitter_usd:.4f})</li>
          <li>・<strong>Gemini AI (3.5-Flash-Lite):</strong> {total_tokens:,} tokens ➔ 約 <strong>{gemini_jpy:.2f} 円</strong> (${gemini_usd:.4f})</li>
          <li style="margin-top: 6px; border-top: 1px dashed #e1e4e8; padding-top: 6px;">
            ★ <strong>前日24時間 合計コスト: 約 <span style="color:#0366d6; font-size:15px; font-weight:bold;">{total_jpy:.2f} 円</span></strong> (${total_usd:.4f})
          </li>
        </ul>

        <div class="section-title">■ 前日実績ベースの月間換算試算 (30日分)</div>
        <ul class="meta-list">
          <li>・<strong>TwitterAPI.io (月間換算):</strong> 約 {monthly_twitter_credits:,} credits / 月 ➔ 約 <strong>{monthly_twitter_jpy:.2f} 円 / 月</strong> (${monthly_twitter_usd:.2f})</li>
          <li>・<strong>Gemini AI (月間換算):</strong> 約 {monthly_gemini_tokens:,} tokens / 月 ➔ 約 <strong>{monthly_gemini_jpy:.2f} 円 / 月</strong> (${monthly_gemini_usd:.2f})</li>
          <li style="margin-top: 6px; border-top: 1px dashed #e1e4e8; padding-top: 6px;">
            ★ <strong>月間合計概算コスト: 約 <span style="color:#d73a49; font-size:16px; font-weight:bold;">{monthly_total_jpy:.2f} 円 / 月</span></strong> (${monthly_total_usd:.2f} / 月)
          </li>
        </ul>

        <div class="section-title">■ 前日ヒット数 上位10パターン (TOP 10)</div>
        <ul style="padding-left: 20px; font-size:13px; line-height: 1.6; color:#24292e; margin: 8px 0;">
          {top10_html}
        </ul>

        <details style="margin-top: 14px; border: 1px solid #e1e4e8; border-radius: 6px; padding: 10px; background-color: #fafbfc;">
          <summary style="font-size: 13.5px; font-weight: bold; color: #0366d6; cursor: pointer; padding: 4px;">
            検索単語ごとの全処理内訳を表示する (全84パターン)
          </summary>
          <ul style="padding-left: 18px; margin: 10px 0 0 0; line-height: 1.5;">
            {all_kws_html}
          </ul>
        </details>
      </div>
    </body>
    </html>
    """

    msg.attach(MIMEText(html_content, 'html', 'utf-8'))
    send_email_with_retry(msg)

def send_summary_email(summary_data, is_test_mode=False, test_hours=0.0):
    msg = MIMEMultipart()
    msg['From'] = os.environ["GMAIL_USER"]
    msg['To'] = os.environ["TO_EMAIL"]

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    date_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")

    sent_count = summary_data["sent_count"]
    fetched_count = summary_data["fetched_count"]
    raw_tweets_count = summary_data.get("raw_tweets_count", fetched_count)
    
    test_hours_display = "15分" if test_hours == 0.25 else ("30分" if test_hours == 0.5 else f"{int(test_hours) if test_hours.is_integer() else test_hours}時間")
    subject_prefix = f"【テスト実行({test_hours_display})/X撮影募集】" if is_test_mode else "【X撮影募集】"
    msg['Subject'] = f"{subject_prefix}実行完了サマリー (通知: {sent_count}件 / 取得: {fetched_count}件)"

    input_tokens = summary_data["input_tokens"]
    output_tokens = summary_data["output_tokens"]
    total_tokens = input_tokens + output_tokens
    
    # Gemini 3.5 Flash-Lite 最新公式単価 (入力: $0.30/1M, 出力: $2.50/1M)
    gemini_usd = ((input_tokens / 1_000_000) * 0.30) + ((output_tokens / 1_000_000) * 2.50)
    gemini_jpy = gemini_usd * 155.0

    # TwitterAPI.io 公式単価 (1件=15クレジット, 100万クレジット=$10)
    twitter_credits = raw_tweets_count * 15
    twitter_usd = (twitter_credits / 1_000_000) * 10.0
    twitter_jpy = twitter_usd * 155.0

    total_usd = gemini_usd + twitter_usd
    total_jpy = gemini_jpy + twitter_jpy

    # 検索対象期間（時間数）に基づく月間概算コスト換算 (1日24時間 × 30日)
    period_hours = summary_data.get("period_hours", 1.0)
    if period_hours <= 0:
        period_hours = 1.0
    monthly_multiplier = (24.0 / period_hours) * 30.0

    monthly_twitter_credits = int(twitter_credits * monthly_multiplier)
    monthly_raw_tweets = int(raw_tweets_count * monthly_multiplier)
    monthly_twitter_jpy = twitter_jpy * monthly_multiplier
    monthly_twitter_usd = twitter_usd * monthly_multiplier

    monthly_gemini_tokens = int(total_tokens * monthly_multiplier)
    monthly_gemini_jpy = gemini_jpy * monthly_multiplier
    monthly_gemini_usd = gemini_usd * monthly_multiplier

    monthly_jpy_cost = total_jpy * monthly_multiplier
    monthly_usd_cost = total_usd * monthly_multiplier

    duration = summary_data.get("duration", "不明")
    display_keywords = summary_data.get("display_keywords", [])
    kw_order = {kw: i for i, kw in enumerate(display_keywords)}

    skipped_tweets = summary_data.get("skipped_tweets", [])

    # 優先度順グループ再編成 (絵文字撤去)
    grouped_skipped = {
        "【要確認・併せ募集】コスプレ併せ・撮影 (カメラマン募集あり・場所不明/都外判定)": [],
        "【一般撮影】ポートレート・個人撮影 (カメラマン募集あり)": [],
        "【企業・公式・求人】公式イベント / 企業雇用・スタッフ募集": [],
        "【除外ジャンル】指定除外作品 (東リベ/ワートリ/呪術/ブルロ/金カム/P5等)": [],
        "【完全ノイズ・対象外】ゲーム募集 / 音楽ライブ / 都外確定 / カメラマン非募集": []
    }
    others = []
    
    for item in skipped_tweets:
        group_key = item.get("group_key")
        if group_key and group_key in grouped_skipped:
            grouped_skipped[group_key].append(item)
        else:
            others.append(item)

    group_configs = [
        ("【要確認・併せ募集】コスプレ併せ・撮影 (カメラマン募集あり・場所不明/都外判定)", "#e36209", "#fff8f2"),
        ("【一般撮影】ポートレート・個人撮影 (カメラマン募集あり)", "#0366d6", "#f1f8ff"),
        ("【企業・公式・求人】公式イベント / 企業雇用・スタッフ募集", "#6f42c1", "#fbf0fc"),
        ("【除外ジャンル】指定除外作品 (東リベ/ワートリ/呪術/ブルロ/金カム/P5等)", "#d73a49", "#ffeef0"),
        ("【完全ノイズ・対象外】ゲーム募集 / 音楽ライブ / 都外確定 / カメラマン非募集", "#6a737d", "#f6f8fa")
    ]

    skipped_html = ""
    total_idx = 1
    has_printed_group = False

    for group_name, border_color, bg_color in group_configs:
        items = grouped_skipped[group_name]
        if items:
            # 2段階多段ソート: ①ヒット単語順 ➔ ②撮影種別順
            items.sort(key=lambda x: (
                kw_order.get(x.get("matched_keyword", "不明"), 999),
                x.get("shooting_type", "不明"),
                x.get("url", "")
            ))

            if has_printed_group:
                skipped_html += f'<div style="text-align: center; color: #0366d6; letter-spacing: 1px; margin: 18px 0; font-size:13px; font-weight:bold;">{"=" * 30}</div>'
            
            skipped_html += f"""
            <div style="border-left: 4px solid {border_color}; background-color: {bg_color}; padding: 14px; margin-bottom: 14px; border-radius: 6px;">
                <h4 style="margin: 0 0 12px 0; color: #24292e; font-size: 14.5px; font-weight:bold;">■ {group_name} ({len(items)}件):</h4>
            """
            for item in items:
                tweet_text_safe = item.get('text', '').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br>')
                ai_shooting = item.get('shooting_type', '不明')
                ai_location = item.get('location', '場所不明')
                ai_ocr = item.get('ocr_text', 'なし')
                detailed_reason = item.get('detailed_reason', item.get('reason', '不明'))

                skipped_html += f"""
                <div style="background-color: #ffffff; border: 1px solid #e1e4e8; border-radius: 6px; padding: 12px; margin-bottom: 12px; font-size: 13px; line-height: 1.5;">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 6px;">
                        <div>
                            <strong>[{total_idx}]</strong> 
                            <span style="background-color: #e2f0fd; color: #0366d6; padding: 2px 6px; border-radius: 4px; font-size:12px; font-weight:bold;">{item.get('matched_keyword', '不明')}</span>
                        </div>
                        <a href="{item.get('url', '#')}" style="color: #0366d6; text-decoration: none; font-weight:bold; font-size:13px;" target="_blank">投稿を見る</a>
                    </div>
                    
                    <div style="background-color: #fff9f0; border-left: 3px solid {border_color}; padding: 6px 10px; margin: 6px 0; border-radius: 0 4px 4px 0; font-size: 12.5px;">
                        <strong>スキップ理由:</strong> <span style="color: #cb2431; font-weight:bold;">{detailed_reason}</span><br>
                        <strong>AI判定結果:</strong> 撮影種別: <strong>{ai_shooting}</strong> │ 判定場所: <strong>{ai_location}</strong><br>
                        <strong>画像OCR要点:</strong> {ai_ocr}
                    </div>

                    <div style="margin-top: 6px; color: #444d56; font-size:12.5px; background-color: #fafbfc; padding: 8px; border-radius: 4px; word-break: break-all;">
                        {tweet_text_safe}
                    </div>
                </div>
                """
                total_idx += 1
            skipped_html += "</div>"
            has_printed_group = True

    if others:
        others.sort(key=lambda x: (
            kw_order.get(x.get("matched_keyword", "不明"), 999),
            x.get("shooting_type", "不明"),
            x.get("url", "")
        ))
        if has_printed_group:
            skipped_html += f'<div style="text-align: center; color: #0366d6; letter-spacing: 1px; margin: 18px 0; font-size:13px; font-weight:bold;">{"=" * 30}</div>'
        
        skipped_html += f"""
        <div style="border-left: 4px solid #d73a49; background-color: #ffeef0; padding: 14px; margin-bottom: 14px; border-radius: 6px;">
            <h4 style="margin: 0 0 12px 0; color: #24292e; font-size: 14.5px; font-weight:bold;">■【その他】({len(others)}件):</h4>
        """
        for item in others:
            tweet_text_safe = item.get('text', '').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br>')
            skipped_html += f"""
            <div style="background-color: #ffffff; border: 1px solid #e1e4e8; border-radius: 6px; padding: 12px; margin-bottom: 12px; font-size: 13px; line-height: 1.5;">
                <strong>[{total_idx}]</strong> <span style="background-color: #e1e4e8; padding: 2px 6px; border-radius: 3px; font-size:12px;">{item.get('matched_keyword', '不明')}</span> 
                <span style="color:#d73a49; font-weight:bold; font-size:12.5px;">({item.get('reason', '不明')})</span> 
                <a href="{item.get('url', '#')}" style="color: #0366d6; text-decoration: none; font-weight:bold; font-size:13px; margin-left:8px;" target="_blank">投稿を見る</a><br>
                <div style="margin-top: 6px; color: #586069; font-size:12.5px; line-height:1.4;">{tweet_text_safe}</div>
            </div>
            """
            total_idx += 1
        skipped_html += "</div>"
        has_printed_group = True

    if not skipped_tweets:
        skipped_html = '<div style="font-size: 13px; color: #586069; padding: 8px 0;">・スキップされたポストはありません。</div>'

    test_banner_html = ""
    if is_test_mode:
        test_banner_html = f"""
        <div style="background-color: #fff3cd; color: #856404; padding: 10px; border-radius: 6px; margin-bottom: 15px; font-weight: bold; text-align: center; border: 1px solid #ffeeba;">
          これは手動テスト実行の結果です（直近{test_hours_display}を重複除外なしで取得）
        </div>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <style>
        body {{
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
          background-color: #f4f5f7;
          color: #333333;
          margin: 0;
          padding: 15px;
        }}
        .card {{
          background-color: #ffffff;
          max-width: 600px;
          margin: 0 auto;
          border: 1px solid #e1e4e8;
          border-radius: 8px;
          padding: 20px;
          box-shadow: 0 4px 10px rgba(0,0,0,0.05);
        }}
        .header {{
          font-size: 18px;
          font-weight: bold;
          color: #24292e;
          margin-bottom: 15px;
          border-bottom: 2px solid #e1e4e8;
          padding-bottom: 10px;
        }}
        .meta-list {{
          list-style: none;
          padding: 0;
          margin: 10px 0;
          font-size: 13.5px;
          line-height: 1.6;
        }}
        .meta-list li {{
          margin-bottom: 6px;
          color: #24292e;
        }}
        .section-title {{
          font-size: 14.5px;
          font-weight: bold;
          color: #24292e;
          margin-top: 18px;
          margin-bottom: 10px;
          background-color: #f6f8fa;
          padding: 7px 12px;
          border-radius: 4px;
        }}
        .stat-box-container {{
          display: flex;
          justify-content: space-between;
          margin: 12px 0;
        }}
        .stat-box {{
          flex: 1;
          background-color: #fafbfc;
          border: 1px solid #e1e4e8;
          border-radius: 6px;
          padding: 10px 4px;
          text-align: center;
          margin: 0 3px;
        }}
        .stat-num {{
          font-size: 17px;
          font-weight: bold;
          color: #0366d6;
          margin-top: 3px;
        }}
      </style>
    </head>
    <body>
      <div class="card">
        {test_banner_html}
        <div class="header">【X撮影募集】実行完了サマリー</div>
        <ul class="meta-list">
          <li><strong>■ 実行日時 (JST):</strong> {date_str}</li>
          <li><strong>・検索対象期間 (JST):</strong> {summary_data.get('target_period_start', '不明')} 〜 {summary_data.get('target_period_end', '不明')} ({period_hours:.2f}時間分)</li>
          <li><strong>・処理所要時間:</strong> {duration}</li>
        </ul>
        
        <div class="section-title">■ 全体ポスト処理件数</div>
        <div class="stat-box-container">
          <div class="stat-box">
            <div style="font-size: 10.5px; color: #586069;">新規取得</div>
            <div class="stat-num">{fetched_count:,}</div>
          </div>
          <div class="stat-box" style="border-color: #34d058;">
            <div style="font-size: 10.5px; color: #28a745;">個別通知</div>
            <div class="stat-num" style="color: #28a745;">{sent_count:,}</div>
          </div>
          <div class="stat-box">
            <div style="font-size: 10.5px; color: #586069;">スキップ</div>
            <div class="stat-num" style="color: #6a737d;">{summary_data['skipped_count']:,}</div>
          </div>
          <div class="stat-box" style="border-color: #f97583;">
            <div style="font-size: 10.5px; color: #cb2431;">エラー</div>
            <div class="stat-num" style="color: #cb2431;">{summary_data['error_count']:,}</div>
          </div>
        </div>
        
        <div class="section-title">■ API消費量 ＆ 概算コスト (今回の実行)</div>
        <ul class="meta-list" style="padding-left: 10px;">
          <li>・<strong>TwitterAPI.io:</strong> {twitter_credits:,} credits ({raw_tweets_count:,}件) ➔ 約 <strong>{twitter_jpy:.2f} 円</strong> (${twitter_usd:.4f})</li>
          <li>・<strong>Gemini AI (3.5-Flash-Lite):</strong> {total_tokens:,} tokens ➔ 約 <strong>{gemini_jpy:.2f} 円</strong> (${gemini_usd:.4f})</li>
          <li style="margin-top: 6px; border-top: 1px dashed #e1e4e8; padding-top: 6px;">
            ★ <strong>今回の実行合計コスト: 約 <span style="color:#0366d6; font-size:15px; font-weight:bold;">{total_jpy:.2f} 円</span></strong> (${total_usd:.4f})
          </li>
        </ul>

        <div class="section-title">■ 月間概算コスト換算試算 (1日24時間 × 30日)</div>
        <ul class="meta-list" style="padding-left: 10px;">
          <li>・<strong>TwitterAPI.io (月間換算):</strong> 約 {monthly_twitter_credits:,} credits (約 {monthly_raw_tweets:,}件) ➔ 約 <strong>{monthly_twitter_jpy:.2f} 円 / 月</strong> (${monthly_twitter_usd:.2f})</li>
          <li>・<strong>Gemini AI (月間換算):</strong> 約 {monthly_gemini_tokens:,} tokens / 月 ➔ 約 <strong>{monthly_gemini_jpy:.2f} 円 / 月</strong> (${monthly_gemini_usd:.2f})</li>
          <li style="margin-top: 6px; border-top: 1px dashed #e1e4e8; padding-top: 6px;">
            ★ <strong>月間合計概算コスト: 約 <span style="color:#d73a49; font-size:16px; font-weight:bold;">{monthly_jpy_cost:.2f} 円 / 月</span></strong> (${monthly_usd_cost:.2f} / 月)
            <div style="font-size:11.5px; color:#586069; margin-top:2px;">(※今回の検索範囲 {period_hours:.2f}時間分 を基準に 30日換算で試算)</div>
          </li>
        </ul>
        
        <details style="margin-top: 18px; border: 1px solid #e1e4e8; border-radius: 6px; padding: 12px; background-color: #fafbfc;">
          <summary style="font-size: 14.5px; font-weight: bold; color: #cb2431; cursor: pointer; padding: 4px;">
            スキップされたポスト一覧を表示する (計 {len(skipped_tweets)} 件)
          </summary>
          <div style="margin-top: 14px;">
            {skipped_html}
          </div>
        </details>
      </div>
    </body>
    </html>
    """

    msg.attach(MIMEText(html_content, 'html', 'utf-8'))
    send_email_with_retry(msg)

def main():
    start_time_epoch = time.time()
    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today_str = now_jst.strftime("%Y-%m-%d")
    yesterday_str = (now_jst - timedelta(days=1)).strftime("%Y-%m-%d")
    start_time_str = now_jst.strftime("%H:%M:%S")

    # テストモード判定 (遡り時間: 0.25, 0.5, 1, 2, 4, 8, 16)
    test_hours_str = os.environ.get("TEST_HOURS", "0")
    if "--test" in sys.argv:
        test_hours = 2.0
    elif "--test-hours" in sys.argv:
        try:
            idx = sys.argv.index("--test-hours")
            test_hours = float(sys.argv[idx + 1])
        except Exception:
            test_hours = 2.0
    else:
        try:
            test_hours = float(test_hours_str)
        except ValueError:
            test_hours = 0.0

    is_test_mode = test_hours > 0.0
    test_hours_display = "15分" if test_hours == 0.25 else ("30分" if test_hours == 0.5 else f"{int(test_hours) if test_hours.is_integer() else test_hours}時間")

    if is_test_mode:
        print("=" * 60)
        print(f"【手動テスト実行モードで起動中 (直近 {test_hours_display})】")
        print(f"・直近 {test_hours_display} 分のポストをID重複除外なしで取得・解析します。")
        print("・本番DBのタイムトラッキングは保護され、次回の定期実行に影響を与えません。")
        print("=" * 60)

    check_env_vars()

    config = load_config("config.json")
    display_kws = config.get("display_keywords", [])
    (processed_ids, 
     last_search_start_time_str, 
     last_search_end_time_str, 
     last_daily_summary_date, 
     daily_history) = load_processed_ids("processed_ids.json")
    
    # テストモードではない場合のみ前日サマリーを送信
    if not is_test_mode and last_daily_summary_date != yesterday_str and yesterday_str in daily_history:
        try:
            print(f"前日 ({yesterday_str}) のトータルサマリーメールを送信中...")
            send_daily_total_summary_email(daily_history[yesterday_str], yesterday_str, display_kws)
            last_daily_summary_date = yesterday_str
            print(f"前日トータルサマリーの送信が完了しました。")
        except Exception as e:
            print(f"前日トータルサマリーの送信中にエラーが発生しました: {e}")

    now_utc = datetime.now(timezone.utc)
    search_end_time = now_utc.replace(microsecond=0)
    search_end_time_str = search_end_time.isoformat().replace("+00:00", "Z")

    if is_test_mode:
        search_start_time = now_utc - timedelta(hours=test_hours)
    else:
        if last_search_end_time_str:
            try:
                if last_search_end_time_str.endswith("Z"):
                    last_end = datetime.fromisoformat(last_search_end_time_str.replace("Z", "+00:00"))
                else:
                    last_end = datetime.fromisoformat(last_search_end_time_str)
                search_start_time = last_end
            except Exception as e:
                print(f"前回検索終了時刻のパース失敗 ({e})。デフォルト期間(130分前)を使用します。")
                search_start_time = now_utc - timedelta(minutes=130)
        else:
            search_start_time = now_utc - timedelta(minutes=130)

        max_back_time = now_utc - timedelta(hours=24)
        if search_start_time < max_back_time:
            print("警告: 前回実行から24時間以上経過しているため、直近24時間に検索範囲を制限します。")
            search_start_time = max_back_time

    search_start_time = search_start_time.replace(microsecond=0)
    search_start_time_str = search_start_time.isoformat().replace("+00:00", "Z")

    target_start_str = (search_start_time + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
    target_end_str = (search_end_time + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
    period_hours = max((search_end_time - search_start_time).total_seconds() / 3600.0, 0.01)

    prev_start_jst_str = "不明"
    prev_end_jst_str = "不明"
    if last_search_start_time_str and last_search_end_time_str:
        try:
            p_start = datetime.fromisoformat(last_search_start_time_str.replace("Z", "+00:00"))
            p_end = datetime.fromisoformat(last_search_end_time_str.replace("Z", "+00:00"))
            prev_start_jst_str = (p_start + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
            prev_end_jst_str = (p_end + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            print(f"前回期間のJST変換エラー: {e}")

    ai_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    
    tweets, raw_total_count = fetch_tweets_from_twitterapi_io(config, processed_ids, search_start_time, search_end_time, is_test_mode=is_test_mode)
    fetched_count = len(tweets)
    print(f"処理対象 of 新規ポスト: {fetched_count} 件 (API生取得: {raw_total_count} 件)")
    
    sent_count = 0
    skipped_count = 0
    error_count = 0
    total_input_tokens = 0
    total_output_tokens = 0
    skipped_tweets = []

    clean_kws = [k.replace('"', '').replace('#', '').strip() for k in display_kws]
    keyword_stats = {
        kw: {"fetched": 0, "sent": 0, "skipped": 0, "error": 0}
        for kw in clean_kws
    }
    keyword_stats["不明"] = {"fetched": 0, "sent": 0, "skipped": 0, "error": 0}

    for tweet in tweets:
        kw = tweet.get("matched_keyword", "不明")
        if kw in keyword_stats:
            keyword_stats[kw]["fetched"] += 1

    for i, tweet in enumerate(tweets, 1):
        kw = tweet.get("matched_keyword", "不明")
        try:
            print(f"[{i}/{fetched_count}] Tweet ID: {tweet['id']} ({kw}) を解析中...")
            analyzed_data = analyze_tweet_with_ai(ai_client, tweet, config)
            
            total_input_tokens += analyzed_data.get("input_tokens", 0)
            total_output_tokens += analyzed_data.get("output_tokens", 0)

            is_looking = analyzed_data.get("is_looking_for_photographer", True)
            is_tokyo_near = analyzed_data.get("is_tokyo_near", False)
            is_cosplay = analyzed_data.get("is_cosplay", False)
            is_excluded_genre = analyzed_data.get("is_excluded_genre", False)
            is_official_or_job = analyzed_data.get("is_official_or_job", False)
            is_noise = analyzed_data.get("is_noise", False)

            shooting_type = analyzed_data.get("shooting_type", "不明")
            location_name = analyzed_data.get("raw_location", "場所不明")
            ocr_text = analyzed_data.get("ocr_text", "なし")

            # 個別通知判定（都内近郊 & コスプレ & カメラマン募集 & 除外ジャンル以外 & 非公式・非求人 & 非ノイズ）
            is_valid_for_notification = (
                is_looking and 
                is_tokyo_near and 
                is_cosplay and 
                not is_excluded_genre and 
                not is_official_or_job and 
                not is_noise
            )

            if not is_valid_for_notification:
                skipped_count += 1
                group_key = ""
                reason = ""
                detailed_reason = ""

                # 優先度別グループ振り分け
                if is_cosplay and is_looking and not is_excluded_genre and not is_official_or_job and not is_noise and not is_tokyo_near:
                    # ① 【要確認・併せ募集】コスプレ併せ・撮影 (カメラマン募集あり・場所不明/都外判定)
                    group_key = "【要確認・併せ募集】コスプレ併せ・撮影 (カメラマン募集あり・場所不明/都外判定)"
                    reason = "エリア対象外/場所不明"
                    detailed_reason = f"コスプレ撮影ですが対象エリア外または場所不明 (判定: {location_name})"
                elif not is_cosplay and is_looking and not is_official_or_job and not is_noise:
                    # ② 【一般撮影】ポートレート・個人撮影 (カメラマン募集あり)
                    group_key = "【一般撮影】ポートレート・個人撮影 (カメラマン募集あり)"
                    reason = "撮影種別対象外 (コスプレ以外)"
                    detailed_reason = f"コスプレ以外の個人撮影 ({shooting_type}) / 判定場所: {location_name}"
                elif is_official_or_job:
                    # ③ 【企業・公式・求人】公式イベント / 企業雇用・スタッフ募集
                    group_key = "【企業・公式・求人】公式イベント / 企業雇用・スタッフ募集"
                    reason = "企業・公式求人"
                    detailed_reason = f"公式イベントカメラマンまたは企業・スタジオ求人募集 ({shooting_type})"
                elif is_excluded_genre:
                    # ④ 【除外ジャンル】指定除外作品
                    group_key = "【除外ジャンル】指定除外作品 (東リベ/ワートリ/呪術/ブルロ/金カム/P5等)"
                    reason = "除外ジャンル該当"
                    detailed_reason = f"除外対象ジャンルに該当 ({shooting_type})"
                else:
                    # ⑤ 【完全ノイズ・対象外】ゲーム募集 / 音楽ライブ / カメラマン非募集
                    group_key = "【完全ノイズ・対象外】ゲーム募集 / 音楽ライブ / 都外確定 / カメラマン非募集"
                    if is_noise:
                        reason = "非撮影ノイズ"
                        detailed_reason = f"ゲーム募集・音楽ライブ撮影・非撮影ノイズ ({shooting_type})"
                    elif not is_looking:
                        reason = "カメラマン非募集"
                        detailed_reason = "カメラマンを募集していません（被写体/レイヤーのみ募集等）"
                    else:
                        reason = "エリア対象外"
                        detailed_reason = f"対象エリア外 (判定: {location_name})"

                print(f" ➔ スキップ: {detailed_reason}")
                skipped_tweets.append({
                    "text": tweet["text"],
                    "url": f"https://x.com/i/status/{tweet['id']}",
                    "matched_keyword": kw,
                    "reason": reason,
                    "detailed_reason": detailed_reason,
                    "shooting_type": shooting_type,
                    "location": analyzed_data.get("location", "場所不明"),
                    "ocr_text": ocr_text,
                    "group_key": group_key
                })

                if not is_test_mode:
                    processed_ids.add(tweet["id"])
                    save_processed_ids(processed_ids, search_start_time_str, search_end_time_str, last_daily_summary_date, daily_history)
                if kw in keyword_stats:
                    keyword_stats[kw]["skipped"] += 1
                continue

            print(f" ➔ メール送信中...")
            send_single_email(analyzed_data, is_test_mode=is_test_mode, test_hours=test_hours)
            sent_count += 1
            print(f" ➔ 送信成功！")
            
            if not is_test_mode:
                processed_ids.add(tweet["id"])
                save_processed_ids(processed_ids, search_start_time_str, search_end_time_str, last_daily_summary_date, daily_history)

            if kw in keyword_stats:
                keyword_stats[kw]["sent"] += 1

        except Exception as e:
            print(f" ➔ エラーが発生しました (Tweet ID: {tweet.get('id')}): {e}")
            error_count += 1
            if kw in keyword_stats:
                keyword_stats[kw]["error"] += 1

    end_time_epoch = time.time()
    end_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    end_time_str = end_jst.strftime("%H:%M:%S")

    duration_sec = end_time_epoch - start_time_epoch
    minutes = int(duration_sec // 60)
    seconds = int(duration_sec % 60)
    duration_str = f"{minutes}分{seconds}秒" if minutes > 0 else f"{seconds}秒"

    if not is_test_mode:
        if today_str not in daily_history:
            daily_history[today_str] = {
                "fetched_count": 0, "raw_tweets_count": 0, "sent_count": 0, "skipped_count": 0, "error_count": 0,
                "input_tokens": 0, "output_tokens": 0, "keyword_stats": {}
            }

        daily_history[today_str]["fetched_count"] += fetched_count
        daily_history[today_str]["raw_tweets_count"] = daily_history[today_str].get("raw_tweets_count", 0) + raw_total_count
        daily_history[today_str]["sent_count"] += sent_count
        daily_history[today_str]["skipped_count"] += skipped_count
        daily_history[today_str]["error_count"] += error_count
        daily_history[today_str]["input_tokens"] += total_input_tokens
        daily_history[today_str]["output_tokens"] += total_output_tokens

        # 単語別統計の蓄積
        if "keyword_stats" not in daily_history[today_str]:
            daily_history[today_str]["keyword_stats"] = {}
        for kw, s in keyword_stats.items():
            if kw not in daily_history[today_str]["keyword_stats"]:
                daily_history[today_str]["keyword_stats"][kw] = {"fetched": 0, "sent": 0, "skipped": 0, "error": 0}
            daily_history[today_str]["keyword_stats"][kw]["fetched"] += s["fetched"]
            daily_history[today_str]["keyword_stats"][kw]["sent"] += s["sent"]
            daily_history[today_str]["keyword_stats"][kw]["skipped"] += s["skipped"]
            daily_history[today_str]["keyword_stats"][kw]["error"] += s["error"]

    summary_data = {
        "fetched_count": fetched_count,
        "raw_tweets_count": raw_total_count,
        "sent_count": sent_count,
        "skipped_count": skipped_count,
        "error_count": error_count,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "total_tokens": total_input_tokens + total_output_tokens,
        "skipped_tweets": skipped_tweets,
        "start_time": start_time_str,
        "end_time": end_time_str,
        "duration": duration_str,
        "display_keywords": display_kws,
        "target_period_start": target_start_str,
        "target_period_end": target_end_str,
        "period_hours": period_hours,
        "prev_period_start": prev_start_jst_str,
        "prev_period_end": prev_end_jst_str
    }

    if not is_test_mode:
        save_processed_ids(processed_ids, search_start_time_str, search_end_time_str, last_daily_summary_date, daily_history)

    try:
        print("実行サマリーメールを送信中...")
        send_summary_email(summary_data, is_test_mode=is_test_mode, test_hours=test_hours)
        print("サマリーメールの送信が完了しました。")
    except Exception as e:
        print(f"サマリーメールの送信中にエラーが発生しました: {e}")

    if sent_count > 0:
        print(f"合計 {sent_count} 件の通知メールを個別に送信しました。")
    else:
        print("該当する新しいポストはありませんでした。")

if __name__ == "__main__":
    main()
