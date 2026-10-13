"""Japan: the Ministry of Health's NHI drug price list (薬価基準収載品目リスト).

MHLW publishes every medicine reimbursed under Japan's national health
insurance -- in practice every prescription medicine on the market -- as Excel
files, updated with each listing round:

    https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/0000078916.html
        -> the current list page -> 内用薬 (oral), 注射薬 (injection),
           外用薬 (external) workbooks

Each row gives the ingredient, strength, product name, the marketing company
(製造販売業者), originator or generic, and the price. PMDA's own search is
the register of approvals, but it turns away scripted clients; this list is
the Ministry's own and is published for download.

Everything in it is Japanese. Three things are put into English when the
index is built, and nothing else is:

* the ingredient, through KEGG's list of Japanese drug names with their
  English equivalents (a dictionary, not a data source; the row's values stay
  MHLW's);
* the dosage form and strength, from the few words Japan uses for them;
* the company, from a table of the companies' own English names. A company
  not in it keeps its Japanese name rather than a guessed one.

Brand names are written in Hepburn romanisation and say so.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urljoin

import requests

from core.logging_config import get_logger
from sources.open_registers import OpenRegister, _match_text, add_register, search_register


logger = get_logger(__name__)

INDEX_PAGE = "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/0000078916.html"
KEGG_NAMES = "https://rest.kegg.jp/list/dr_ja"
USER_AGENT = "Mozilla/5.0"
REQUEST_TIMEOUT = (30, 180)
# 01 oral, 02 injection, 03 external. 04 (dental) is left out.
WORKBOOKS = {"01": "内用薬", "02": "注射薬", "03": "外用薬"}
ROUTES = {"01": "Oral", "02": "Parenteral", "03": "External use"}


def _nfkc(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


# ---------------------------------------------------------------- romanisation

_KANA = {
    "ア": "a", "イ": "i", "ウ": "u", "エ": "e", "オ": "o",
    "カ": "ka", "キ": "ki", "ク": "ku", "ケ": "ke", "コ": "ko",
    "サ": "sa", "シ": "shi", "ス": "su", "セ": "se", "ソ": "so",
    "タ": "ta", "チ": "chi", "ツ": "tsu", "テ": "te", "ト": "to",
    "ナ": "na", "ニ": "ni", "ヌ": "nu", "ネ": "ne", "ノ": "no",
    "ハ": "ha", "ヒ": "hi", "フ": "fu", "ヘ": "he", "ホ": "ho",
    "マ": "ma", "ミ": "mi", "ム": "mu", "メ": "me", "モ": "mo",
    "ヤ": "ya", "ユ": "yu", "ヨ": "yo",
    "ラ": "ra", "リ": "ri", "ル": "ru", "レ": "re", "ロ": "ro",
    "ワ": "wa", "ヲ": "o", "ン": "n",
    "ガ": "ga", "ギ": "gi", "グ": "gu", "ゲ": "ge", "ゴ": "go",
    "ザ": "za", "ジ": "ji", "ズ": "zu", "ゼ": "ze", "ゾ": "zo",
    "ダ": "da", "ヂ": "ji", "ヅ": "zu", "デ": "de", "ド": "do",
    "バ": "ba", "ビ": "bi", "ブ": "bu", "ベ": "be", "ボ": "bo",
    "パ": "pa", "ピ": "pi", "プ": "pu", "ペ": "pe", "ポ": "po",
    "ヴ": "vu",
    "ァ": "a", "ィ": "i", "ゥ": "u", "ェ": "e", "ォ": "o",
}
_YOON = {"ャ": "ya", "ュ": "yu", "ョ": "yo"}


def romanise(text: str) -> str:
    """Katakana (and hiragana) in Hepburn; other characters are kept."""
    chars = []
    for char in _nfkc(text):
        code = ord(char)
        chars.append(chr(code + 0x60) if 0x3041 <= code <= 0x3096 else char)
    out: list[str] = []
    index = 0
    while index < len(chars):
        char = chars[index]
        nxt = chars[index + 1] if index + 1 < len(chars) else ""
        if char == "ッ" and nxt in _KANA:
            out.append(_KANA[nxt][0])
        elif char == "ー":
            pass
        elif char in _YOON and out:
            syllable = out.pop()
            stem = syllable[:-1] if syllable.endswith("i") else syllable
            out.append(("sh" if stem == "sh" else "ch" if stem == "ch" else "j" if stem == "j" else stem)
                       + (_YOON[char][1:] if stem in ("sh", "ch", "j") else _YOON[char]))
        elif char in "ァィゥェォ" and out:
            syllable = out.pop()
            out.append(syllable[:-1] + _KANA[char])
        elif char in _KANA:
            out.append(_KANA[char])
        elif char in "・･":
            out.append(" ")
        else:
            out.append(char)
        index += 1
    text = "".join(out)
    return " ".join(word[:1].upper() + word[1:] for word in text.split())


def _has_japanese(text: str) -> bool:
    return bool(re.search(r"[぀-ヿ㐀-鿿]", text))


# ---------------------------------------------------------------- companies

# The companies' own English names. Only names the company itself uses.
COMPANIES = {
    "東和薬品": "Towa Pharmaceutical", "沢井製薬": "Sawai Pharmaceutical", "ニプロ": "Nipro",
    "日医工": "Nichi-Iko Pharmaceutical", "ファイザー": "Pfizer Japan", "陽進堂ホールディングス": "Yoshindo Holdings",
    "高田製薬": "Takata Pharmaceutical", "ツムラ": "Tsumura", "日新製薬(山形)": "Nissin Pharmaceutical (Yamagata)",
    "Meiji Seika ファルマ": "Meiji Seika Pharma", "日医工岐阜工場": "Nichi-Iko Gifu Plant",
    "共和薬品工業": "Kyowa Pharmaceutical Industry", "田辺ファーマ": "Tanabe Pharma",
    "日本ジェネリック": "Nihon Generic", "ヴィアトリス・ヘルスケア": "Viatris Healthcare",
    "富士製薬工業": "Fuji Pharma", "ノバルティス ファーマ": "Novartis Pharma", "テルモ": "Terumo",
    "大塚製薬工場": "Otsuka Pharmaceutical Factory", "第一三共": "Daiichi Sankyo",
    "扶桑薬品工業": "Fuso Pharmaceutical Industries", "アルフレッサファーマ": "Alfresa Pharma",
    "サノフィ": "Sanofi", "第一三共エスファ": "Daiichi Sankyo Espha", "武田薬品工業": "Takeda Pharmaceutical",
    "バイエル薬品": "Bayer Yakuhin", "住友ファーマ": "Sumitomo Pharma", "グラクソ・スミスクライン": "GlaxoSmithKline",
    "エーザイ": "Eisai", "鶴原製薬": "Tsuruhara Pharmaceutical", "辰巳化学": "Tatsumi Kagaku",
    "ヴィアトリス製薬": "Viatris Pharmaceuticals", "ヤンセンファーマ": "Janssen Pharmaceutical",
    "小太郎漢方製薬": "Kotaro Pharmaceutical", "日本ケミファ": "Nippon Chemiphar",
    "キョーリンリメディオ": "Kyorin Rimedio", "中外製薬": "Chugai Pharmaceutical", "協和キリン": "Kyowa Kirin",
    "丸石製薬": "Maruishi Pharmaceutical", "サンド": "Sandoz", "大塚製薬": "Otsuka Pharmaceutical",
    "日本化薬": "Nippon Kayaku", "久光製薬": "Hisamitsu Pharmaceutical", "日本イーライリリー": "Eli Lilly Japan",
    "ヴァンティブ": "Vantive", "T’s製薬": "T's Pharma", "あすか製薬": "ASKA Pharmaceutical",
    "健栄製薬": "Kenei Pharmaceutical", "大原薬品工業": "Ohara Pharmaceutical",
    "シオノギファーマ": "Shionogi Pharma", "ノボ ノルディスク ファーマ": "Novo Nordisk Pharma",
    "帝國製薬": "Teikoku Seiyaku", "サンドファーマ": "Sandoz Pharma", "持田製薬": "Mochida Pharmaceutical",
    "ダイト": "Daito Pharmaceutical", "長生堂製薬": "Choseido Pharmaceutical",
    "三和化学研究所": "Sanwa Kagaku Kenkyusho", "アストラゼネカ": "AstraZeneca", "興和": "Kowa",
    "太陽ファルマ": "Taiyo Pharma", "日本新薬": "Nippon Shinyaku", "LTLファーマ": "LTL Pharma",
    "日興製薬": "Nikko Pharmaceutical", "参天製薬": "Santen Pharmaceutical", "岩城製薬": "Iwaki Seiyaku",
    "日本赤十字社": "Japanese Red Cross Society", "共創未来ファーマ": "Kyoso Mirai Pharma",
    "帝人ファーマ": "Teijin Pharma", "アステラス製薬": "Astellas Pharma", "サンファーマ": "Sun Pharma Japan",
    "吉田製薬": "Yoshida Pharmaceutical", "光製薬": "Hikari Pharmaceutical", "小野薬品工業": "Ono Pharmaceutical",
    "MSD": "MSD", "日本ベーリンガーインゲルハイム": "Nippon Boehringer Ingelheim", "オルガノン": "Organon",
    "大正製薬": "Taisho Pharmaceutical", "鳥居薬品": "Torii Pharmaceutical", "クラシエ": "Kracie",
    "日本薬品工業": "Nihon Pharmaceutical Industry", "塩野義製薬": "Shionogi", "キッセイ薬品工業": "Kissei Pharmaceutical",
    "旭化成セラピューティクス": "Asahi Kasei Therapeutics", "大鵬薬品工業": "Taiho Pharmaceutical",
    "トーアエイヨー": "Toa Eiyo", "CSLベーリング": "CSL Behring", "わかもと製薬": "Wakamoto Pharmaceutical",
    "千寿製薬": "Senju Pharmaceutical", "チェプラファーム": "Cheplapharm", "マルホ": "Maruho",
    "シオノケミカル": "Shiono Chemical", "ブリストル・マイヤーズ スクイブ": "Bristol Myers Squibb",
    "T’sファーマ": "T's Pharma", "ジェイ・エム・エス": "JMS", "アッヴィ": "AbbVie",
    "ジェーピーエス製薬": "JPS Pharmaceutical", "PDRファーマ": "PDRadiopharma",
    "GEヘルスケアファーマ": "GE HealthCare Pharma", "全星薬品工業": "Zensei Pharmaceutical",
    "杏林製薬": "Kyorin Pharmaceutical", "あゆみ製薬": "Ayumi Pharmaceutical", "科研製薬": "Kaken Pharmaceutical",
    "ユーシービージャパン": "UCB Japan", "EAファーマ": "EA Pharma", "ノーベルファーマ": "Nobelpharma",
    "日本メジフィジックス": "Nihon Medi-Physics", "佐藤製薬": "Sato Pharmaceutical",
    "富士フイルム富山化学": "FUJIFILM Toyama Chemical", "テイカ製薬": "Teika Pharmaceutical",
    "フェリング・ファーマ": "Ferring Pharmaceuticals", "ブラッコ・ジャパン": "Bracco Japan",
    "KMバイオロジクス": "KM Biologics", "寿製薬": "Kotobuki Pharmaceutical", "JCRファーマ": "JCR Pharmaceuticals",
    "ゼリア新薬工業": "Zeria Pharmaceutical", "カイゲンファーマ": "Kaigen Pharma", "ゲルベ・ジャパン": "Guerbet Japan",
    "日本臓器製薬": "Nippon Zoki Pharmaceutical", "ギリアド・サイエンシズ": "Gilead Sciences",
    "アレクシオンファーマ": "Alexion Pharma", "クリニジェン": "Clinigen", "ヴィーブヘルスケア": "ViiV Healthcare",
    "日本血液製剤機構": "Japan Blood Products Organization", "Me ファルマ": "Me Pharma",
    "セルトリオン・ヘルスケア・ジャパン": "Celltrion Healthcare Japan", "日医工ファーマ": "Nichi-Iko Pharma",
    "アムジェン": "Amgen", "メルクバイオファーマ": "Merck Biopharma", "持田製薬販売": "Mochida Pharmaceutical Sales",
    "生化学工業": "Seikagaku Corporation", "バイオジェン・ジャパン": "Biogen Japan", "ムンディファーマ": "Mundipharma",
    "BioMarin Pharmaceutical Japan": "BioMarin Pharmaceutical Japan", "協和キリンフロンティア": "Kyowa Kirin Frontier",
    "レオファーマ": "LEO Pharma", "日本メダック": "medac Japan", "住友ファーマプロモ": "Sumitomo Pharma Promo",
    "ミノファーゲン製薬": "Minophagen Pharmaceutical", "バイエルライフサイエンス": "Bayer Life Science",
    "IPSEN": "Ipsen", "ロートニッテンファーマ": "Rohto-Nitten Pharma", "ロートニッテン": "Rohto-Nitten",
    "日本アルコン": "Alcon Japan", "ニプロファーマ": "Nipro Pharma", "ビオフェルミン製薬": "Biofermin Pharmaceutical",
    "マグミット製薬": "Magmitt Pharmaceutical", "全薬工業": "Zenyaku Kogyo", "バクスター・ジャパン": "Baxter Japan",
    "iNova Pharmaceuticals Japan": "iNova Pharmaceuticals Japan", "イーエヌ大塚製薬": "EN Otsuka Pharmaceutical",
    "オリオンファーマ・ジャパン": "Orion Pharma Japan", "ネクセラファーマジャパン": "Nxera Pharma Japan",
    "京都薬品工業": "Kyoto Pharmaceutical Industries", "興和AGファーマ": "Kowa AG Pharma",
    "ミヤリサン製薬": "Miyarisan Pharmaceutical", "ブリストル・マイヤーズスクイブ販売": "Bristol Myers Squibb Sales",
    "日本セルヴィエ": "Servier Japan", "フレゼニウスカービジャパン": "Fresenius Kabi Japan",
    "日本マイクロバイオファーマ": "Japan Microbiopharma", "エイエムオー・ジャパン": "AMO Japan",
    "参天アイケア": "Santen Eyecare", "東レ": "Toray Industries", "アボットジャパン": "Abbott Japan",
    "Swedish Orphan Biovitrum Japan": "Swedish Orphan Biovitrum Japan", "アミカス・セラピューティクス": "Amicus Therapeutics",
    "Ultragenyx Japan": "Ultragenyx Japan", "インサイト・バイオサイエンシズ・ジャパン": "Incyte Biosciences Japan",
    "SBIファーマ": "SBI Pharmaceuticals", "日機装": "Nikkiso", "協和キリン富士フイルムバイオロジクス": "Kyowa Kirin Fujifilm Biologics",
    "シンバイオ製薬": "SymBio Pharmaceuticals", "ジェンマブ": "Genmab", "アルジェニクスジャパン": "argenx Japan",
    "スミス・アンド・ネフュー": "Smith & Nephew", "Freyr Life Sciences": "Freyr Life Sciences",
    "エムジーファーマ": "MG Pharma", "雪印メグミルク": "Megmilk Snow Brand", "クレハ": "Kureha",
    "アンジェス": "AnGes", "PTCセラピューティクス": "PTC Therapeutics", "ビーワン・メディシンズ": "BeOne Medicines",
    "Alnylam Japan": "Alnylam Japan", "ファーマエッセンシアジャパン": "PharmaEssentia Japan",
    "日本ビーシージー製造": "Japan BCG Laboratory", "森下仁丹": "Morishita Jintan", "ヤクルト本社": "Yakult Honsha",
    "KalVista Pharmaceuticals Japan": "KalVista Pharmaceuticals Japan", "キエジ・ファーマ・ジャパン": "Chiesi Pharma Japan",
    "ステラファーマ": "Stella Pharma", "ソレイジア・ファーマ": "Solasia Pharma", "リジェネロン・ジャパン": "Regeneron Japan",
    "楽天メディカル": "Rakuten Medical", "サンバイオ": "SanBio", "オンコリスバイオファーマ": "Oncolys BioPharma",
    "日本エア・リキード": "Air Liquide Japan", "日産化学": "Nissan Chemical", "日東電工": "Nitto Denko",
    "サラヤ": "Saraya", "インスメッド": "Insmed", "科研ファルマ": "Kaken Pharma",
    "レコルダティ・レア・ディジーズ・ジャパン": "Recordati Rare Diseases Japan", "オーファンパシフィック": "Orphan Pacific",
    "コーアイセイ": "Kohjin Isei", "藤本製薬": "Fujimoto Pharmaceutical", "三笠製薬": "Mikasa Seiyaku",
    "祐徳薬品工業": "Yutoku Pharmaceutical", "大興製薬": "Taiko Pharmaceutical", "岡山大鵬薬品": "Okayama Taiho Pharmaceutical",
    "救急薬品工業": "Kyukyu Pharmaceutical", "堀井薬品工業": "Horii Pharmaceutical", "東菱薬品工業": "Toho Pharmaceutical",
    "ビタカイン製薬": "Vitacain Pharmaceutical", "天藤製薬": "Amato Pharmaceutical", "大蔵製薬": "Okura Pharmaceutical",
}


def english_company(name: str) -> str:
    text = _nfkc(name)
    return COMPANIES.get(text, text)


# ---------------------------------------------------------------- forms, strengths

# Longest first: 口腔内崩壊錠 before 錠.
FORM_WORDS = [
    ("配合OD錠", "Orally disintegrating combination tablet"), ("OD錠", "Orally disintegrating tablet"),
    ("口腔内崩壊錠", "Orally disintegrating tablet"), ("配合錠", "Combination tablet"),
    ("徐放錠", "Prolonged-release tablet"), ("腸溶錠", "Gastro-resistant tablet"), ("チュアブル錠", "Chewable tablet"),
    ("フィルムコーティング錠", "Film-coated tablet"), ("錠", "Tablet"),
    ("徐放カプセル", "Prolonged-release capsule"), ("腸溶カプセル", "Gastro-resistant capsule"), ("カプセル", "Capsule"),
    ("ドライシロップ", "Dry syrup"), ("シロップ", "Syrup"), ("内用液", "Oral solution"), ("内服液", "Oral solution"),
    ("細粒", "Fine granules"), ("顆粒", "Granules"), ("散", "Powder"), ("懸濁用", "For suspension"),
    ("点眼液", "Eye drops"), ("点鼻液", "Nasal drops"), ("吸入", "Inhalation"), ("軟膏", "Ointment"),
    ("クリーム", "Cream"), ("ローション", "Lotion"), ("ゲル", "Gel"), ("テープ", "Tape (patch)"),
    ("パップ", "Poultice (patch)"), ("貼付剤", "Patch"), ("坐剤", "Suppository"), ("注射用", "For injection"),
    ("キット", "Injection kit"), ("注", "Injection"), ("エキス", "Extract"),
]
UNITS = {"錠": "tablet", "カプセル": "capsule", "包": "sachet", "瓶": "vial", "管": "ampoule", "袋": "bag",
         "キット": "kit", "筒": "syringe", "枚": "sheet", "個": "piece", "本": "unit",
         "セット": "set", "シート": "blister sheet", "カセット": "cassette"}


def dosage_form(product: str, workbook: str) -> str:
    text = _nfkc(product).replace("「", " ").split(" ")[0]
    for japanese, english in FORM_WORDS:
        if japanese in text:
            return english
    return {"02": "Injection"}.get(workbook, "")


def strength_and_unit(spec: str) -> tuple[str, str]:
    """ "１０ｍｇ１錠" -> ("10 mg", "tablet"); "１錠" -> ("", "tablet")."""
    text = _nfkc(spec).replace("μ", "µ")
    unit = ""
    match = re.search(r"1\s*(" + "|".join(map(re.escape, UNITS)) + r")$", text)
    if match:
        unit = UNITS[match.group(1)]
        text = text[: match.start()]
    text = re.sub(r"(\d)(?=[A-Za-zµ%])", r"\1 ", text.replace("単位", " units")).strip()
    # A strength still worded in Japanese ("1 mL(懸濁後の内用液として)") is left
    # out rather than shown half-translated.
    return ("" if _has_japanese(text) else text), unit


# ---------------------------------------------------------------- ingredients

SALTS_JA = [
    ("臭化水素酸塩", "hydrobromide"), ("塩酸塩", "hydrochloride"), ("硫酸塩", "sulfate"), ("リン酸塩", "phosphate"),
    ("酢酸塩", "acetate"), ("マレイン酸塩", "maleate"), ("メシル酸塩", "mesilate"), ("ベシル酸塩", "besilate"),
    ("酒石酸塩", "tartrate"), ("フマル酸塩", "fumarate"), ("クエン酸塩", "citrate"), ("コハク酸塩", "succinate"),
    ("炭酸塩", "carbonate"), ("トシル酸塩", "tosilate"), ("水和物", "hydrate"), ("無水物", "anhydrous"),
    ("ナトリウム", "sodium"), ("カリウム", "potassium"), ("カルシウム", "calcium"), ("マグネシウム", "magnesium"),
]


def _name_key(text: str) -> str:
    return re.sub(r"[\s・･]", "", _nfkc(text))


def parse_kegg_names(lines: Iterable[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for line in lines:
        if "\t" not in line:
            continue
        parts = [re.sub(r"\s*\([^()]*\)\s*$", "", part.strip()) for part in line.rstrip("\n").split("\t", 1)[1].split("; ")]
        english = [part for part in parts if part and re.fullmatch(r"[\x20-\x7E]+", part)]
        if not english:
            continue
        for part in parts:
            if part and _has_japanese(part):
                names.setdefault(_name_key(part), english[0])
    return names


def english_ingredient(japanese: str, names: dict[str, str]) -> str:
    """One Japanese ingredient in English, or "" when the dictionary lacks it."""
    key = _name_key(re.sub(r"\[[^\]]*\]", "", _nfkc(japanese)))
    if key in names:
        return names[key]
    plain = key.replace("(遺伝子組換え)", "")
    if plain in names:
        return names[plain]
    salts = []
    while True:
        for suffix, english in SALTS_JA:
            if plain.endswith(suffix) and len(plain) > len(suffix):
                plain = plain[: -len(suffix)]
                salts.insert(0, english)
                break
        else:
            break
        if plain in names:
            return " ".join([names[plain], *salts]).strip()
    return ""


def english_ingredients(japanese: str, names: dict[str, str]) -> str:
    """ "エゼチミブ・アトルバスタチンカルシウム水和物" -> "Ezetimibe; Atorvastatin calcium hydrate"."""
    whole = english_ingredient(japanese, names)
    if whole and "・" not in _nfkc(japanese):
        return whole
    parts = [part for part in re.split(r"[・･]", _nfkc(japanese)) if part]
    translated = [english_ingredient(part, names) for part in parts]
    if parts and all(translated):
        return "; ".join(translated)
    return whole


# ---------------------------------------------------------------- the register

def fetch_list(register: OpenRegister, workdir: Path) -> Path:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    index = session.get(INDEX_PAGE, timeout=REQUEST_TIMEOUT)
    index.raise_for_status()
    pages = re.findall(r'href="([^"]*/topics/\d{4}/\d{2}/tp\d{8}-01\.html)"', index.text)
    if not pages:
        raise RuntimeError("MHLW index page no longer links to the NHI price list")
    page_url = urljoin(INDEX_PAGE, pages[0])
    page = session.get(page_url, timeout=REQUEST_TIMEOUT)
    page.raise_for_status()
    links = re.findall(r'href="([^"]+_0(\d)\.xlsx)"', page.content.decode("shift_jis", "replace"))
    fetched = {}
    for href, number in links:
        key = f"0{number}"
        # The page lists the newest workbook of each kind first.
        if key in WORKBOOKS and key not in fetched:
            response = session.get(urljoin(page_url, href), timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            target = workdir / f"nhi_{key}.xlsx"
            target.write_bytes(response.content)
            fetched[key] = urljoin(page_url, href)
    if set(fetched) != set(WORKBOOKS):
        raise RuntimeError(f"MHLW list page is missing workbooks: {sorted(set(WORKBOOKS) - set(fetched))}")
    names = session.get(KEGG_NAMES, timeout=REQUEST_TIMEOUT)
    names.raise_for_status()
    (workdir / "kegg_dr_ja.txt").write_text(names.text, encoding="utf-8")
    (workdir / "sources.json").write_text(json.dumps({"page": page_url, "files": fetched}), encoding="utf-8")
    return workdir


def read_list(register: OpenRegister, workdir: Path) -> Iterator[dict[str, Any]]:
    import openpyxl

    names = parse_kegg_names((workdir / "kegg_dr_ja.txt").read_text(encoding="utf-8").splitlines())
    sources = json.loads((workdir / "sources.json").read_text(encoding="utf-8"))
    for key in WORKBOOKS:
        workbook = openpyxl.load_workbook(workdir / f"nhi_{key}.xlsx", read_only=True)
        try:
            sheet = workbook.worksheets[0]
            header: list[str] = []
            for values in sheet.iter_rows(values_only=True):
                if not header:
                    header = [_nfkc(value) for value in values]
                    continue
                record = {name: value for name, value in zip(header, values) if name}
                record["_workbook"] = key
                record["_file"] = sources["files"][key]
                record["_english"] = english_ingredients(record.get("成分名", ""), names)
                yield record
        finally:
            workbook.close()


def _price(value: object, unit: str) -> str:
    if value in (None, ""):
        return ""
    amount = f"{float(value):g}" if isinstance(value, (int, float)) else _nfkc(value)
    return f"JPY {amount} per {unit}" if unit else f"JPY {amount}"


FORM_START = re.compile(
    r"(配合)?(OD|口腔内崩壊|徐放|腸溶|チュアブル)?"
    r"(錠|カプセル|細粒|顆粒|散|ドライシロップ|シロップ|内用液|内服液|注|点眼液|点鼻液|軟膏|クリーム|テープ|パップ|坐剤|吸入|キット|エキス)"
)


def english_product(
    name: str, japanese_ingredient: str, english: str, strength: str, form: str, company: str = ""
) -> str:
    """The product in English, keeping what tells it apart.

        アトルバスタチン錠10mg「サワイ」 -> Atorvastatin calcium hydrate 10 mg Tablet [Sawai]
        リピトール錠10mg               -> Ripitoru (romanised brand) - Atorvastatin ... 10 mg Tablet

    A generic named after its ingredient carries the maker's mark in 「」; a
    brand is romanised and says so. A brand in kanji is left out rather than
    shown untranslated.
    """
    text = _nfkc(name)
    start = FORM_START.search(text)
    # Combination tablets give their dose as a code after the form: 配合錠LD, HD.
    code = re.match(r"\s*([A-Za-z0-9.]+)", re.split(r"「", text[start.end():])[0]) if start else None
    label = " ".join(part for part in (english, strength, form, code.group(1) if code and not strength else "") if part)
    mark = re.search(r"「([^」]+)」", text)
    if mark:
        # A mark in Japanese (「サワイ」, 「日医工」) is the maker's short name, and
        # the maker's English name says it better than a romanisation.
        tag = mark.group(1)
        if _has_japanese(tag):
            tag = company if company and not _has_japanese(company) else romanise(tag)
        return f"{label} [{tag}]" if tag and not _has_japanese(tag) else label
    brand = re.split(r"\d", text[: start.start()] if start else text)[0].strip()
    ingredient = _name_key(japanese_ingredient)
    if not brand or ingredient.startswith(_name_key(brand)) or _name_key(brand).startswith(ingredient[:4]):
        return label
    romanised = romanise(brand)
    if _has_japanese(romanised):
        return label
    return f"{romanised} (romanised brand) - {label}"


def build_nhi_rows(records: Iterable[dict[str, Any]], fetched_at: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for record in records:
        english = record.get("_english", "")
        product_name = _nfkc(record.get("品名"))
        if not english or not product_name:
            continue
        strength, unit = strength_and_unit(record.get("規格", ""))
        form = dosage_form(product_name, record["_workbook"])
        company = english_company(record.get("メーカー名", ""))
        originator = _nfkc(record.get("先発医薬品"))
        generic = _nfkc(record.get("診療報酬において加算等の算定対象となる後発医薬品"))
        scope = "Originator" if originator == "先発品" else "Generic" if generic == "後発品" else ""
        until = _nfkc(record.get("経過措置による使用期限"))
        status = "NHI listed" + (f" (transitional, delisting: {until})" if until else "")
        yield _match_text(english), {
            "substance": english,
            "active_substance": english,
            "source_substance": english,
            "product": english_product(product_name, record.get("成分名", ""), english, strength, form, company),
            "local_product_name": product_name,
            "company": company,
            "country": "Japan",
            "region": "JP",
            "status": status,
            "authorisation_scope": scope,
            "strength": strength,
            "dosage_form": form,
            "route": ROUTES[record["_workbook"]],
            "price": _price(record.get("薬価"), unit),
            "registration_number": _nfkc(record.get("薬価基準収載医薬品コード")),
            "registration_number_type": "NHI drug price code",
            "source": "MHLW Japan",
            "source_url": record["_file"],
            "product_url": "",
            "document_type": "MHLW NHI drug price list entry",
            "last_checked": fetched_at,
        }


MHLW_NHI = add_register(
    OpenRegister(
        source="MHLW Japan",
        country="Japan",
        region="JP",
        url=INDEX_PAGE,
        slug="mhlw_nhi_price_list",
        max_age_seconds=7 * 24 * 3600,
        build_rows=build_nhi_rows,
        fetch=fetch_list,
        read_records=read_list,
    )
)


def run_mhlw_japan_search(substance: str) -> list[dict[str, Any]]:
    return search_register(MHLW_NHI, substance)
