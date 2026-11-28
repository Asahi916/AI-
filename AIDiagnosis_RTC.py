#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIDiagnosis_RTC  ―  基板AI診断RTC（OpenRTM-aist Python版）

【役割】
  InPort  "request_in"  (RTC.TimedString) : Camera_RTC から「診断の依頼」(JSON文字列) を受け取る
  OutPort "result_out"  (RTC.TimedString) : 診断の「経過・結果・エラー」(JSON文字列) を Display_RTC へ送る
  診断のあと、診断の記録(logs/diagnosis_log.csv)と、場所を示した写真(logs/images/)を保存する。

【受け取るJSON】（Camera_RTC が作る。送らない項目は空文字 ""）
  {"boards": ["C:/.../a.jpg", ...], "reference": "", "circuit": "", "parts": "",
   "symptom": "", "circuit_note": "", "parts_note": ""}
  ※ JSONではなく「画像1枚のパス文字列」だけが届いた場合は、基板の写真1枚として診断する。

【送るJSON】 "type" で種類を区別する
  {"type": "status", "text": "..."}                       経過
  {"type": "error",  "text": "..."}                       失敗
  {"type": "result", "id", "time", "model", "verdict", "summary", "detail",
   "marked_image", "log_saved", "log_error"}              結果

  ※ TimedString の中身は CORBA の string 型で、日本語を直接は通せない。
    そのため JSON は ensure_ascii=True（日本語を \\uXXXX に変換）で作る。受け取り側の json.loads で元に戻る。

【コンフィグレーションパラメータ】（RT System Editor で変更できる）
  api_key : Gemini APIキー。"none" のままなら次の順で探す
            ① 環境変数 GEMINI_API_KEY / GOOGLE_API_KEY  ② このファイルと同じ場所の gemini_api_key.txt
  models  : 使うモデル名をカンマ区切りで。"auto" のままなら下の MODELS を上から順に試す

【準備】
  pip install google-genai pillow
  APIキー: https://aistudio.google.com/apikey （Googleアカウントのみ・クレカ不要）

【起動】
  コマンドプロンプトで   python AIDiagnosis_RTC.py
  ネームサーバー(localhost)を起動しておき、RT System Editor でポートをつないでアクティブ化する。
  終了は Ctrl+C。

【自動で作られるもの】（このファイルと同じ場所。Display_RTC.py も同じ場所の logs/ を使う）
  logs/diagnosis_log.csv   診断の記録
  logs/images/             場所を示した写真
"""
import csv
import io
import json
import mimetypes
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:  # Pillowが無くても診断は動く（縮小・場所の表示は使えない）
    Image = ImageDraw = ImageFont = ImageOps = None

try:
    import RTC
    import OpenRTM_aist
except ImportError as _e:
    print("OpenRTM_aist / RTC を読み込めません: %s" % _e)
    print("OpenRTM-aist (Python版) と omniORBpy のインストールを確認してください。")
    sys.exit(1)

# 無料枠で使えるモデルを上から順に試す（2026年11月時点の公式情報に基づく）。
# 404（モデルが無い）や429（無料枠の上限）のときは次のモデルへ自動で切り替える。
# gemini-2.5-* は「過去に使ったことがある人」だけに制限されているため最後に置く。
# 名前は変わるので、コンフィグレーションパラメータ models で上書きできる。
MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
          "gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.5-flash", "gemini-2.5-flash-lite"]

RETRY_WAITS = [3, 8, 15]   # 混雑(500/503/504)のとき、同じモデルで待ってやり直す秒数
MAX_SIDE = 1600            # 写真の長辺の上限(px)
REQUEST_TIMEOUT_MS = 120000

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
IMG_DIR = os.path.join(LOG_DIR, "images")
LOG_CSV = os.path.join(LOG_DIR, "diagnosis_log.csv")
KEY_FILE = os.path.join(BASE_DIR, "gemini_api_key.txt")

# スペック(仕様)の文字列は CORBA に渡るので、必ず ASCII(英数字)だけにする
aidiagnosis_spec = [
    "implementation_id", "AIDiagnosis_RTC",
    "type_name", "AIDiagnosis_RTC",
    "description", "Board diagnosis with Gemini API",
    "version", "1.0.0",
    "vendor", "board_ai",
    "category", "Diagnosis",
    "activity_type", "DataFlowComponent",
    "max_instance", "1",
    "language", "Python",
    "lang_type", "SCRIPT",
    "exec_cxt.periodic.type", "PeriodicExecutionContext",
    "exec_cxt.periodic.rate", "20.0",
    "conf.default.api_key", "none",
    "conf.default.models", "auto",
    "conf.__widget__.api_key", "text",
    "conf.__widget__.models", "text",
    "conf.__type__.api_key", "string",
    "conf.__type__.models", "string",
    ""]

SYSTEM_PROMPT = """あなたは電子工作の初心者（中高生）を助ける、電子回路基板の診断の専門家です。
ユーザーが送る基板の写真（と、あれば正常な基板の写真・回路図・部品の情報・症状の説明）を見て、動かない原因を考えてください。

守ること:
- 専門用語はなるべく使わない。使うときは（ ）で短く説明する。
- 写真から実際に見えることと、推測を分ける。見えないものを断定しない。
- 次の観点で確認する: 部品の向き（LED・電解コンデンサ・IC・ダイオードの極性）、
  取り付け位置の間違い（抵抗値の入れ替わり等）、はんだ不良（つながっていない・ブリッジ・イモはんだ）、
  部品の欠品、配線ミス、焦げ・変色。
- 「正常に完成した同じ基板の写真」が付いているときは、必ずそれと見比べて、
  違う所（部品の有無・向き・はんだの様子）を探す。
- 回路図・部品の写真や説明が付いているときは、それを判断の根拠にする。付いていないものは推測で補わない。
- 写真だけで判断できないときは、無理に結論を出さず、
  初心者でもできる追加の確認（別角度の撮り直し、テスターで測る場所と測り方、症状の詳しい様子）を具体的に頼む。
- 電池・電源は外してから触るよう、安全に関する注意も必要なら一言添える。

文章のルール:
- 友達に教えるような、やさしい話し言葉で書く。あいさつ、前置き、「！」の連発はしない。
- Markdown記法（** や # や ---）は絶対に使わない。箇条書きは「・」か「1.」だけを使う。

出力は3つの部分に分け、部分の間に区切り行を入れる。

【1つめ: まとめ】全体で200字以内。必ず次の形にする。
判定: （「異常なし」「異常の疑いあり」「写真だけでは判断できない」のどれかと、理由を一言）
怪しい所:
1. （場所と理由を1行で）
2. （同上。最大3つまで。番号を付ける）
次にやること:
・（1行で。最大3つ）
（電源が入ったままだと危険そうなときだけ、最後に注意を1行）

===詳細===
【2つめ: 詳細】まとめより必ず長く、くわしく書く（まとめの3倍以上の分量）。
まとめに書いた内容（判定・怪しい所・次にやること）をすべて含め、それぞれに説明を足す。
次の見出しを使い、各項目を2〜4文で書く。
【判定と理由】
【写真から見えたこと】（正常な写真があれば、違いも）
【怪しい所の説明】（まとめの番号ごとに「どこか」「なぜ怪しいか」「どうすれば確かめられるか」）
【次にやること】（番号付きの手順。テスターで測るなら、どこに何を当てて、どんな値なら正常かまで書く）
【追加で教えてほしいこと】（無ければ「特になし」）

===場所===
【3つめ: 場所】「怪しい所」の番号と対応する、写真の中の位置を、次のJSONだけで書く（説明文は書かない）。
[{"no": 1, "label": "J6端子", "box_2d": [ymin, xmin, ymax, xmax]}]
box_2d は「基板の写真 1」の中の枠で、縦横とも0〜1000に正規化した数値。
位置を特定できないものは含めない。1つも無ければ [] とだけ書く。
"""

DETAIL_MARK = "===詳細==="
LOC_MARK = "===場所==="


class DiagnosisError(Exception):
    pass


class LogError(Exception):
    pass


def say(msg):
    """コンソールに時刻つきで表示する。"""
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)


# ---------------------------------------------------------------- RTC間のデータ（JSON文字列）
def pack(obj):
    """辞書 → JSON文字列。日本語は \\uXXXX にして、CORBAのstring型でも安全な ASCII だけにする。"""
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


def unpack(text):
    """JSON文字列 → 辞書。読めなければ None。"""
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except (ValueError, TypeError):
        return None


def decode_request(text):
    """届いた文字列から診断の依頼(辞書)を作る。使えなければ None。"""
    text = (text or "").strip()
    if not text:
        return None
    d = unpack(text) if text.startswith("{") else None
    if d is None:  # 画像パスだけの文字列
        p = text.strip().strip('"').strip("'")
        d = {"boards": [p]} if p else None
    if d is None:
        return None
    boards = d.get("boards", d.get("board", []))
    if isinstance(boards, str):
        boards = [boards]

    def s(key):
        v = d.get(key, "")
        return v.strip() if isinstance(v, str) else ""

    return {"boards": [b.strip() for b in boards if isinstance(b, str) and b.strip()],
            "reference": s("reference"), "circuit": s("circuit"), "parts": s("parts"),
            "symptom": s("symptom"), "circuit_note": s("circuit_note"), "parts_note": s("parts_note")}


def check_files(job):
    """依頼の画像が全部あるか調べる。足りなければ説明文、問題なければ ""。"""
    if not job["boards"]:
        return "診断する基板の写真のパスが入っていません。"
    paths = list(job["boards"]) + [job[k] for k in ("reference", "circuit", "parts") if job[k]]
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        return "画像ファイルが見つかりません（別のPCのパスではありませんか？）:\n" + "\n".join(missing)
    return ""


# ---------------------------------------------------------------- APIキー・モデルの決定
def resolve_api_key(conf_value):
    """コンフィグ → 環境変数 → gemini_api_key.txt の順で探す。"""
    for v in (conf_value, os.environ.get("GEMINI_API_KEY"), os.environ.get("GOOGLE_API_KEY")):
        v = (v or "").strip()
        if v and v.lower() not in ("none", "auto"):
            return v
    try:
        with open(KEY_FILE, encoding="utf-8-sig") as f:
            return f.read().strip()
    except OSError:
        return ""


def resolve_models(conf_value):
    names = [m.strip() for m in (conf_value or "").split(",") if m.strip()]
    if not names or names[0].lower() in ("auto", "none"):
        return list(MODELS)
    return names


# ---------------------------------------------------------------- 回答の整形
def clean_text(text):
    """AIがMarkdown記号を混ぜた場合に備えて、見づらい記号を取り除く。"""
    out = []
    for line in text.splitlines():
        s = line.replace("**", "").replace("__", "")
        t = s.lstrip()
        if t.startswith("---"):
            continue
        if t.startswith("#"):
            s = t.lstrip("# ").strip()
        elif t.startswith("* ") or t.startswith("- "):
            s = "・" + t[2:]
        out.append(s)
    return "\n".join(out).strip()


def parse_marks(text):
    """「場所」部分のJSONを読み取り、枠のリストにする。読めなければ空リスト。"""
    if not text:
        return []
    i, j = text.find("["), text.rfind("]")
    if i < 0 or j <= i:
        return []
    try:
        data = json.loads(text[i:j + 1])
    except ValueError:
        return []
    marks = []
    for k, d in enumerate(data if isinstance(data, list) else [], 1):
        try:
            box = [max(0.0, min(1000.0, float(v))) for v in d["box_2d"]]  # ymin, xmin, ymax, xmax
            if len(box) != 4 or box[2] <= box[0] or box[3] <= box[1]:
                continue
            marks.append({"no": int(d.get("no", k)), "label": str(d.get("label", "")), "box": box})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return marks[:6]


def split_answer(text):
    """回答を (まとめ, 詳細, 場所のリスト) に分ける。区切りが無ければ全体をまとめとして扱う。"""
    marks_text = ""
    if LOC_MARK in text:
        text, _, marks_text = text.partition(LOC_MARK)
    detail = ""
    if DETAIL_MARK in text:
        text, _, detail = text.partition(DETAIL_MARK)
    return text.strip(), detail.strip(), parse_marks(marks_text)


def detail_view(summary, detail):
    """「詳しく見る」で出す文章。詳細がまとめより短いときは、まとめも付けて情報が減らないようにする。"""
    if not detail:
        return ""
    if len(detail) < len(summary) * 1.5:
        return summary + "\n\n――――――――\n" + detail
    return detail


# ---------------------------------------------------------------- 画像
def open_pil(path):
    """向き情報を反映したRGB画像を返す（Pillow必須）。"""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img) or img
    return img.convert("RGB")


def load_image(path):
    """画像ファイルを (mime_type, バイト列) にする。Pillowがあれば縮小する。"""
    if Image is not None:
        try:
            img = open_pil(path)
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            return "image/jpeg", buf.getvalue()
        except Exception as e:
            raise DiagnosisError("画像を開けませんでした: %s (%s)" % (os.path.basename(path), e))
    mime = mimetypes.guess_type(path)[0] or ""
    if mime not in ("image/jpeg", "image/png", "image/webp"):
        raise DiagnosisError("JPEG / PNG / WebP の画像を選んでください: %s" % os.path.basename(path))
    with open(path, "rb") as f:
        data = f.read()
    if len(data) > 12 * 1024 * 1024:
        raise DiagnosisError("画像が大きすぎます（12MB以下に）。Pillowを入れると自動で縮小します。")
    return mime, data


def make_marked_image(path, marks):
    """基板の写真に、怪しい所の赤い枠と番号を描いた画像を返す（Pillow必須）。"""
    img = open_pil(path)
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    draw = ImageDraw.Draw(img)
    w, h = img.size
    lw = max(3, w // 250)
    size = max(22, w // 30)
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # 古いPillow
        font = ImageFont.load_default()
    red = (255, 40, 40)
    for m in marks:
        y0, x0, y1, x1 = m["box"]
        box = (x0 * w / 1000, y0 * h / 1000, x1 * w / 1000, y1 * h / 1000)
        draw.rectangle(box, outline=red, width=lw)
        label = str(m["no"])
        tb = draw.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        bx, by = box[0], max(0, box[1] - th - 12)
        draw.rectangle((bx, by, bx + tw + 14, by + th + 12), fill=red)
        draw.text((bx + 7, by + 4 - tb[1]), label, fill=(255, 255, 255), font=font)
    return img


# ---------------------------------------------------------------- 診断の記録（Display_RTC.py と同じ形式）
LOG_FIELDS = ["ID", "日時", "写真", "症状", "回路図", "部品の情報", "モデル", "AIの判定",
              "実際の状態", "実際の原因", "撮影条件", "判定の自動照合", "原因の当たり外れ",
              "まとめ", "詳細", "場所を示した写真"]


def ai_verdict(summary):
    m = re.search(r"判定[:：]\s*(.+)", summary)
    line = m.group(1) if m else summary[:60]
    if "異常なし" in line:
        return "異常なし"
    if "異常の疑い" in line:
        return "異常の疑いあり"
    if "判断できない" in line:
        return "判断できない"
    return "不明"


def new_log_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _prepare_log_file():
    """古い形式の記録ファイルがあれば、別名に退避して新しく作り直す。新規ならTrue。"""
    if not os.path.exists(LOG_CSV):
        return True
    with open(LOG_CSV, newline="", encoding="utf-8-sig") as f:
        header = next(csv.reader(f), [])
    if header == LOG_FIELDS:
        return False
    os.replace(LOG_CSV, os.path.join(LOG_DIR, "diagnosis_log_old_%s.csv" % datetime.now().strftime("%H%M%S")))
    return True


def append_log(row):
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        is_new = _prepare_log_file()
        # utf-8-sig: Excelで文字化けせずに開ける
        with open(LOG_CSV, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if is_new:
                w.writeheader()
            w.writerow(row)
    except PermissionError:
        raise LogError("記録ファイルに書き込めません。Excelで開いている場合は閉じてください。")


# ---------------------------------------------------------------- Gemini API（公式 google-genai ライブラリ）
def build_contents(types, job, images):
    """AIに送る内容を作る。images は {"boards": [...], "reference": ..., "circuit": ..., "parts": ...}。"""
    boards = images["boards"]
    lines = ["診断してほしい基板の写真が%d枚あります。" % len(boards)]
    lines.append("症状・状況: " + (job["symptom"] or "（特に書かれていない。動作がおかしい）"))
    if images["reference"]:
        lines.append("正常に完成した同じ基板の写真（比較用）が付いています。")
    if images["circuit"]:
        lines.append("回路図の画像が付いています。")
    if job["circuit_note"]:
        lines.append("回路図についての説明（手書き）:\n" + job["circuit_note"])
    if images["parts"]:
        lines.append("使用部品の写真（または部品表の写真）が付いています。")
    if job["parts_note"]:
        lines.append("使用部品の情報（手書き）:\n" + job["parts_note"])
    parts = [types.Part(text="\n".join(lines))]

    def add(label, img):
        parts.append(types.Part(text=label))
        parts.append(types.Part.from_bytes(data=img[1], mime_type=img[0]))

    for i, img in enumerate(boards, 1):
        add("[基板の写真 %d]" % i, img)
    if images["reference"]:
        add("[正常に完成した同じ基板の写真（比較用）]", images["reference"])
    if images["circuit"]:
        add("[回路図]", images["circuit"])
    if images["parts"]:
        add("[使用部品の写真]", images["parts"])
    return [types.Content(role="user", parts=parts)]


def extract_text(resp):
    fb = getattr(resp, "prompt_feedback", None)
    block = getattr(fb, "block_reason", None) if fb else None
    if block:
        raise DiagnosisError("AIが回答を返しませんでした（理由: %s）。別の写真で試してください。" % block)
    cands = getattr(resp, "candidates", None) or []
    if not cands:
        raise DiagnosisError("AIから回答が返ってきませんでした。もう一度試してください。")
    content = getattr(cands[0], "content", None)
    parts = getattr(content, "parts", None) or []
    text = clean_text("".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False)))
    if not text:
        raise DiagnosisError("回答が空でした（finishReason: %s）。もう一度試してください。"
                             % getattr(cands[0], "finish_reason", None))
    return text


def explain_denied(code, msg):
    """400/401/403 のエラーを、原因ごとの案内文にする。キーの間違いと決めつけない。"""
    low = msg.lower()
    raw = "\n\n（Googleからのメッセージ: HTTP %s %s）" % (code, msg[:300])
    if "free tier" in low or "billing" in low:
        return ("このGoogleプロジェクトでは、Gemini APIの無料枠が使えません。キーの入力ミスではありません。\n"
                "・学校や会社のGoogleアカウント（Workspace）、年齢制限のあるアカウントでは、無料枠が使えないことがあります\n"
                "・個人のGoogleアカウントで https://aistudio.google.com/apikey から新しいキーを作り直す\n"
                "・または課金の設定をして有料枠に切り替える（使った分だけ料金がかかります）" + raw)
    if "location" in low or "region" in low:
        return "この地域・環境からはGemini APIを使えません。" + raw
    if code == 401 or "api key" in low or "api_key" in low:
        return ("APIキーが正しくないか、期限切れ・無効になっています。キーを作り直して設定し直してください。" + raw)
    if code == 403:
        return "このキーではこの操作が許可されていません（権限がありません）。" + raw
    return "リクエストが受け付けられませんでした。" + raw


def diagnose(api_key, job, models=None, on_status=None):
    """診断を実行して (回答テキスト, 使ったモデル名) を返す。失敗時は DiagnosisError。"""
    if not api_key:
        raise DiagnosisError(
            "APIキーが見つかりません。次のどれかで設定してください。\n"
            "・RT System Editor で AIDiagnosis_RTC のコンフィグレーション api_key に入れる\n"
            "・環境変数 GEMINI_API_KEY に入れる（設定後はコマンドプロンプトを開き直す）\n"
            "・%s にキーだけを書いて保存する" % KEY_FILE)
    try:
        from google import genai
        from google.genai import errors as genai_errors
        from google.genai import types
    except ImportError:
        raise DiagnosisError("google-genai ライブラリが見つかりません。\npip install google-genai を実行してください。")

    if on_status:
        on_status("画像を準備中…")
    images = {
        "boards": [load_image(p) for p in job["boards"]],
        "reference": load_image(job["reference"]) if job["reference"] else None,
        "circuit": load_image(job["circuit"]) if job["circuit"] else None,
        "parts": load_image(job["parts"]) if job["parts"] else None,
    }
    contents = build_contents(types, job, images)
    config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.3)
    try:
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS))
    except Exception as e:
        raise DiagnosisError("Geminiクライアントを作れませんでした: %s" % e)

    failures = []
    for model in (models or MODELS):
        # 混雑(500/503/504)は一時的なことが多いので、同じモデルで待ってから数回やり直す
        for attempt in range(len(RETRY_WAITS) + 1):
            if on_status:
                on_status("AIに送信中… (%s)%s" % (model, "  やり直し %d回目" % attempt if attempt else ""))
            try:
                resp = client.models.generate_content(model=model, contents=contents, config=config)
                return extract_text(resp), model
            except genai_errors.APIError as e:
                code = getattr(e, "code", 0) or 0
                msg = str(getattr(e, "message", "") or e)
                if code in (400, 401, 403):
                    # どのモデルでも同じ原因なので、次のモデルは試さず、原因に合わせた案内を出す
                    raise DiagnosisError(explain_denied(code, msg))
                if code in (500, 503, 504) and attempt < len(RETRY_WAITS):
                    if on_status:
                        on_status("%s が混雑中。%d秒待ってやり直します…" % (model, RETRY_WAITS[attempt]))
                    time.sleep(RETRY_WAITS[attempt])
                    continue
                # 404=モデルなし / 429=無料枠の上限 / 待っても混雑 → 次のモデルへ
                failures.append("%s: HTTP %s %s" % (model, code, msg[:120].replace("\n", " ")))
                break
            except DiagnosisError:
                raise
            except Exception as e:
                name = type(e).__name__
                if "Timeout" in name:
                    failures.append("%s: 時間切れ" % model)
                    break
                if "Connect" in name or "Network" in name:
                    raise DiagnosisError("ネットワークに接続できません。インターネット接続を確認してください。(%s)" % e)
                raise DiagnosisError("予期しないエラー: %s: %s" % (name, e))
    raise DiagnosisError(
        "どのモデルでも失敗しました。Google側が混雑しているか、無料枠の上限に達した可能性があります。\n"
        "数分〜数十分あけてから、もう一度試してください。\n"
        "モデル名が古い場合は、コンフィグレーション models に新しい名前を入れてください。\n\n"
        "各モデルのエラー:\n" + "\n".join(failures))


def process_job(job, on_status):
    """1件の診断依頼を最後まで処理して、Display_RTC へ送る結果(辞書)を返す。"""
    text, model = diagnose(job["api_key"], job, job["models"], on_status)
    summary, detail, marks = split_answer(text)
    log_id = new_log_id()

    marked_path = ""
    if marks and Image is not None:
        try:
            img = make_marked_image(job["boards"][0], marks)
            os.makedirs(IMG_DIR, exist_ok=True)
            marked_path = os.path.join(IMG_DIR, log_id + ".jpg")
            img.save(marked_path, quality=90)
        except Exception:
            marked_path = ""

    verdict = ai_verdict(summary)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_saved, log_error = True, ""
    try:
        append_log({
            "ID": log_id, "日時": now,
            "写真": " / ".join(job["boards"]), "症状": job["symptom"],
            "回路図": os.path.basename(job["circuit"]) if job["circuit"] else "",
            "部品の情報": job["parts_note"], "モデル": model,
            "AIの判定": verdict, "実際の状態": "", "実際の原因": "", "撮影条件": "",
            "判定の自動照合": "", "原因の当たり外れ": "",
            "まとめ": summary, "詳細": detail, "場所を示した写真": marked_path,
        })
    except (LogError, OSError) as e:
        log_saved, log_error = False, str(e)

    return {"type": "result", "id": log_id, "time": now, "model": model, "verdict": verdict,
            "summary": summary, "detail": detail_view(summary, detail),
            "marked_image": marked_path, "log_saved": log_saved, "log_error": log_error}


# ---------------------------------------------------------------- RTCとの受け渡し
class Bridge:
    """RTC(onExecute) と 診断スレッドの間でデータを受け渡す箱。"""

    def __init__(self):
        self.jobs = queue.Queue()     # 診断の依頼 (onExecute → 診断スレッド)
        self.outbox = queue.Queue()   # 送信するJSON文字列 (診断スレッド → onExecute)
        self.active = False

    def emit(self, obj):
        self.outbox.put(pack(obj))


BRIDGE = Bridge()


def worker_loop(bridge):
    """診断を1件ずつ処理するスレッド。時間がかかるので、RTCの周期処理(onExecute)とは分けてある。"""
    while True:
        job = bridge.jobs.get()
        if job is None:
            break
        try:
            say("診断を始めます（写真%d枚）" % len(job["boards"]))
            bridge.emit({"type": "status", "text": "診断を開始します…"})

            def on_status(s):
                say(s)
                bridge.emit({"type": "status", "text": s})

            result = process_job(job, on_status)
            say("診断が終わりました（モデル: %s / 判定: %s）" % (result["model"], result["verdict"]))
            bridge.emit(result)
        except DiagnosisError as e:
            say("失敗: %s" % str(e).splitlines()[0])
            bridge.emit({"type": "error", "text": str(e)})
        except Exception as e:  # 想定外でもスレッドが止まらないように
            say("予期しないエラー: %r" % e)
            bridge.emit({"type": "error", "text": "予期しないエラー: %r" % e})


# ---------------------------------------------------------------- RTC本体
class AIDiagnosis_RTC(OpenRTM_aist.DataFlowComponentBase):
    def __init__(self, manager):
        OpenRTM_aist.DataFlowComponentBase.__init__(self, manager)
        self._api_key = ["none"]   # コンフィグレーション: 値は [0] に入る
        self._models = ["auto"]
        self._worker = None

    def onInitialize(self):
        # ポート（TimedString型）
        self._d_in = RTC.TimedString(RTC.Time(0, 0), "")
        self._inIn = OpenRTM_aist.InPort("request_in", self._d_in)
        self._d_out = RTC.TimedString(RTC.Time(0, 0), "")
        self._outOut = OpenRTM_aist.OutPort("result_out", self._d_out)
        self.addInPort("request_in", self._inIn)
        self.addOutPort("result_out", self._outOut)

        # コンフィグレーションパラメータ
        self.bindParameter("api_key", self._api_key, "none")
        self.bindParameter("models", self._models, "auto")

        # 診断スレッド
        self._worker = threading.Thread(target=worker_loop, args=(BRIDGE,), daemon=True)
        self._worker.start()
        say("AIDiagnosis_RTC を初期化しました")
        return RTC.RTC_OK

    def onActivated(self, ec_id):
        BRIDGE.active = True
        say("アクティブになりました（診断の依頼を待っています）")
        return RTC.RTC_OK

    def onDeactivated(self, ec_id):
        BRIDGE.active = False
        say("非アクティブになりました")
        return RTC.RTC_OK

    def onExecute(self, ec_id):
        # ① 届いた依頼を診断スレッドへ渡す
        n = 0
        while n < 20 and self._inIn.isNew():
            n += 1
            data = self._inIn.read()
            self._accept(getattr(data, "data", ""))
        # ② 診断スレッドが作った経過・結果を送信する
        while True:
            try:
                text = BRIDGE.outbox.get_nowait()
            except queue.Empty:
                break
            self._send(text)
        return RTC.RTC_OK

    def onFinalize(self):
        BRIDGE.jobs.put(None)
        return RTC.RTC_OK

    def _accept(self, text):
        job = decode_request(text)
        if job is None:
            say("読み取れないデータが届きました")
            BRIDGE.emit({"type": "error", "text": "読み取れないデータが届きました。"})
            return
        problem = check_files(job)
        if problem:
            say("依頼を受け付けられません: %s" % problem.splitlines()[0])
            BRIDGE.emit({"type": "error", "text": problem})
            return
        # 設定はここ(RTCのスレッド)で読み、診断スレッドへ渡す
        job["api_key"] = resolve_api_key(str(self._api_key[0]))
        job["models"] = resolve_models(str(self._models[0]))
        waiting = BRIDGE.jobs.qsize()
        BRIDGE.jobs.put(job)
        say("診断の依頼を受け取りました（写真%d枚）" % len(job["boards"]))
        if waiting:
            BRIDGE.emit({"type": "status", "text": "順番待ち: 前に %d 件あります…" % waiting})

    def _send(self, text):
        self._d_out.data = text
        try:
            OpenRTM_aist.setTimestamp(self._d_out)
        except Exception:
            pass
        try:
            self._outOut.write()
        except Exception as e:
            say("送信に失敗しました: %r" % e)


def MyModuleInit(manager):
    profile = OpenRTM_aist.Properties(defaults_str=aidiagnosis_spec)
    manager.registerFactory(profile, AIDiagnosis_RTC, OpenRTM_aist.Delete)
    comp = manager.createComponent("AIDiagnosis_RTC")
    if comp is None:
        say("AIDiagnosis_RTC を作れませんでした")


def main():
    mgr = OpenRTM_aist.Manager.init(sys.argv)
    mgr.setModuleInitProc(MyModuleInit)
    mgr.activateManager()
    mgr.runManager(True)   # True: 別スレッドで動かし、この関数は先へ進む
    say("起動しました。RT System Editor でポートをつないで、アクティブ化してください。（終了: Ctrl+C）")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            mgr.shutdown()
        except Exception:
            pass
        sys.stdout.flush()
        os._exit(0)  # 終了時にスレッドが残って固まるのを防ぐ


if __name__ == "__main__":
    main()
