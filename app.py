"""
減量ログ - Streamlit版 (Supabase永続化・スマホ最適化版)
Claude Artifact (React) から移植した減量・コンディション管理ツール。

実行方法:
    pip install -r requirements.txt
    streamlit run app.py

.streamlit/secrets.toml に以下を設定してください:
    SUPABASE_URL = "https://xxxx.supabase.co"
    SUPABASE_KEY = "sb_publishable_xxxx"

画像自動入力機能を使う場合は、Anthropic APIキーが必要です(画面で入力するか、
環境変数 ANTHROPIC_API_KEY を設定してください)。
"""

import base64
import io
import json
import math
import os
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from supabase import create_client

DEFAULT_CONFIG = {
    "fightDate": None,
    "weighInDate": None,
    "targetWeight": None,
    "startWeight": None,
}

SKIN_LABELS = ["絶好調", "良好", "普通", "やや荒れ", "悪化"]
COND_LABELS = ["絶好調", "良好", "普通", "きつい", "限界近い"]

METRICS = [
    ("weight", "体重", "kg", "基本"),
    ("bodyFat", "体脂肪率", "%", "基本"),
    ("bodyWater", "体水分率", "%", "基本"),
    ("calories", "消費カロリー", "kcal", "基本"),
    ("sleepHours", "睡眠時間", "h", "基本"),
    ("sleepScore", "睡眠スコア", "", "基本"),
    ("muscleMass", "筋肉量", "kg", "体組成 詳細"),
    ("bmi", "BMI", "", "体組成 詳細"),
    ("visceralFat", "内臓脂肪レベル", "", "体組成 詳細"),
    ("boneMass", "推定骨量", "kg", "体組成 詳細"),
    ("basalMetabolism", "基礎代謝量", "kcal", "体組成 詳細"),
    ("bodyAge", "体内年齢", "才", "体組成 詳細"),
    ("muscleScore", "筋肉スコア", "", "体組成 詳細"),
    ("muscleQualityScore", "筋質点数", "", "体組成 詳細"),
    ("restingHR", "安静時脈拍", "拍/分", "体組成 詳細"),
    ("armMuscleR", "右腕筋肉量", "kg", "部位別 筋肉量"),
    ("armMuscleL", "左腕筋肉量", "kg", "部位別 筋肉量"),
    ("legMuscleR", "右脚筋肉量", "kg", "部位別 筋肉量"),
    ("legMuscleL", "左脚筋肉量", "kg", "部位別 筋肉量"),
    ("trunkMuscle", "体幹筋肉量", "kg", "部位別 筋肉量"),
    ("armFatR", "右腕脂肪率", "%", "部位別 脂肪率"),
    ("armFatL", "左腕脂肪率", "%", "部位別 脂肪率"),
    ("legFatR", "右脚脂肪率", "%", "部位別 脂肪率"),
    ("legFatL", "左脚脂肪率", "%", "部位別 脂肪率"),
    ("trunkFat", "体幹脂肪率", "%", "部位別 脂肪率"),
    ("armQualityR", "右腕筋質点数", "", "部位別 筋質点数"),
    ("armQualityL", "左腕筋質点数", "", "部位別 筋質点数"),
    ("legQualityR", "右脚筋質点数", "", "部位別 筋質点数"),
    ("legQualityL", "左脚筋質点数", "", "部位別 筋質点数"),
]
METRIC_BY_KEY = {m[0]: m for m in METRICS}
BASIC_KEYS = [m[0] for m in METRICS if m[3] == "基本"]
ADVANCED_METRICS = [m for m in METRICS if m[3] != "基本"]
ADVANCED_GROUPS = sorted(set(m[3] for m in ADVANCED_METRICS), key=lambda g: [m[3] for m in ADVANCED_METRICS].index(g))
ALL_NUMERIC_KEYS = [m[0] for m in METRICS]

TABLE_EXTRA_LABELS = {"mbaRating": ("MBA判定", ""), "targetHRRange": ("運動時目標脈拍", "")}

NATIVE_KEYS = {"date", "weight", "bodyFat", "muscleMass", "bodyWater", "notes"}


def label_for(key):
    if key in METRIC_BY_KEY:
        _, label, unit, _ = METRIC_BY_KEY[key]
        return label, unit
    return TABLE_EXTRA_LABELS.get(key, (key, ""))


@st.cache_resource
def get_supabase():
    url = st.secrets["SUPABASE_URL"].strip()
    key = st.secrets["SUPABASE_KEY"].strip()
    for name, value in (("SUPABASE_URL", url), ("SUPABASE_KEY", key)):
        try:
            value.encode("ascii")
        except UnicodeEncodeError as e:
            bad_char = value[e.start:e.end]
            st.error(
                f"Secretsの{name}に半角ASCII以外の文字が含まれています"
                f"(該当文字: {bad_char!r} / コード: U+{ord(bad_char):04X})。\n\n"
                "多くの場合、Streamlit CloudのSecrets欄に貼り付けた際に日本語IMEが"
                "オンになっていて、全角コロン「：」や全角スラッシュ「／」、"
                "全角スペースなどが紛れ込んでいます。IMEをオフにした状態で"
                f"{name}を一度削除し、半角英数字のみで貼り付け直してください。"
            )
            st.stop()
    return create_client(url, key)

def _row_to_entry(row):
    entry = {
        "date": row["measure_date"],
        "weight": row.get("weight"),
        "bodyFat": row.get("body_fat"),
        "muscleMass": row.get("muscle"),
        "bodyWater": row.get("water"),
        "notes": row.get("memo"),
    }
    entry.update(dict(row.get("extra") or {}))
    entry.setdefault("skin", 2)
    entry.setdefault("cond", 2)
    return entry


def load_entries():
    sb = get_supabase()
    res = sb.table("weight_logs").select("*").order("measure_date").execute()
    return [_row_to_entry(row) for row in res.data]


def upsert_entry(entry):
    from postgrest.exceptions import APIError

    sb = get_supabase()
    extra = {k: v for k, v in entry.items() if k not in NATIVE_KEYS}
    row = {
        "measure_date": entry["date"],
        "weight": entry.get("weight"),
        "body_fat": entry.get("bodyFat"),
        "muscle": entry.get("muscleMass"),
        "water": entry.get("bodyWater"),
        "memo": entry.get("notes"),
        "extra": extra,
    }
    try:
        sb.table("weight_logs").upsert(row, on_conflict="measure_date").execute()
    except APIError as e:
        st.error(
            "記録の保存に失敗しました(APIError)。\n\n"
            f"message: {e.message}\n"
            f"code: {getattr(e, 'code', None)}\n"
            f"details: {getattr(e, 'details', None)}\n"
            f"hint: {getattr(e, 'hint', None)}"
        )
        st.stop()
    except Exception as e:
        st.error(f"記録の保存に失敗しました: {e}")
        st.stop()

def delete_entry(d):
    from postgrest.exceptions import APIError

    sb = get_supabase()
    try:
        sb.table("weight_logs").delete().eq("measure_date", d).execute()
    except APIError as e:
        st.error(
            "記録の削除に失敗しました(APIError)。\n\n"
            f"message: {e.message}\n"
            f"code: {getattr(e, 'code', None)}\n"
            f"details: {getattr(e, 'details', None)}\n"
            f"hint: {getattr(e, 'hint', None)}"
        )
        st.stop()
    except Exception as e:
        st.error(f"記録の削除に失敗しました: {e}")
        st.stop()


def delete_all_entries():
    from postgrest.exceptions import APIError

    sb = get_supabase()
    try:
        sb.table("weight_logs").delete().gte("measure_date", "1900-01-01").execute()
    except APIError as e:
        st.error(
            "全データの削除に失敗しました(APIError)。\n\n"
            f"message: {e.message}\n"
            f"code: {getattr(e, 'code', None)}\n"
            f"details: {getattr(e, 'details', None)}\n"
            f"hint: {getattr(e, 'hint', None)}"
        )
        st.stop()
    except Exception as e:
        st.error(f"全データの削除に失敗しました: {e}")
        st.stop()


def load_config():
    sb = get_supabase()
    res = sb.table("app_config").select("*").eq("id", 1).execute()
    if res.data:
        row = res.data[0]
        return {
            "weighInDate": row.get("weigh_in_date"),
            "fightDate": row.get("fight_date"),
            "startWeight": row.get("start_weight"),
            "targetWeight": row.get("target_weight"),
        }
    return dict(DEFAULT_CONFIG)


def save_config(config):
    from postgrest.exceptions import APIError

    sb = get_supabase()
    try:
        sb.table("app_config").upsert({
            "id": 1,
            "weigh_in_date": config.get("weighInDate"),
            "fight_date": config.get("fightDate"),
            "start_weight": config.get("startWeight"),
            "target_weight": config.get("targetWeight"),
        }, on_conflict="id").execute()
    except APIError as e:
        st.error(
            "app_configへの保存に失敗しました(APIError)。\n\n"
            f"message: {e.message}\n"
            f"code: {getattr(e, 'code', None)}\n"
            f"details: {getattr(e, 'details', None)}\n"
            f"hint: {getattr(e, 'hint', None)}"
        )
        st.stop()
    except Exception as e:
        st.error(f"app_configへの保存に失敗しました: {e}")
        st.stop()


def parse_date(s):
    return date.fromisoformat(s) if s else None


def days_until(d):
    if not d:
        return None
    return (parse_date(d) - date.today()).days


def fmt_md(d):
    dt = d if isinstance(d, date) else parse_date(d)
    return f"{dt.month}/{dt.day}"


def fmt_sleep(hours):
    if hours is None:
        return "—"
    h = int(hours)
    m = round((hours - h) * 60)
    return f"{h}時間{m}分"


def fmt_num(n):
    if n is None:
        return "—"
    return f"{n:,.0f}"


FIELD_HINTS = {
    "date": "計測日(画面上部の日付表記。例: 2026年07月06日、7月6日、7/6)",
    "time": "計測時刻(例: 10:21)",
    "weight": "体重",
    "bodyFat": "体脂肪率(全身。部位別ではなく合計値)",
    "bodyWater": "体水分率",
    "calories": "消費カロリー、合計消費カロリー、アクティブカロリー",
    "sleepHours": "睡眠時間(例: 7時間20分)",
    "sleepScore": "睡眠スコア、睡眠の質のスコア(例: 85 良い、のような数値)",
    "muscleMass": "筋肉量(全身合計。部位別の腕・脚・体幹ではない)",
    "bmi": "BMI",
    "visceralFat": "内臓脂肪レベル",
    "boneMass": "推定骨量",
    "basalMetabolism": "基礎代謝量、基礎代謝",
    "bodyAge": "体内年齢",
    "muscleScore": "筋肉スコア",
    "muscleQualityScore": "筋質点数(全身合計)",
    "restingHR": "脈拍(体組成計)、安静時脈拍",
    "armMuscleR": "右腕筋肉量",
    "armMuscleL": "左腕筋肉量",
    "legMuscleR": "右脚筋肉量",
    "legMuscleL": "左脚筋肉量",
    "trunkMuscle": "体幹筋肉量",
    "armFatR": "右腕脂肪率",
    "armFatL": "左腕脂肪率",
    "legFatR": "右脚脂肪率",
    "legFatL": "左脚脂肪率",
    "trunkFat": "体幹脂肪率",
    "armQualityR": "右腕筋質点数",
    "armQualityL": "左腕筋質点数",
    "legQualityR": "右脚筋質点数",
    "legQualityL": "左脚筋質点数",
    "mbaRating": "MBA判定(例: アマチュア、エリート等の文字列)",
    "targetHRRange": "運動時目標脈拍(範囲。例: 136〜155)",
}
EXTRACT_SCHEMA_KEYS = list(FIELD_HINTS.keys())


def build_extraction_prompt():
    hint_lines = "\n".join(f'- "{k}": {v}' for k, v in FIELD_HINTS.items())
    return (
        "あなたは体組成計アプリ(Tanita等)およびヘルスケアアプリのスクリーンショットから"
        "数値データを正確に抽出する専門ツールです。\n\n"
        "この画像は、体組成計アプリまたはヘルスケアアプリの計測結果画面です。多くの場合、"
        "項目名(日本語ラベル)とその右側に数値+単位が一行ずつ並ぶ表形式で、"
        "「多い/標準/低い/+標準」のようなバッジが数値の左右に付いていることがあります。"
        "バッジの文字列は無視し、数値のみを抽出してください。\n\n"
        "特に重要視して正確に読み取ってほしい項目(誤読・見落としが許されないもの):"
        " weight(体重), bodyFat(体脂肪率), bodyWater(体水分率), muscleMass(筋肉量),"
        " basalMetabolism(基礎代謝量)、および部位別データ"
        "(armMuscleR/L, legMuscleR/L, trunkMuscle, armFatR/L, legFatR/L, trunkFat)。"
        "これらは全身の合計値と部位別の値が同じ画面に混在することが多いので、"
        "「右腕」「左腕」「右脚」「左脚」「体幹」のラベルを取り違えないよう、"
        "各数値がどの行・どのラベルに属するかを一つずつ確認してから割り当ててください。\n\n"
        "抽出対象の項目とヒント(日本語ラベルの例)は以下の通りです。1枚の画像に全項目が"
        "写っているとは限りません。画像に明確に写っている項目だけを埋めてください。\n"
        f"{hint_lines}\n\n"
        "厳守事項:\n"
        "1. 画像に明確に写っていない項目は必ずnullにする。推測・補完は絶対にしない。\n"
        "2. 数値は表示されている桁をそのまま使い、四捨五入や丸めをしない。\n"
        "3. dateはYYYY-MM-DD形式、timeはHH:MM形式に変換する。年が画像に無い場合はnullにする。\n"
        "4. sleepHoursは「7時間20分」のような表記を10進の時間(7.33)に変換する。\n"
        "5. mbaRatingとtargetHRRangeは文字列のまま抽出する(targetHRRangeは「136-155」のような形式)。\n"
        "6. 出力はJSONオブジェクトのみ。前置き、説明文、マークダウンのコードフェンス(```)は"
        "一切含めない。\n\n"
        "出力するJSONのキー一覧: " + ", ".join(EXTRACT_SCHEMA_KEYS)
    )


def extract_from_image(api_key, image_bytes, media_type):
    import anthropic

    raw_text = None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        resp = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": build_extraction_prompt()},
                ],
            }],
        )
        raw_text = "".join(block.text for block in resp.content if getattr(block, "type", None) == "text")
    except Exception as e:
        return None, raw_text, f"API呼び出しエラー: {e}"

    cleaned = raw_text.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(cleaned)
        return parsed, raw_text, None
    except json.JSONDecodeError as e:
        return None, raw_text, f"JSON解析エラー: {e}"


def build_weight_chart_drawing(entries, config, width=460, height=240):
    from reportlab.graphics.shapes import Circle, Drawing, Line, String
    from reportlab.lib import colors as rl_colors

    FONT = "HeiseiKakuGo-W5"

    data = [(parse_date(e["date"]), e["weight"]) for e in entries if e.get("weight") is not None]
    data.sort(key=lambda p: p[0])
    if len(data) < 1:
        return None

    target = config.get("targetWeight")
    weigh_in_d = parse_date(config.get("weighInDate"))
    fight_d = parse_date(config.get("fightDate"))

    all_dates = [d for d, _ in data]
    if weigh_in_d:
        all_dates.append(weigh_in_d)
    if fight_d:
        all_dates.append(fight_d)
    x_min, x_max = min(all_dates), max(all_dates)
    if x_min == x_max:
        x_max = x_min + timedelta(days=1)
    x_span_days = max((x_max - x_min).days, 1)

    y_vals = [w for _, w in data]
    if target:
        y_vals.append(target)
    y_min = math.floor(min(y_vals) - 1)
    y_max = math.ceil(max(y_vals) + 1)
    y_span = max(y_max - y_min, 1)

    margin_left, margin_bottom, margin_top, margin_right = 32, 24, 40, 12
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    def xpos(d):
        return margin_left + (d - x_min).days / x_span_days * plot_w

    def ypos(v):
        return margin_bottom + (v - y_min) / y_span * plot_h

    d = Drawing(width, height)

    d.add(Line(margin_left, margin_bottom, margin_left, height - margin_top, strokeColor=rl_colors.grey))
    d.add(Line(margin_left, margin_bottom, width - margin_right, margin_bottom, strokeColor=rl_colors.grey))

    y_tick = y_min
    while y_tick <= y_max:
        y = ypos(y_tick)
        d.add(Line(margin_left - 3, y, margin_left, y, strokeColor=rl_colors.grey))
        d.add(String(margin_left - 5, y - 2.5, f"{y_tick:.0f}", fontName=FONT, fontSize=6, textAnchor="end"))
        y_tick += 1

    n_ticks = 5
    for i in range(n_ticks + 1):
        tick_date = x_min + timedelta(days=round(i / n_ticks * x_span_days))
        x = xpos(tick_date)
        d.add(Line(x, margin_bottom, x, margin_bottom - 3, strokeColor=rl_colors.grey))
        d.add(String(x, margin_bottom - 13, f"{tick_date.month}/{tick_date.day}", fontName=FONT, fontSize=6, textAnchor="middle"))

    if target:
        y = ypos(target)
        d.add(Line(margin_left, y, width - margin_right, y, strokeColor=rl_colors.HexColor("#6B9080"), strokeWidth=1, strokeDashArray=[3, 2]))
        d.add(String(margin_left + 2, y + 2, "目標体重", fontName=FONT, fontSize=6, fillColor=rl_colors.HexColor("#6B9080")))

    if weigh_in_d:
        x = xpos(weigh_in_d)
        d.add(Line(x, margin_bottom, x, height - margin_top, strokeColor=rl_colors.HexColor("#6E93B0"), strokeWidth=1, strokeDashArray=[3, 2]))
        d.add(String(x - 2, height - margin_top + 20, f"計量日 {weigh_in_d.month}/{weigh_in_d.day}", fontName=FONT, fontSize=6,
                     fillColor=rl_colors.HexColor("#6E93B0"), textAnchor="end"))
    if fight_d:
        x = xpos(fight_d)
        d.add(Line(x, margin_bottom, x, height - margin_top, strokeColor=rl_colors.HexColor("#C1443C"), strokeWidth=1, strokeDashArray=[3, 2]))
        d.add(String(x + 2, height - margin_top + 8, f"試合日 {fight_d.month}/{fight_d.day}", fontName=FONT, fontSize=6,
                     fillColor=rl_colors.HexColor("#C1443C"), textAnchor="start"))

    for i in range(len(data) - 1):
        x1, y1 = xpos(data[i][0]), ypos(data[i][1])
        x2, y2 = xpos(data[i + 1][0]), ypos(data[i + 1][1])
        d.add(Line(x1, y1, x2, y2, strokeColor=rl_colors.HexColor("#D9A441"), strokeWidth=2))
    for dt, w in data:
        x, y = xpos(dt), ypos(w)
        d.add(Circle(x, y, 2, fillColor=rl_colors.HexColor("#D9A441"), strokeColor=None))

    return d


def generate_pdf_report(config, entries):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    title_style = ParagraphStyle("title", fontName="HeiseiKakuGo-W5", fontSize=18, spaceAfter=4)
    sub_style = ParagraphStyle("sub", fontName="HeiseiKakuGo-W5", fontSize=9, textColor=colors.grey, spaceAfter=14)
    h2_style = ParagraphStyle("h2", fontName="HeiseiKakuGo-W5", fontSize=13, spaceBefore=10, spaceAfter=6)
    body_style = ParagraphStyle("body", fontName="HeiseiKakuGo-W5", fontSize=9)

    elements = [
        Paragraph("減量・コンディション レポート", title_style),
        Paragraph(f"出力日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}", sub_style),
    ]

    weighInTs = days_until(config.get("weighInDate"))
    fightTs = days_until(config.get("fightDate"))

    elements.append(Paragraph("試合情報", h2_style))
    info_data = [
        ["計量日", f"{config.get('weighInDate') or '—'}" + (f"（残り{weighInTs}日）" if weighInTs is not None else "")],
        ["試合日", f"{config.get('fightDate') or '—'}" + (f"（残り{fightTs}日）" if fightTs is not None else "")],
        ["開始体重", f"{config.get('startWeight')} kg" if config.get("startWeight") else "—"],
        ["目標体重", f"{config.get('targetWeight')} kg" if config.get("targetWeight") else "—"],
    ]
    t = Table(info_data, colWidths=[100, 300])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "HeiseiKakuGo-W5"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
    ]))
    elements.append(t)

    latest = entries[-1] if entries else None
    elements.append(Paragraph("現在の状況", h2_style))
    if latest:
        target = config.get("targetWeight")
        delta = round(latest["weight"] - target, 2) if target else None
        status_data = [
            ["最終記録日", f"{latest['date']}" + (f" {latest['time']}" if latest.get("time") else "")],
            ["体重", f"{latest['weight']:.2f} kg" + (f"（目標差 {'+' if delta > 0 else ''}{delta}kg）" if delta is not None else "")],
            ["体脂肪率", f"{latest.get('bodyFat', '—')}%" if latest.get("bodyFat") is not None else "—"],
            ["体水分率", f"{latest.get('bodyWater', '—')}%" if latest.get("bodyWater") is not None else "—"],
            ["消費カロリー", f"{fmt_num(latest.get('calories'))} kcal"],
            ["睡眠", fmt_sleep(latest.get("sleepHours")) + (f"（スコア{latest['sleepScore']}）" if latest.get("sleepScore") else "")],
        ]
        t2 = Table(status_data, colWidths=[100, 300])
        t2.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "HeiseiKakuGo-W5"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
        ]))
        elements.append(t2)
    else:
        elements.append(Paragraph("記録がありません。", body_style))

    elements.append(Paragraph("体重推移", h2_style))
    chart = build_weight_chart_drawing(entries, config)
    if chart:
        elements.append(chart)
        elements.append(Spacer(1, 10))
    else:
        elements.append(Paragraph("グラフを表示するには体重の記録が1件以上必要です。", body_style))

    if entries:
        elements.append(Paragraph("記録履歴", h2_style))
        header = ["日付", "体重", "体脂肪率", "体水分率", "睡眠", "メモ"]
        rows = [header]
        for e in entries:
            rows.append([
                e["date"],
                f"{e['weight']:.2f}kg",
                str(e.get("bodyFat", "—")),
                str(e.get("bodyWater", "—")),
                fmt_sleep(e.get("sleepHours")),
                (e.get("notes") or "")[:30],
            ])
        t3 = Table(rows, colWidths=[55, 45, 55, 55, 60, 130])
        t3.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "HeiseiKakuGo-W5"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#333333")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ]))
        elements.append(t3)

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


st.set_page_config(page_title="減量ログ", page_icon="🥊", layout="centered")

def check_password():
    def password_entered():
        if st.session_state.get("password_input") == st.secrets.get("APP_PASSWORD"):
            st.session_state["authenticated"] = True
            del st.session_state["password_input"]
        else:
            st.session_state["authenticated"] = False

    if st.session_state.get("authenticated"):
        return True

    st.text_input("パスワード", type="password", key="password_input", on_change=password_entered)
    if st.session_state.get("authenticated") is False:
        st.error("パスワードが違います。")
    return False


if not check_password():
    st.stop()

config = load_config()
entries = load_entries()

st.markdown("###### CORNER BOARD")
st.title("減量ログ")

with st.expander("⚙️ 設定(計量日・試合日・目標体重)", expanded=not config.get("weighInDate")):
    weigh_in = st.date_input("計量日", value=parse_date(config.get("weighInDate")) or date.today())
    fight = st.date_input("試合日", value=parse_date(config.get("fightDate")) or date.today())
    start_w = st.number_input("開始体重 (kg)", value=float(config.get("startWeight") or 0.0), step=0.01, format="%.2f")
    target_w = st.number_input("目標体重 (kg)", value=float(config.get("targetWeight") or 0.0), step=0.01, format="%.2f")
    if st.button("設定を保存"):
        save_config({
            "weighInDate": weigh_in.isoformat(),
            "fightDate": fight.isoformat(),
            "startWeight": start_w or None,
            "targetWeight": target_w or None,
        })
        st.success("保存しました")
        st.rerun()
    st.caption("全データを削除するには、下の欄に「削除」と入力してください。")
    confirm_text = st.text_input("確認", key="confirm_delete_all", label_visibility="collapsed", placeholder="ここに「削除」と入力")
    if st.button("🗑️ 全データを削除", type="secondary", disabled=(confirm_text != "削除")):
        delete_all_entries()
        save_config(dict(DEFAULT_CONFIG))
        st.rerun()

target = config.get("targetWeight")
days_weighin = days_until(config.get("weighInDate"))
days_fight = days_until(config.get("fightDate"))

st.metric("計量まで", f"{days_weighin}日" if days_weighin is not None else "—")
st.metric("試合まで", f"{days_fight}日" if days_fight is not None else "—")

latest = entries[-1] if entries else None
if latest:
    delta = round(latest["weight"] - target, 2) if target else None
    st.metric("現在の体重", f"{latest['weight']:.2f} kg")
    if delta is not None:
        st.metric("目標差", f"{'+' if delta > 0 else ''}{delta} kg")
    st.metric("体脂肪率", f"{latest.get('bodyFat')}%" if latest.get("bodyFat") is not None else "—")
    st.metric("体水分率", f"{latest.get('bodyWater')}%" if latest.get("bodyWater") is not None else "—")
    st.metric("消費kcal", fmt_num(latest.get("calories")))
    st.metric("睡眠", fmt_sleep(latest.get("sleepHours")))

st.subheader("項目別 推移")
metric_options = {f"{m[1]}{f'（{m[2]}）' if m[2] else ''}": m[0] for m in METRICS}
metric_label = st.selectbox("表示する項目", list(metric_options.keys()))
chart_metric = metric_options[metric_label]
metric_unit = METRIC_BY_KEY[chart_metric][2]

metric_entries = [e for e in entries if e.get(chart_metric) is not None]

if len(metric_entries) >= 2:
    dates = [parse_date(e["date"]) for e in metric_entries]
    values = [e[chart_metric] for e in metric_entries]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=dates, y=values, mode="lines+markers", line=dict(color="#D9A441", width=2), marker=dict(size=7), name=metric_label))

    weigh_in_d = parse_date(config.get("weighInDate"))
    fight_d = parse_date(config.get("fightDate"))

    if weigh_in_d:
        earliest = min(dates) if dates else weigh_in_d
        cutoff = earliest - timedelta(days=7)
        n = 1
        while n <= 12:
            marker_d = weigh_in_d - timedelta(weeks=n)
            if marker_d < cutoff:
                break
            fig.add_vline(x=marker_d.isoformat(), line_dash="dot", line_color="lightgray", line_width=1,
                          annotation_text=f"-{n}w", annotation_font_size=9, annotation_font_color="gray")
            n += 1

    if weigh_in_d:
        fig.add_vline(x=weigh_in_d.isoformat(), line_dash="dash", line_color="#6E93B0",
                      annotation_text=f"計量日 {fmt_md(weigh_in_d)}", annotation_font_color="#6E93B0",
                      annotation_position="top", annotation_y=1.12, annotation_yref="paper")
    if fight_d:
        fig.add_vline(x=fight_d.isoformat(), line_dash="dash", line_color="#C1443C",
                      annotation_text=f"試合日 {fmt_md(fight_d)}", annotation_font_color="#C1443C",
                      annotation_position="top", annotation_y=1.02, annotation_yref="paper")
    if chart_metric == "weight" and target:
        fig.add_hline(y=target, line_dash="dash", line_color="#6B9080", annotation_text="目標体重")
        if weigh_in_d:
            fig.add_trace(go.Scatter(x=[weigh_in_d], y=[target], mode="markers",
                                      marker=dict(size=12, color="#6B9080", line=dict(width=2, color="white")),
                                      name="目標(計量日)"))

    yaxis_kwargs = {}
    if chart_metric == "weight":
        yaxis_kwargs["dtick"] = 1
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=55, b=10), showlegend=False,
                       yaxis_title=metric_unit, xaxis_title=None)
    fig.update_yaxes(**yaxis_kwargs)
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info(f"このグラフを表示するにはあと{2 - len(metric_entries)}件の記録が必要です。")

st.subheader("レポート出力")
if st.button("📄 PDFレポートを生成"):
    pdf_bytes = generate_pdf_report(config, entries)
    st.download_button("レポートをダウンロード (PDF)", data=pdf_bytes,
                        file_name=f"減量レポート_{date.today().isoformat()}.pdf", mime="application/pdf")

st.subheader("画像から自動入力")
api_key = st.text_input("Anthropic APIキー", value=os.environ.get("ANTHROPIC_API_KEY", ""), type="password",
                         help="ヘルスケアアプリ・体組成計アプリのスクリーンショットから自動で数値を読み取るために使います。")
uploaded_images = st.file_uploader("スクリーンショットを選択(複数可)", type=["png", "jpg", "jpeg"], accept_multiple_files=True)

if uploaded_images and st.button("画像を解析"):
    if not api_key:
        st.error("APIキーを入力してください。")
    else:
        per_image = []
        merged = {}
        source_map = {}
        conflicts = []

        for idx, img in enumerate(uploaded_images, start=1):
            media_type = img.type or "image/jpeg"
            img_bytes = img.read()
            with st.spinner(f"{img.name} を解析中... ({idx}/{len(uploaded_images)})"):
                parsed, raw_text, error = extract_from_image(api_key, img_bytes, media_type)
            non_null = {k: v for k, v in (parsed or {}).items() if v is not None} if parsed else {}
            per_image.append({"name": img.name, "error": error, "raw_text": raw_text, "non_null": non_null})
            for k, v in non_null.items():
                if k not in merged:
                    merged[k] = v
                    source_map[k] = img.name
                elif merged[k] != v:
                    conflicts.append({
                        "項目": label_for(k)[0], "採用した値": merged[k], "採用元画像": source_map[k],
                        "他の値": v, "他の画像": img.name,
                    })

        st.session_state.review = {"per_image": per_image, "merged": merged, "source_map": source_map, "conflicts": conflicts}
        st.session_state.review_active = True
        st.rerun()

if st.session_state.get("review_active"):
    review = st.session_state.review
    st.markdown("### 📋 読み取り結果の確認")
    st.caption("誤読の可能性があるため、保存前に必ず内容を確認してください。")

    for idx, item in enumerate(review["per_image"], start=1):
        with st.expander(f"画像{idx}: {item['name']}", expanded=True):
            if item["error"]:
                st.error(f"抽出エラー: {item['error']}")
                st.caption("Claudeの生レスポンス:")
                st.code(item["raw_text"] if item["raw_text"] else "(レスポンスなし)")
            elif not item["non_null"]:
                st.warning("この画像からは項目を読み取れませんでした。")
                st.caption("Claudeの生レスポンス:")
                st.code(item["raw_text"] or "(レスポンスなし)")
            else:
                st.success(f"{len(item['non_null'])}項目を読み取りました。")
                rows = [{"項目": label_for(k)[0], "値": v, "単位": label_for(k)[1]} for k, v in item["non_null"].items()]
                st.table(pd.DataFrame(rows))

    if review["conflicts"]:
        st.warning("同じ項目が複数画像で異なる値として検出されました。下の表で採用する値を確認・修正してください。")
        st.table(pd.DataFrame(review["conflicts"]))

    st.markdown("#### 採用する値(表の「値」列は修正できます)")

    review_date = st.date_input("日付", value=parse_date(review["merged"].get("date")) or date.today(), key="review_date")
    review_time = st.text_input("計測時刻 (任意, HH:MM)", value=review["merged"].get("time") or "", key="review_time")

    table_rows = []
    for k in ALL_NUMERIC_KEYS + ["mbaRating", "targetHRRange"]:
        label, unit = label_for(k)
        table_rows.append({
            "キー": k,
            "項目": label,
            "単位": unit,
            "値": review["merged"].get(k, None),
            "採用元画像": review["source_map"].get(k, ""),
        })
    review_df = pd.DataFrame(table_rows)

    edited_df = st.data_editor(
        review_df,
        column_config={
            "キー": None,
            "項目": st.column_config.TextColumn("項目", disabled=True),
            "単位": st.column_config.TextColumn("単位", disabled=True),
            "値": st.column_config.TextColumn("値(修正可)"),
            "採用元画像": st.column_config.TextColumn("採用元画像", disabled=True),
        },
        hide_index=True,
        use_container_width=True,
        key="review_editor",
    )

    review_skin = st.select_slider("肌の状態", options=list(range(5)), format_func=lambda i: SKIN_LABELS[i], value=2, key="review_skin")
    review_cond = st.select_slider("コンディション", options=list(range(5)), format_func=lambda i: COND_LABELS[i], value=2, key="review_cond")
    review_water = st.number_input("水分摂取 (ml・任意)", value=0.0, step=50.0, format="%.0f", key="review_water")
    review_notes = st.text_area("メモ (任意)", value="", key="review_notes")

    if st.button("✅ この内容で記録する", type="primary"):
        entry = {
            "date": review_date.isoformat(),
            "time": review_time or None,
            "water": review_water or None,
            "skin": review_skin,
            "cond": review_cond,
            "notes": review_notes.strip(),
        }
        for _, row in edited_df.iterrows():
            k = row["キー"]
            v = row["値"]
            if v is None or v == "" or (isinstance(v, float) and pd.isna(v)):
                entry[k] = None
                continue
            if k in ("mbaRating", "targetHRRange"):
                entry[k] = str(v)
            else:
                try:
                    entry[k] = float(v)
                except (ValueError, TypeError):
                    entry[k] = None

        if not entry.get("weight"):
            st.error("体重が読み取れていません。値を入力してから記録してください。")
        else:
            upsert_entry(entry)
            st.session_state.review_active = False
            del st.session_state["review"]
            st.success("記録しました")
            st.rerun()
    if st.button("キャンセル"):
        st.session_state.review_active = False
        del st.session_state["review"]
        st.rerun()

st.subheader("今日の記録(手動入力)")

f_date = st.date_input("日付", value=date.today(), key="f_date")
f_time = st.text_input("計測時刻 (任意, HH:MM)", value="")
f_weight = st.number_input("体重 (kg)", value=0.0, step=0.01, format="%.2f")
f_bodyfat = st.number_input("体脂肪率 (%・任意)", value=0.0, step=0.1, format="%.1f")
f_bodywater = st.number_input("体水分率 (%・任意)", value=0.0, step=0.1, format="%.1f")
f_calories = st.number_input("消費カロリー (kcal・任意)", value=0.0, step=1.0, format="%.0f")
f_sleep_h = st.number_input("睡眠時間 (時間・任意)", value=0.0, step=0.1, format="%.1f")
f_sleep_score = st.number_input("睡眠スコア (任意)", value=0.0, step=1.0, format="%.0f")
f_water = st.number_input("水分摂取 (ml・任意)", value=0.0, step=50.0, format="%.0f")
f_skin = st.select_slider("肌の状態", options=list(range(5)), format_func=lambda i: SKIN_LABELS[i], value=2)
f_cond = st.select_slider("コンディション", options=list(range(5)), format_func=lambda i: COND_LABELS[i], value=2)

advanced_values = {}
with st.expander("体組成計の詳細項目"):
    for group in ADVANCED_GROUPS:
        st.markdown(f"**{group}**")
        for key, label, unit, _ in [m for m in ADVANCED_METRICS if m[3] == group]:
            advanced_values[key] = st.number_input(f"{label}{f' ({unit})' if unit else ''}", value=0.0, step=0.1, format="%.1f", key=f"adv_{key}")
    f_mba = st.text_input("MBA判定 (任意)", value="")
    f_hr_range = st.text_input("運動時目標脈拍 (任意)", value="")

f_notes = st.text_area("メモ (任意)", value="")

if st.button("記録する", type="primary", disabled=(f_weight <= 0)):
    entry = {
        "date": f_date.isoformat(),
        "time": f_time or None,
        "weight": f_weight,
        "bodyFat": f_bodyfat or None,
        "bodyWater": f_bodywater or None,
        "calories": f_calories or None,
        "sleepHours": f_sleep_h or None,
        "sleepScore": f_sleep_score or None,
        "water": f_water or None,
        "skin": f_skin,
        "cond": f_cond,
        "notes": f_notes.strip(),
        "mbaRating": f_mba or None,
        "targetHRRange": f_hr_range or None,
    }
    for k in ALL_NUMERIC_KEYS:
        if k in BASIC_KEYS:
            continue
        entry[k] = advanced_values.get(k) or None
    upsert_entry(entry)
    st.success("記録しました")
    st.rerun()

if entries:
    st.subheader("履歴")
    for e in reversed(entries):
        with st.container(border=True):
            st.markdown(f"**{e['date']}{(' ' + e['time']) if e.get('time') else ''} — {e['weight']:.2f}kg**")
            sub = f"肌:{SKIN_LABELS[e['skin']]} / コンディション:{COND_LABELS[e['cond']]}"
            if e.get("water"):
                sub += f" / 水分:{e['water']}ml"
            st.caption(sub)
            extra_bits = []
            if e.get("bodyFat") is not None:
                extra_bits.append(f"体脂肪:{e['bodyFat']}%")
            if e.get("bodyWater") is not None:
                extra_bits.append(f"体水分:{e['bodyWater']}%")
            if e.get("calories") is not None:
                extra_bits.append(f"消費:{fmt_num(e['calories'])}kcal")
            if e.get("sleepHours") is not None:
                extra_bits.append(f"睡眠:{fmt_sleep(e['sleepHours'])}")
            if extra_bits:
                st.caption(" / ".join(extra_bits))
            if e.get("notes"):
                st.caption(f"_{e['notes']}_")
            if st.button("削除", key=f"del_{e['date']}"):
                delete_entry(e["date"])
                st.rerun()
