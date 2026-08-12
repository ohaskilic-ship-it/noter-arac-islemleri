import os
import glob
import re
import unicodedata
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, render_template, request, Response
from rapidfuzz import fuzz, process
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

app = Flask(__name__)

DATA_DIR = Path(__file__).parent / "data"
CSV_PRIMARY = DATA_DIR / "kasko_guncel.csv"
CSV_PATTERN = str(DATA_DIR / "kasko_guncel*.csv")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
APP_VERSION = "3.1-polish"

FEE_CONFIG = {
    "year": 2026,
    "harc_rate": 0.002,
    "min_harc": 1000.00,
    "noter_ucreti": 528.63,
    "artes_tescil": 350.92,
    "kdv_rate": 0.20,
    "darphane": 36.00,
    "tescil": 1511.00,
}

df = None
brands = []
year_columns = []


def normalize(value):
    if value is None:
        return ""
    s = str(value).strip().upper()
    tr = str.maketrans({
        "Ç": "C", "Ğ": "G", "İ": "I", "Ö": "O",
        "Ş": "S", "Ü": "U", "Â": "A", "Î": "I", "Û": "U"
    })
    s = s.translate(tr)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def compact_normalize(value):
    """E200, E 200 ve E-200 gibi yazımları aynı anahtara dönüştürür."""
    return normalize(value).replace(" ", "")

def load_csv():
    global df, brands, year_columns

    if CSV_PRIMARY.exists():
        active_file = str(CSV_PRIMARY)
    else:
        files = sorted(glob.glob(CSV_PATTERN), key=os.path.getmtime, reverse=True)
        if not files:
            raise FileNotFoundError("data klasöründe kasko_guncel*.csv dosyası bulunamadı.")
        active_file = files[0]

    data = pd.read_csv(
        active_file,
        sep=";",
        skiprows=1,
        encoding="utf-8-sig",
        dtype=str,
        keep_default_na=False
    )

    data.columns = [str(c).strip() for c in data.columns]
    for col in ["Marka Kodu", "Tip Kodu", "Marka Adı", "Tip Adı"]:
        if col not in data.columns:
            raise ValueError(f"CSV içinde '{col}' sütunu bulunamadı.")

    data["_brand_norm"] = data["Marka Adı"].map(normalize)
    data["_type_norm"] = data["Tip Adı"].map(normalize)
    data["_full_norm"] = (data["Marka Adı"] + " " + data["Tip Adı"]).map(normalize)
    data["_type_compact"] = data["Tip Adı"].map(compact_normalize)
    data["_full_compact"] = (data["Marka Adı"] + " " + data["Tip Adı"]).map(compact_normalize)
    data["_row_key"] = data["Marka Kodu"].astype(str) + ":" + data["Tip Kodu"].astype(str)

    year_columns = [c for c in data.columns if re.fullmatch(r"\d{4}", str(c))]
    for y in year_columns:
        data[y] = pd.to_numeric(
            data[y].astype(str)
                .str.replace(".", "", regex=False)
                .str.replace(",", "", regex=False),
            errors="coerce"
        ).fillna(0).astype(int)

    df = data
    brands = sorted(df["_brand_norm"].dropna().unique().tolist())
    return Path(files[0]).name


ACTIVE_CSV = load_csv()


class VehicleTurn(BaseModel):
    year: int | None = None
    brand: str | None = None
    model_or_type: str | None = None
    descriptors: list[str] = Field(default_factory=list)
    is_greeting_only: bool = False


class CandidateSelection(BaseModel):
    matching_keys: list[str] = Field(default_factory=list)
    confidence: str = "medium"


class ClarifyingQuestion(BaseModel):
    question: str
    options: list[str] = Field(default_factory=list)


class ConversationReply(BaseModel):
    reply: str
    options: list[str] = Field(default_factory=list)


def get_ai_client():
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        return None
    return genai.Client(api_key=key)


def ai_json(prompt, schema, max_tokens=700):
    client = get_ai_client()
    if client is None:
        raise RuntimeError("AI_UNAVAILABLE")

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                max_output_tokens=max_tokens,
                temperature=0.1,
            ),
        )
        return schema.model_validate_json(response.text).model_dump()
    except Exception as exc:
        text = str(exc).upper()
        if "429" in text or "RESOURCE_EXHAUSTED" in text or "QUOTA" in text:
            raise RuntimeError("AI_QUOTA") from exc
        raise RuntimeError("AI_UNAVAILABLE") from exc



COMMON_WORDS = {
    "MODEL", "MODELI", "ARAC", "ARACI", "ARACIM", "ARACIMIZ", "KASKO",
    "DEGER", "DEGERI", "NEDIR", "BUL", "BAK", "MERHABA", "SELAM",
    "BENIM", "BIZIM", "BIR", "VE", "ILE", "ICIN", "TL", "MARKA",
    "MARKASI", "TIP", "TIPI", "SERISI", "SERI", "DEGIL", "DEĞIL",
    "EVET", "HAYIR", "PEKI", "TAMAM"
}


def local_extract_turn(message, state):
    """
    Yaygın mesajları Gemini'ye gitmeden çözer.
    Marka biliniyorsa model araması yalnızca o markanın Marka Kodu havuzunda yapılır.
    """
    raw = str(message or "").strip()
    norm = normalize(raw)
    result = {
        "year": None,
        "brand": None,
        "model_or_type": None,
        "descriptors": [],
        "is_greeting_only": False,
    }

    greeting_tokens = {"MERHABA", "SELAM", "SELAMLAR", "HEY", "SA"}
    toks = norm.split()
    if toks and all(t in greeting_tokens for t in toks):
        result["is_greeting_only"] = True
        return result

    ym = re.search(r"\b(19\d{2}|20\d{2})\b", norm)
    if ym:
        result["year"] = int(ym.group(1))

    detected_brand = detect_brand_locally(norm)
    if detected_brand:
        result["brand"] = detected_brand

    scope_state = dict(state or {})
    if result["brand"]:
        scope_state["brand"] = result["brand"]
        scope_state = establish_brand_code_lock(scope_state)

    allowed_codes = scope_state.get("brand_codes") or []

    brand_tokens = set()
    if result["brand"]:
        brand_tokens.update(normalize(result["brand"]).split())
    elif state.get("brand"):
        brand_tokens.update(normalize(state["brand"]).split())

    cleaned_tokens = []
    for token in toks:
        if token == str(result["year"] or ""):
            continue
        if token in COMMON_WORDS or token in brand_tokens:
            continue
        cleaned_tokens.append(token)

    candidate_text = " ".join(cleaned_tokens).strip()

    if candidate_text:
        rows, score = global_model_candidates(
            candidate_text,
            raw,
            limit=160,
            allowed_brand_codes=allowed_codes
        )

        if not rows.empty and score >= 72:
            compact_candidate = compact_normalize(candidate_text)

            if (
                re.search(r"[A-Z]", compact_candidate)
                and re.search(r"\d", compact_candidate)
                and len(compact_candidate) <= 12
            ):
                result["model_or_type"] = candidate_text.upper()
            else:
                hint = common_model_hint(rows)
                result["model_or_type"] = hint or candidate_text.title()

            if not result["brand"]:
                unique_brands = rows["Marka Adı"].drop_duplicates().tolist()
                if len(unique_brands) == 1:
                    result["brand"] = str(unique_brands[0])

        else:
            best = None
            for token in cleaned_tokens:
                if len(token) < 2:
                    continue

                rows2, score2 = global_model_candidates(
                    token,
                    raw,
                    limit=160,
                    allowed_brand_codes=allowed_codes
                )

                if not rows2.empty and (best is None or score2 > best[2]):
                    best = (token, rows2, score2)

            if best and best[2] >= 78:
                token, rows2, score2 = best
                result["model_or_type"] = (
                    token.upper() if any(c.isdigit() for c in token) else token.title()
                )

                if not result["brand"]:
                    unique_brands = rows2["Marka Adı"].drop_duplicates().tolist()
                    if len(unique_brands) == 1:
                        result["brand"] = str(unique_brands[0])

    if cleaned_tokens and not result["model_or_type"]:
        result["descriptors"] = cleaned_tokens

    return result

def extract_turn_smart(message, state):
    raw = str(message or "").strip()
    norm = normalize(raw)

    # Bir araç ailesi kilitlendiyse; yakıt, paket, şanzıman gibi kısa cevapları
    # yeni bir model sanma. Araç ancak kullanıcı açıkça düzeltme yaparsa değişir.
    if state.get("family_keys") or state.get("candidate_keys"):
        correction_words = {
            "HAYIR", "DEGIL", "DEĞIL", "ASLINDA", "YANLIS", "YANLIŞ",
            "DUZELTEYIM", "DÜZELTEYIM", "MODELIM", "MODELİM"
        }
        has_correction = any(w in norm.split() for w in correction_words)

        # Yıl düzeltmesi her zaman kabul edilir.
        ym = re.search(r"\b(19\d{2}|20\d{2})\b", norm)
        if ym and len(norm.split()) <= 4 and not has_correction:
            return {
                "year": int(ym.group(1)),
                "brand": None,
                "model_or_type": None,
                "descriptors": [],
                "is_greeting_only": False,
            }

        # Açık bir marka veya "hayır/değil/aslında" gibi düzeltme varsa
        # normal çözümleyici yeni aracı anlamaya çalışabilir.
        explicit_brand = False
        for bnorm in brands:
            if re.search(rf"\b{re.escape(bnorm)}\b", norm):
                explicit_brand = True
                break

        if not has_correction and not explicit_brand:
            return {
                "year": None,
                "brand": None,
                "model_or_type": None,
                "descriptors": [t for t in norm.split() if t not in COMMON_WORDS],
                "is_greeting_only": False,
            }

    local = local_extract_turn(message, state)

    if local["is_greeting_only"] or local["year"] or local["brand"] or local["model_or_type"]:
        return local

    if state.get("candidate_keys"):
        if norm:
            local["descriptors"] = [t for t in norm.split() if t not in COMMON_WORDS]
            return local

    try:
        return extract_turn(message, state)
    except Exception:
        local["descriptors"] = [t for t in norm.split() if t not in COMMON_WORDS]
        return local

def extract_turn(message, state):
    state_text = (
        f"Bilinen yıl: {state.get('year') or 'yok'}\n"
        f"Bilinen marka: {state.get('brand') or 'yok'}\n"
        f"Bilinen model: {state.get('model_or_type') or 'yok'}"
    )

    prompt = f"""
Türkiye'deki araç kasko değer listesinde arama yapan bir asistan için
kullanıcının SON mesajındaki araç bilgilerini çıkar.

{state_text}

Son kullanıcı mesajı:
{message}

Kurallar:
- year: Son mesajda açıkça yıl söyleniyorsa yaz, yoksa null.
- brand: Son mesajda açıkça marka söyleniyorsa yaz, yoksa null.
- model_or_type: Son mesajda model/seri adı söyleniyorsa yaz. Örn: Passat, Golf,
  Corolla, 320i, Clio. Söylenmiyorsa null.
- descriptors: Motor, yakıt, güç, şanzıman, kasa, donanım/paket gibi SON mesajda
  verilen tüm ayırt edici ifadeleri yaz. Örn ["1.5", "benzinli", "business"].
- Kullanıcının söylediği bilgileri uydurma veya değiştirme.
- "merhaba", "selam" gibi sadece sohbet mesajıysa is_greeting_only=true.
- Plakadan araç bilgisi tahmin etme.
"""
    return ai_json(prompt, VehicleTurn)


def best_brand_name(raw_brand):
    if not raw_brand:
        return None, 0
    q = normalize(raw_brand)
    match = process.extractOne(q, brands, scorer=fuzz.WRatio)
    if not match:
        return None, 0
    return match[0], float(match[1])


def rows_from_keys(keys, year=None):
    if not keys:
        return df.iloc[0:0].copy()
    rows = df[df["_row_key"].isin(keys)].copy()
    if year and str(year) in year_columns:
        rows = rows[rows[str(year)] > 0].copy()
    return rows


def token_search_initial(year, brand, model_or_type, descriptors, raw_message):
    if not year or str(year) not in year_columns:
        return df.iloc[0:0].copy()

    pool = df[df[str(year)] > 0].copy()
    if pool.empty:
        return pool

    brand_norm = None
    if brand:
        brand_norm, brand_score = best_brand_name(brand)
        if brand_norm and brand_score >= 65:
            pool = pool[pool["_brand_norm"] == brand_norm].copy()

    model_q = normalize(model_or_type or "")
    descriptor_q = normalize(" ".join(descriptors or []))

    # Güçlü yöntem 1: model ifadesi Tip Adı içinde geçiyorsa tüm gerçek adayları al.
    if model_q:
        exactish = pool[pool["_type_norm"].str.contains(re.escape(model_q), regex=True, na=False)].copy()
        if not exactish.empty:
            return exactish.head(80)

        # Model birden fazla kelimeyse kelimelerin tümünün geçtiği kayıtları dene.
        words = [w for w in model_q.split() if len(w) >= 2]
        if words:
            mask = pd.Series(True, index=pool.index)
            for w in words:
                mask &= pool["_type_norm"].str.contains(rf"\b{re.escape(w)}\b", regex=True, na=False)
            exact_words = pool[mask].copy()
            if not exact_words.empty:
                return exact_words.head(80)

    # Marka yazılmadıysa "2021 Passat" gibi sorgularda ham mesajdan model kelimesini yakala.
    raw_norm = normalize(raw_message)
    stop = {
        "MODEL", "KASKO", "DEGER", "DEGERI", "NEDIR", "ARAC", "ARABA",
        str(year), "BENIM", "BUL", "BAK", "TL"
    }
    raw_words = [w for w in raw_norm.split() if w not in stop and len(w) >= 3]
    for w in raw_words:
        hits = pool[pool["_type_norm"].str.contains(rf"\b{re.escape(w)}\b", regex=True, na=False)]
        if 1 <= len(hits) <= 80:
            return hits.copy()

    # Son çare: fuzzy. Burada doğrudan sonuç dönmek yerine aday havuzu çıkarıyoruz.
    query = normalize(" ".join(filter(None, [brand or "", model_or_type or "", descriptor_q])))
    if not query:
        return pool.iloc[0:0].copy()

    matches = process.extract(
        query,
        pool["_full_norm"].tolist(),
        scorer=fuzz.WRatio,
        limit=min(30, len(pool))
    )
    if not matches:
        return pool.iloc[0:0].copy()

    good_indexes = []
    top_score = matches[0][1]
    for _, score, idx in matches:
        if score >= max(58, top_score - 12):
            good_indexes.append(pool.index[idx])

    return pool.loc[good_indexes].copy()



def establish_brand_code_lock(state):
    """
    Marka belirlendiğinde o markaya ait bütün Marka Kodu değerlerini kilitler.
    Aynı marka birden fazla kod kullanıyorsa tüm kodlar korunur.
    """
    brand = state.get("brand")
    if not brand:
        return state

    bnorm, score = best_brand_name(brand)
    if not bnorm or score < 65:
        return state

    rows = df[df["_brand_norm"] == bnorm].copy()
    if rows.empty:
        return state

    state["brand"] = str(rows["Marka Adı"].iloc[0])
    state["brand_codes"] = rows["Marka Kodu"].astype(str).drop_duplicates().tolist()
    return state


def enforce_brand_code_lock(rows, state):
    """Adayların kilitli marka kodlarının dışına çıkmasını engeller."""
    if rows.empty:
        return rows

    codes = [str(x) for x in (state.get("brand_codes") or [])]
    if not codes:
        return rows

    return rows[rows["Marka Kodu"].astype(str).isin(codes)].copy()


def detect_brand_locally(norm_text):
    """Mercedes / Mercedes-Benz gibi günlük marka yazımlarını Gemini kullanmadan bulur."""
    if not norm_text:
        return None

    direct = []
    for bnorm in brands:
        if bnorm in norm_text:
            direct.append(bnorm)

    if direct:
        chosen = max(direct, key=len)
        return str(df.loc[df["_brand_norm"] == chosen, "Marka Adı"].iloc[0])

    tokens = [
        t for t in norm_text.split()
        if t not in COMMON_WORDS and not re.fullmatch(r"(19|20)\d{2}", t)
    ]
    if not tokens:
        return None

    query = " ".join(tokens)
    match = process.extractOne(query, brands, scorer=fuzz.WRatio)
    if match and float(match[1]) >= 84:
        return str(df.loc[df["_brand_norm"] == match[0], "Marka Adı"].iloc[0])

    return None


def global_model_candidates(model_or_type, raw_message="", limit=80, allowed_brand_codes=None):
    """
    Model/tip aramasını gerçek CSV kayıtlarında yapar.
    Marka biliniyorsa yalnızca o markaya ait Marka Kodu satırlarında arar.
    E200 / E 200 / E-200 aynı model araması olarak kabul edilir.
    """
    query = normalize(model_or_type or "")
    if not query:
        raw = normalize(raw_message)
        stop = {"MODEL", "KASKO", "DEGER", "DEGERI", "NEDIR", "ARAC", "ARABA",
                "BENIM", "BUL", "BAK", "TL"}
        words = [w for w in raw.split() if w not in stop and not w.isdigit() and len(w) >= 2]
        query = " ".join(words)

    if not query:
        return df.iloc[0:0].copy(), 0.0

    source = df
    codes = [str(x) for x in (allowed_brand_codes or [])]
    if codes:
        source = source[source["Marka Kodu"].astype(str).isin(codes)].copy()

    if source.empty:
        return source, 0.0

    compact_q = compact_normalize(query)

    if len(compact_q) >= 2:
        hits = source[
            source["_type_compact"].str.contains(re.escape(compact_q), regex=True, na=False)
        ].copy()
        if not hits.empty:
            return hits.head(limit), 100.0

    hits = source[
        source["_type_norm"].str.contains(re.escape(query), regex=True, na=False)
    ].copy()
    if not hits.empty:
        return hits.head(limit), 100.0

    words = [w for w in query.split() if len(w) >= 2]
    if words:
        mask = pd.Series(True, index=source.index)
        for w in words:
            mask &= source["_type_norm"].str.contains(
                rf"\b{re.escape(w)}\b", regex=True, na=False
            )
        hits = source[mask].copy()
        if not hits.empty:
            return hits.head(limit), 96.0

    uniques = source[
        ["_row_key", "Marka Adı", "Tip Adı", "_full_norm", "_full_compact"]
    ].drop_duplicates("_row_key")

    use_compact = any(c.isdigit() for c in compact_q)
    fuzzy_query = compact_q if use_compact else query
    choices = uniques["_full_compact"].tolist() if use_compact else uniques["_full_norm"].tolist()

    matches = process.extract(
        fuzzy_query,
        choices,
        scorer=fuzz.WRatio,
        limit=min(40, len(uniques))
    )
    if not matches:
        return source.iloc[0:0].copy(), 0.0

    top_score = float(matches[0][1])
    if top_score < 62:
        return source.iloc[0:0].copy(), top_score

    keys = [
        uniques.iloc[idx]["_row_key"]
        for _, score, idx in matches
        if score >= max(62, top_score - 8)
    ]
    return source[source["_row_key"].isin(keys)].copy().head(limit), top_score

def natural_conversation_reply(facts, goal, options=None, fallback=None):
    """
    Kararları kod verir; Gemini yalnızca kullanıcıya söylenecek doğal cümleyi yazar.
    Böylece sohbet daha insani olur ama araç/değer uydurulmaz.
    """
    options = options or []
    fallback = fallback or goal
    prompt = f"""
Sen profesyonel ama sıcak konuşan bir araç kasko değeri asistanısın.
Kullanıcıyla kısa, doğal ve gerçekten sohbet ediyormuş gibi Türkçe konuş.

KESİN BİLGİLER:
{facts}

ŞİMDİKİ AMAÇ:
{goal}

Kurallar:
- Kesin bilgiler dışında marka, model, yıl, motor, paket veya fiyat UYDURMA.
- Kullanıcının daha önce verdiği bilgiyi tekrar sorma.
- Eğer bir bilgiyi anladıysan bunu doğal biçimde teyit et:
  "Anladım, aracınız 2004 model." gibi.
- Tek seferde mümkünse yalnızca bir sonraki gerekli bilgiyi sor.
- Resmî ama soğuk olmayan bir dil kullan.
- 1-3 kısa cümle yeterli.
- Teknik CSV, sütun, veri tabanı, algoritma gibi ifadeler kullanma.
- Çıktı sadece şemaya uygun olsun.
"""
    try:
        result = ai_json(prompt, ConversationReply, max_tokens=300)
        result["options"] = options[:5]
        return result
    except Exception:
        return {"reply": fallback, "options": options[:5]}


def infer_brand_from_model(state, turn, message):
    """
    Marka yazılmasa bile model/tipten markayı çıkarır.
    Marka zaten biliniyorsa yalnızca kilitli Marka Kodu havuzunda arama yapar.
    """
    model_text = turn.get("model_or_type") or state.get("model_or_type")
    if not model_text:
        return state, df.iloc[0:0].copy(), 0.0

    global_rows, score = global_model_candidates(
        model_text,
        message,
        allowed_brand_codes=state.get("brand_codes")
    )

    if global_rows.empty:
        return state, global_rows, score

    unique_brands = global_rows["Marka Adı"].drop_duplicates().tolist()
    if len(unique_brands) == 1 and score >= 70:
        state["brand"] = str(unique_brands[0])
        state = establish_brand_code_lock(state)

    return state, global_rows, score

def candidate_lines(rows, year, max_rows=35):
    items = []
    for _, r in rows.head(max_rows).iterrows():
        items.append(
            f"{r['_row_key']} | {r['Marka Adı']} | {r['Tip Adı']} | "
            f"{year} değeri={int(r[str(year)])}"
        )
    return "\n".join(items)




DIESEL_MARKERS = {
    "DCI", "TDI", "CDI", "HDI", "BLUEHDI", "CRDI", "D4D", "D-4D",
    "DDIS", "DIESEL", "DIZEL", "MULTIJET", "MJET", "JTD"
}
ELECTRIC_MARKERS = {"ELECTRIC", "ELEKTRIK", "EV", "BEV", "ZE", "ELECTRIQUE"}
HYBRID_MARKERS = {"HYBRID", "HIBRIT", "PHEV", "HEV", "MHEV"}
AUTO_MARKERS = {
    "DSG", "EDC", "DCT", "CVT", "TIPTRONIC", "STEPTRONIC", "PDK",
    "AUTOMATIC", "OTOMATIK", "OV", "A/T", "AT"
}
BODY_MAP = {
    "SEDAN": ["SEDAN"],
    "Hatchback": ["HATCHBACK", "HB"],
    "Station / Variant": ["VARIANT", "STATION", "SW", "TOURER", "ESTATE"],
    "SUV": ["SUV"],
    "Coupe": ["COUPE"],
    "Cabrio": ["CABRIO", "CONVERTIBLE"],
    "Pickup": ["PICKUP", "PICK UP"],
    "Van": ["VAN", "PANELVAN", "PANEL VAN"],
}


def infer_fuel_label(type_name):
    text = normalize(type_name)
    tokens = set(text.split())

    if any(x in text for x in HYBRID_MARKERS) or any(x in tokens for x in HYBRID_MARKERS):
        return "Hibrit"
    if any(x in text for x in ELECTRIC_MARKERS) or any(x in tokens for x in ELECTRIC_MARKERS):
        return "Elektrikli"
    if any(x in text for x in DIESEL_MARKERS) or any(x in tokens for x in DIESEL_MARKERS):
        return "Dizel"

    # Kasko listesindeki tip adlarında benzinli araçların önemli bir kısmında
    # "benzin" kelimesi yazmadığı için, dizel/hibrit/elektrik olmayan motorlu
    # seçenekler benzinli olarak sınıflandırılır.
    return "Benzinli"


def infer_transmission_label(type_name):
    text = normalize(type_name)
    tokens = set(text.split())
    if any(x in text for x in AUTO_MARKERS) or any(x in tokens for x in AUTO_MARKERS):
        return "Otomatik"
    return "Manuel"


def infer_body_label(type_name):
    text = normalize(type_name)
    for label, markers in BODY_MAP.items():
        if any(m in text for m in markers):
            return label
    return None


def infer_engine_label(type_name):
    text = normalize(type_name)
    match = re.search(r"(?<!\d)(0\.[6-9]|[1-6]\.\d)(?!\d)", text)
    return match.group(1) if match else None


def infer_power_label(type_name):
    """
    Tip adından motor gücünü ayırt etmeye çalışır.
    Örn: DCI 85, DCI (105), 110 EDC, 115 CVT -> 85 / 105 / 110 / 115 HP
    """
    text = normalize(type_name)

    # Parantez içindeki tipik güç değerleri
    matches = re.findall(r"\((\d{2,3})\)", text)
    for value in matches:
        n = int(value)
        if 60 <= n <= 700:
            return f"{n} HP"

    # Motor ifadesinden sonra gelen tipik güç değerleri
    patterns = [
        r"\b(?:DCI|TDI|CDI|HDI|TSI|TFSI|TCE|MPI|16V)\s+(\d{2,3})\b",
        r"\b(\d{2,3})\s+(?:EDC|DSG|CVT|OV|E5|E6)\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            n = int(m.group(1))
            if 60 <= n <= 700:
                return f"{n} HP"

    return None


def family_rows_from_state(state):
    keys = state.get("family_keys") or []
    if not keys:
        return df.iloc[0:0].copy()

    rows = df[df["_row_key"].isin(keys)].copy()

    locked_brand = state.get("locked_brand")
    if locked_brand:
        bnorm, score = best_brand_name(locked_brand)
        if bnorm and score >= 65:
            rows = rows[rows["_brand_norm"] == bnorm].copy()

    return rows


def establish_family_lock(state):
    """
    Model/tip tanındığında önce Marka Kodu kilidi, sonra araç ailesi kilidi uygulanır.
    """
    model = state.get("model_or_type")
    if not model:
        return state

    rows, score = global_model_candidates(
        model,
        model,
        limit=300,
        allowed_brand_codes=state.get("brand_codes")
    )

    if rows.empty or score < 68:
        return state

    rows = enforce_brand_code_lock(rows, state)
    if rows.empty:
        return state

    unique_brands = rows["Marka Adı"].drop_duplicates().tolist()
    if len(unique_brands) == 1:
        state["brand"] = str(unique_brands[0])
        state["locked_brand"] = str(unique_brands[0])
        state = establish_brand_code_lock(state)

    compact_model = compact_normalize(model)
    if re.search(r"[A-Z]", compact_model) and re.search(r"\d", compact_model):
        state["locked_model"] = str(model).upper()
    else:
        hint = common_model_hint(rows)
        if hint:
            state["model_or_type"] = hint
            state["locked_model"] = hint
        elif not state.get("locked_model"):
            state["locked_model"] = state.get("model_or_type")

    state["family_keys"] = rows["_row_key"].drop_duplicates().tolist()
    return state

def enforce_family_lock(rows, state):
    """Hiçbir aşamada adayların kilitli araç ailesinin dışına çıkmasına izin verme."""
    if rows.empty:
        return rows

    family_keys = set(state.get("family_keys") or [])
    if family_keys:
        locked = rows[rows["_row_key"].isin(family_keys)].copy()
        if not locked.empty:
            rows = locked
        else:
            return rows.iloc[0:0].copy()

    locked_brand = state.get("locked_brand")
    if locked_brand:
        bnorm, score = best_brand_name(locked_brand)
        if bnorm and score >= 65:
            rows = rows[rows["_brand_norm"] == bnorm].copy()

    return rows


def candidate_package_options(rows, state):
    """
    Motor/yakıt/şanzıman ayrımları bittikten sonra tip adlarından güvenli paket
    seçenekleri çıkarmaya çalışır. Yalnızca gerçek adaylarda geçen kelimeleri döndürür.
    """
    model_tokens = set(normalize(state.get("locked_model") or state.get("model_or_type") or "").split())
    ignore = set()
    ignore.update(model_tokens)
    ignore.update(DIESEL_MARKERS)
    ignore.update(ELECTRIC_MARKERS)
    ignore.update(HYBRID_MARKERS)
    ignore.update(AUTO_MARKERS)
    ignore.update({
        "SEDAN", "HATCHBACK", "HB", "VARIANT", "STATION", "SW", "SUV",
        "COUPE", "CABRIO", "CV", "HP", "E5", "E6", "16V", "8V",
        "ACT", "SCR", "BMT", "BLUE", "TECH", "EDITION"
    })

    values = []
    for name in rows["Tip Adı"].tolist():
        tokens = normalize(name).split()
        chosen = None
        for token in tokens:
            if token in ignore:
                continue
            if re.fullmatch(r"\d+", token):
                continue
            if re.fullmatch(r"\d\.\d", token):
                continue
            if len(token) < 3:
                continue
            # Teknik motor kodu/güç ifadelerini mümkün olduğunca atla.
            if token in {"TSI", "TFSI", "TCE", "MPI", "VVT", "VTEC", "ECOBOOST"}:
                continue
            chosen = token
            break
        if chosen:
            values.append(chosen.title())

    unique = []
    for v in values:
        if v not in unique:
            unique.append(v)
    return unique[:8]


def make_safe_question(rows, year, state):
    """
    Yalnızca gerçek kalan adaylara göre soru üretir.
    Aday sayısı çok azaldığında soyut soru sormak yerine gerçek tipleri seçtirir.
    """
    rows = enforce_brand_code_lock(rows, state)
    rows = enforce_family_lock(rows, state)

    if rows.empty:
        return {
            "question": "Aracı bu bilgilerle netleştiremedim. Motor, yakıt, şanzıman veya donanım bilgisinden bildiğiniz birini söyler misiniz?",
            "options": []
        }

    known = []
    if year:
        known.append(f"{year} model")
    if state.get("brand"):
        known.append(str(state["brand"]))
    if state.get("locked_model") or state.get("model_or_type"):
        known.append(str(state.get("locked_model") or state.get("model_or_type")))
    prefix = " ".join(known).strip()
    prefix = f"{prefix} için" if prefix else "Aracınız için"

    # En önemli UX düzeltmesi:
    # Sadece 2-3 gerçek kayıt kaldıysa kullanıcıya artık aynı genel soruyu
    # tekrar tekrar sormak yerine kalan kayıtların kendisini seçtir.
    if 2 <= len(rows) <= 3:
        options = []
        for name in rows["Tip Adı"].drop_duplicates().tolist():
            if name not in options:
                options.append(str(name))

        return {
            "question": (
                f"{prefix} {len(options)} olası kayıt kaldı. "
                "Aşağıdaki seçeneklerden aracınıza ait olanı seçer misiniz?"
            ),
            "options": options[:3]
        }

    # 1) Yakıt
    fuels = []
    for value in rows["Tip Adı"].map(infer_fuel_label).tolist():
        if value not in fuels:
            fuels.append(value)
    if 1 < len(fuels) <= 4:
        return {
            "question": f"Anladım. {prefix} birkaç seçenek kaldı. Yakıt türü nedir?",
            "options": fuels
        }

    # 2) Kasa
    bodies = []
    for value in rows["Tip Adı"].map(infer_body_label).tolist():
        if value and value not in bodies:
            bodies.append(value)
    if 1 < len(bodies) <= 5:
        return {
            "question": f"{prefix} kasa tipini de netleştirelim. Hangisi aracınıza uyuyor?",
            "options": bodies
        }

    # 3) Motor hacmi
    engines = []
    for value in rows["Tip Adı"].map(infer_engine_label).tolist():
        if value and value not in engines:
            engines.append(value)
    if 1 < len(engines) <= 6:
        return {
            "question": f"{prefix} motor hacmi nedir?",
            "options": engines
        }

    # 4) Motor gücü
    powers = []
    for value in rows["Tip Adı"].map(infer_power_label).tolist():
        if value and value not in powers:
            powers.append(value)
    if 1 < len(powers) <= 6:
        return {
            "question": f"{prefix} motor gücü hangisi?",
            "options": powers
        }

    # 5) Şanzıman
    transmissions = []
    for value in rows["Tip Adı"].map(infer_transmission_label).tolist():
        if value not in transmissions:
            transmissions.append(value)
    if 1 < len(transmissions) <= 3:
        return {
            "question": f"{prefix} şanzıman tipi nedir?",
            "options": transmissions
        }

    # 6) Donanım / paket
    packages = candidate_package_options(rows, state)
    if 1 < len(packages) <= 8:
        return {
            "question": f"{prefix} donanım paketini de netleştirelim. Hangisi aracınıza daha yakın?",
            "options": packages[:5]
        }

    # 4-5 aday kaldıysa da gerçek tip adlarını seçenek olarak göstermek,
    # aynı soruyu tekrar etmekten daha kullanışlıdır.
    if 2 <= len(rows) <= 5:
        options = []
        for name in rows["Tip Adı"].drop_duplicates().tolist():
            if name not in options:
                options.append(str(name))
        return {
            "question": f"{prefix} birkaç kayıt kaldı. Aracınıza ait olan seçeneği seçer misiniz?",
            "options": options[:5]
        }

    return {
        "question": (
            f"{prefix} birden fazla kayıt kaldı. "
            "Motor gücü, şanzıman veya donanım paketinden bildiğiniz bir ayrıntıyı yazar mısınız?"
        ),
        "options": []
    }


def exact_candidate_selection(rows, user_message):
    """
    Kullanıcı ekranda gösterilen tam araç tiplerinden birini seçerse,
    genel motor/yakıt filtrelerine girmeden doğrudan o kaydı seçer.
    Örn: 'FLUENCE BUSINESS 1.5 DCI (85)' seçildiğinde 85 HP filtresinin
    diğer 85 HP kayıtlarını da tutması engellenir.
    """
    if rows.empty:
        return rows

    q_norm = normalize(user_message)
    q_compact = compact_normalize(user_message)

    if not q_norm:
        return rows.iloc[0:0].copy()

    # 1) Tam normalize eşleşme
    exact = rows[rows["_type_norm"] == q_norm].copy()
    if not exact.empty:
        return exact

    # 2) Noktalama/boşluk farklarına tolerans
    if "_type_compact" in rows.columns:
        exact_compact = rows[rows["_type_compact"] == q_compact].copy()
        if not exact_compact.empty:
            return exact_compact

    return rows.iloc[0:0].copy()

def refine_locally(rows, user_message):
    """Kısa cevapları yalnızca mevcut/kilitli aday havuzu içinde daralt."""
    if len(rows) <= 1:
        return rows

    # Kullanıcı gerçek tip seçeneklerinden birini seçtiyse bu seçim her şeyden önceliklidir.
    exact = exact_candidate_selection(rows, user_message)
    if not exact.empty:
        return exact

    q = normalize(user_message)
    if not q:
        return rows

    # Yakıt türü: özellikle "benzinli" için tip adında BENZIN yazması gerekmez.
    if "BENZIN" in q:
        hit = rows[rows["Tip Adı"].map(infer_fuel_label) == "Benzinli"].copy()
        if not hit.empty:
            return hit
    if "DIZEL" in q or "DIESEL" in q:
        hit = rows[rows["Tip Adı"].map(infer_fuel_label) == "Dizel"].copy()
        if not hit.empty:
            return hit
    if "ELEKTRIK" in q or re.search(r"\bEV\b", q):
        hit = rows[rows["Tip Adı"].map(infer_fuel_label) == "Elektrikli"].copy()
        if not hit.empty:
            return hit
    if "HIBRIT" in q or "HYBRID" in q:
        hit = rows[rows["Tip Adı"].map(infer_fuel_label) == "Hibrit"].copy()
        if not hit.empty:
            return hit

    # Şanzıman
    if "OTOMATIK" in q:
        hit = rows[rows["Tip Adı"].map(infer_transmission_label) == "Otomatik"].copy()
        if not hit.empty:
            return hit
    if "MANUEL" in q:
        hit = rows[rows["Tip Adı"].map(infer_transmission_label) == "Manuel"].copy()
        if not hit.empty:
            return hit

    # Kasa
    for label, markers in BODY_MAP.items():
        if normalize(label) in q or any(m in q for m in markers):
            hit = rows[rows["Tip Adı"].map(infer_body_label) == label].copy()
            if not hit.empty:
                return hit

    # Motor hacmi
    engine = re.search(r"(?<!\d)(0\.[6-9]|[1-6]\.\d)(?!\d)", q)
    if engine:
        wanted = engine.group(1)
        hit = rows[rows["Tip Adı"].map(infer_engine_label) == wanted].copy()
        if not hit.empty:
            return hit

    # Motor gücü / beygir
    power_match = re.search(r"\b(\d{2,3})\s*(?:HP|BG|BEYGIR)?\b", q)
    if power_match:
        wanted_power = f"{int(power_match.group(1))} HP"
        hit = rows[rows["Tip Adı"].map(infer_power_label) == wanted_power].copy()
        if not hit.empty:
            return hit

    # Kullanıcının yazdığı gerçek paket/teknik kelimeleri aday tip adlarında ara.
    terms = [t for t in q.split() if len(t) >= 2 and t not in COMMON_WORDS]
    if terms:
        mask = pd.Series(True, index=rows.index)
        matched_any = False
        for term in terms:
            term_mask = rows["_type_norm"].str.contains(rf"\b{re.escape(term)}\b", regex=True, na=False)
            if term_mask.any():
                mask &= term_mask
                matched_any = True
        if matched_any:
            hit = rows[mask].copy()
            if not hit.empty:
                return hit

    # Son çare fuzzy; fakat mevcut aday havuzunun dışına asla çıkılmaz.
    scores = []
    for idx, r in rows.iterrows():
        score = fuzz.WRatio(q, normalize(r["Tip Adı"]))
        scores.append((idx, score))
    best = max((s for _, s in scores), default=0)
    if best >= 78:
        keep = [idx for idx, s in scores if s >= best - 4]
        hit = rows.loc[keep].copy()
        if not hit.empty:
            return hit

    return rows

def refine_with_ai(rows, year, user_message):
    if len(rows) <= 1:
        return rows

    prompt = f"""
Aşağıda kasko listesinden GERÇEK araç adayları var.
Kullanıcının son cevabına UYAN adayların row key'lerini seç.

Kullanıcı cevabı:
{user_message}

Adaylar:
{candidate_lines(rows, year)}

Kurallar:
- Sadece listede bulunan key'leri döndür.
- Kullanıcı "1.5 benzinli" derse 1.5 TSI gibi açıkça uyumlu kayıtları seçebilirsin.
- "dizel" -> TDI/CDI/dCi/HDi gibi açık dizel ifadeleriyle uyumlu adayları seç.
- "benzinli" -> TSI/TFSI/TCE vb. benzinli ifadelerle uyumlu adayları seç; hibriti
  benzinli diye otomatik seçme.
- "otomatik" denince DSG, TIPTRONIC, EDC, DCT, AT vb. otomatik ifadeleri dikkate al.
- Kullanıcının cevabı hiçbir adayı ayırmıyorsa TÜM mevcut key'leri döndür.
- Emin olmadığın bilgiyi uydurma.
"""
    result = ai_json(prompt, CandidateSelection)
    keys = [k for k in result["matching_keys"] if k in set(rows["_row_key"])]
    if not keys:
        return rows
    return rows[rows["_row_key"].isin(keys)].copy()


def common_model_hint(rows):
    if rows.empty:
        return ""
    types_list = [normalize(x).split() for x in rows["Tip Adı"].tolist()]
    if not types_list:
        return ""
    common = []
    for token in types_list[0]:
        if len(token) >= 3 and all(token in toks for toks in types_list[1:]):
            if token not in {"DSG", "BMT", "ACT", "SCR", "TIPTRONIC", "TIPTR"}:
                common.append(token)
    return " ".join(common[:2])


def make_question(rows, year):
    if rows.empty:
        return {
            "question": "Bu bilgilerle listede uygun araç bulamadım. Marka, model ve model yılını biraz daha açık yazar mısınız?",
            "options": []
        }

    prompt = f"""
Sen bir kasko değeri danışmanısın. Aşağıdaki GERÇEK araç adaylarının arasından
doğru aracı bulmak için kullanıcıya TEK bir kısa ve doğal Türkçe soru sor.

Adaylar:
{candidate_lines(rows, year, max_rows=30)}

Amaç:
- En ayırt edici bilgiyi sor: önce gerekiyorsa gövde/kasa, motor-yakıt,
  şanzıman, güç veya donanım/paket.
- Aynı soruda gereksiz çok ayrıntı isteme.
- Kullanıcıyla gerçek bir danışman gibi kibar, sıcak ve doğal konuş.
- Kullanıcının daha önce verdiği bilgiyi tekrar sorma.
- Mümkünse önce anladığın bilgiyi kısa biçimde teyit edip sonra tek sorunu sor.
- Sorunun cevabı adayları gerçekten daraltabilsin.
- 2-5 kısa seçenek üret. Seçenekler adaylarda gerçekten bulunan ayrımlardan gelsin.
- "Bilmiyorum" seçeneği ekleme; kullanıcı zaten serbestçe yazabilir.
- Fiyatı henüz söyleme.
"""
    try:
        return ai_json(prompt, ClarifyingQuestion)
    except Exception:
        model = common_model_hint(rows)
        return {
            "question": f"{model or 'Aracınız'} için birden fazla uygun kayıt buldum. Motor hacmi, yakıt türü, şanzıman veya donanım paketinden bildiğiniz birini söyler misiniz?",
            "options": []
        }


def make_kasko_code(brand_code, type_code):
    """Marka Kodu + Tip Kodu birleştirilip soldan sıfırla 7 haneye tamamlanır."""
    brand_digits = re.sub(r"\D", "", str(brand_code))
    type_digits = re.sub(r"\D", "", str(type_code))
    combined = brand_digits + type_digits
    return combined.zfill(7)


def row_to_result(row, year, requested_year=None):
    requested_year = int(requested_year if requested_year is not None else year)
    listed_year = int(year)
    listed_value = int(row[str(listed_year)])

    if requested_year < listed_year:
        value = calculate_older_model_value(listed_value, listed_year, requested_year)
        calculated = True
        discount_years = listed_year - requested_year
    else:
        value = listed_value
        calculated = False
        discount_years = 0

    marka_kodu = re.sub(r"\D", "", str(row["Marka Kodu"]))
    tip_kodu = re.sub(r"\D", "", str(row["Tip Kodu"]))
    kasko_code = (marka_kodu + tip_kodu).zfill(7)

    return {
        "status": "found",
        "brand": str(row["Marka Adı"]),
        "type": str(row["Tip Adı"]),
        "year": requested_year,
        "value": value,
        "kasko_code": kasko_code,
        "calculated": calculated,
        "base_year": listed_year if calculated else None,
        "base_value": listed_value if calculated else None,
        "discount_years": discount_years,
        "source_file": ACTIVE_CSV
    }



def get_oldest_list_year():
    return min(int(y) for y in year_columns)


def calculate_older_model_value(base_value, base_year, target_year):
    """
    Listedeki en eski model yılından daha eski araçlar için her model yılı
    başına bir önceki yıl değeri üzerinden %10 indirim uygular.
    """
    year_difference = int(base_year) - int(target_year)
    if year_difference <= 0:
        return int(base_value)

    value = float(base_value)
    for _ in range(year_difference):
        value *= 0.90

    return int(round(value))


def professional_not_found_message():
    return (
        "Aracınıza uygun bir kasko değeri bulunamadı. "
        "Lütfen marka, model, motor, kasa veya donanım bilgilerini kontrol ederek tekrar deneyin."
    )



def deterministic_missing_question(state):
    """Known facts are never re-asked. No Gemini call is needed for basic slot filling."""
    year = state.get("year")
    brand = state.get("brand")
    model = state.get("model_or_type")

    if year and brand and not model:
        return f"Anladım, aracınız {year} model {brand}. Peki modeli nedir?"
    if year and model and not brand:
        return f"Anladım, aracınız {year} model {model}. Markasını da söyler misiniz?"
    if brand and model and not year:
        return f"Anladım, aracınız {brand} {model}. Model yılı nedir?"
    if year and not brand and not model:
        return f"Anladım, aracınız {year} model. Markası veya modeli nedir?"
    if brand and not year and not model:
        return f"Anladım, aracınız {brand}. Modeli ve model yılı nedir?"
    if model and not year:
        return f"{model} modelini anladım. Model yılı nedir?"
    return "Aracınızın model yılı ile marka/modelini yazar mısınız?"

def process_search(message, incoming_state):
    incoming_state = incoming_state if isinstance(incoming_state, dict) else {}

    state = {
        "year": incoming_state.get("year"),
        "brand": incoming_state.get("brand"),
        "model_or_type": incoming_state.get("model_or_type"),
        "candidate_keys": incoming_state.get("candidate_keys") or [],
        "family_keys": incoming_state.get("family_keys") or [],
        "locked_brand": incoming_state.get("locked_brand"),
        "locked_model": incoming_state.get("locked_model"),
        "brand_codes": incoming_state.get("brand_codes") or [],
    }

    if state["year"] not in (None, ""):
        try:
            state["year"] = int(state["year"])
        except (TypeError, ValueError):
            state["year"] = None

    turn = extract_turn_smart(message, state)

    if turn.get("is_greeting_only") and not any(
        [state.get("year"), state.get("brand"), state.get("model_or_type"), state["candidate_keys"]]
    ):
        reply = natural_conversation_reply(
            "Henüz araç hakkında bilgi yok.",
            "Kullanıcıyı selamla ve model yılı ile marka/model bilgisini doğal biçimde iste.",
            fallback="Merhaba 👋 Memnuniyetle yardımcı olayım. Aracınızın model yılı ile marka/modelini söyler misiniz?"
        )
        return {"status": "need_info", "question": reply["reply"], "options": [], "state": state}

    if turn.get("year"):
        if state.get("year") and int(turn["year"]) != int(state["year"]):
            state["candidate_keys"] = []
        state["year"] = int(turn["year"])

    if turn.get("brand"):
        incoming_brand = str(turn["brand"])

        if state.get("brand") and normalize(incoming_brand) != normalize(state["brand"]):
            state["candidate_keys"] = []
            state["family_keys"] = []
            state["locked_brand"] = None
            state["locked_model"] = None
            state["model_or_type"] = None
            state["brand_codes"] = []

        state["brand"] = incoming_brand
        state = establish_brand_code_lock(state)

    if turn.get("model_or_type"):
        incoming_model = str(turn["model_or_type"])

        if (
            state.get("model_or_type")
            and compact_normalize(incoming_model) != compact_normalize(state["model_or_type"])
        ):
            state["candidate_keys"] = []
            state["family_keys"] = []
            state["locked_model"] = None

        state["model_or_type"] = incoming_model

    # Modelden marka çıkarımı; ardından Marka Kodu ve aile kilitleri.
    state, global_rows, global_score = infer_brand_from_model(state, turn, message)

    if state.get("brand") and not state.get("brand_codes"):
        state = establish_brand_code_lock(state)

    if state.get("model_or_type") and not state.get("family_keys"):
        state = establish_family_lock(state)

    if state.get("year") and not state.get("brand") and not state.get("model_or_type"):
        return {
            "status": "need_info",
            "question": deterministic_missing_question(state),
            "options": [],
            "state": state
        }

    if not state.get("year"):
        return {
            "status": "need_info",
            "question": deterministic_missing_question(state),
            "options": [],
            "state": state
        }

    if state.get("brand") and not state.get("model_or_type"):
        return {
            "status": "need_info",
            "question": deterministic_missing_question(state),
            "options": [],
            "state": state
        }

    requested_year = int(state["year"])
    oldest_year = get_oldest_list_year()

    if str(requested_year) not in year_columns and requested_year >= oldest_year:
        reply = natural_conversation_reply(
            f"İstenen model yılı {requested_year}; bu yıl mevcut değer yılları arasında değil.",
            "Kullanıcıya teknik ayrıntı vermeden aracına uygun kasko değeri bulunamadığını söyle ve model yılını kontrol etmesini iste.",
            fallback="Bu bilgilerle aracınıza uygun bir kasko değeri bulamadım. Model yılını kontrol eder misiniz?"
        )
        return {"status": "not_found", "message": reply["reply"], "state": state}

    search_year = oldest_year if requested_year < oldest_year else requested_year

    # Katman 1: Marka Kodu Kilidi
    # Katman 2: Araç Ailesi Kilidi
    # Katman 3: yıl / yakıt / kasa / motor / şanzıman / paket
    if state["candidate_keys"]:
        candidates = rows_from_keys(state["candidate_keys"], search_year)
        candidates = enforce_brand_code_lock(candidates, state)
        candidates = enforce_family_lock(candidates, state)

        exact = exact_candidate_selection(candidates, message)
        if not exact.empty:
            candidates = exact
        else:
            candidates = refine_locally(candidates, message)

        candidates = enforce_brand_code_lock(candidates, state)
        candidates = enforce_family_lock(candidates, state)

    else:
        family_rows = family_rows_from_state(state)

        if not family_rows.empty:
            candidates = family_rows[
                family_rows[str(search_year)] > 0
            ].copy().head(300)

        elif state.get("model_or_type"):
            candidates, _ = global_model_candidates(
                state["model_or_type"],
                message,
                limit=300,
                allowed_brand_codes=state.get("brand_codes")
            )
            candidates = candidates[
                candidates[str(search_year)] > 0
            ].copy()

        else:
            candidates = token_search_initial(
                search_year,
                state.get("brand"),
                state.get("model_or_type"),
                turn.get("descriptors", []),
                message
            )

        if candidates.empty and not global_rows.empty:
            candidates = global_rows[
                global_rows[str(search_year)] > 0
            ].copy().head(300)

        candidates = enforce_brand_code_lock(candidates, state)
        candidates = enforce_family_lock(candidates, state)

        if len(candidates) > 1 and turn.get("descriptors"):
            candidates = refine_locally(
                candidates,
                " ".join(turn["descriptors"])
            )
            candidates = enforce_brand_code_lock(candidates, state)
            candidates = enforce_family_lock(candidates, state)

    candidates = candidates[
        candidates[str(search_year)] > 0
    ].copy()

    candidates = enforce_brand_code_lock(candidates, state)
    candidates = enforce_family_lock(candidates, state)

    if candidates.empty:
        state["candidate_keys"] = []

        known = (
            f"Yıl: {requested_year}; marka: {state.get('brand') or 'bilinmiyor'}; "
            f"model/tip: {state.get('model_or_type') or 'bilinmiyor'}."
        )

        reply = natural_conversation_reply(
            known,
            "Bu bilgilerle uygun kayıt bulunamadığını nazikçe söyle. Kullanıcının daha önce verdiği bilgileri tekrar isteme; eksik olabilecek motor, kasa veya donanım bilgisinden birini doğal biçimde iste.",
            fallback="Aracı tam eşleştiremedim. Motor, kasa veya donanım paketini biraz daha ayrıntılı söyler misiniz?"
        )

        return {
            "status": "need_info",
            "question": reply["reply"],
            "options": [],
            "state": state
        }

    unique_brands = candidates["Marka Adı"].drop_duplicates().tolist()
    if len(unique_brands) == 1:
        state["brand"] = str(unique_brands[0])
        state = establish_brand_code_lock(state)

    state["candidate_keys"] = candidates["_row_key"].tolist()

    if len(candidates) == 1:
        row = candidates.iloc[0]
        state["candidate_keys"] = []

        result = row_to_result(
            row,
            search_year,
            requested_year=requested_year
        )
        result["state"] = state
        return result

    q = make_safe_question(candidates, search_year, state)

    return {
        "status": "clarify",
        "question": q["question"],
        "options": q.get("options", [])[:5],
        "candidate_count": int(len(candidates)),
        "state": state
    }


@app.route("/robots.txt")
def robots_txt():
    content = """User-agent: *
Allow: /
Sitemap: https://kasko-ai.vercel.app/sitemap.xml
"""
    return Response(content, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    content = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://kasko-ai.vercel.app/</loc>
    <changefreq>monthly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://kasko-ai.vercel.app/satis</loc>
    <changefreq>yearly</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://kasko-ai.vercel.app/kasko</loc>
    <changefreq>monthly</changefreq>
    <priority>0.9</priority>
  </url>
</urlset>
"""
    return Response(content, mimetype="application/xml")


@app.route("/")
def index():
    return render_template("home.html")


@app.route("/kasko")
def kasko_page():
    return render_template("kasko.html")


@app.route("/satis")
def satis_page():
    return render_template("satis.html")



@app.post("/api/hesapla")
def hesapla_satis_ucreti():
    payload = request.get_json(silent=True) or request.form or {}

    try:
        satis_bedeli = float(payload.get("satis_bedeli", 0) or 0)
        kasko_bedeli = float(payload.get("kasko_bedeli", 0) or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Geçersiz tutar bilgisi."}), 400

    raw_yetki = payload.get("yetki_belgesi", False)
    yetki_belgesi = (
        raw_yetki is True
        or str(raw_yetki).strip().lower() in {"true", "1", "on", "yes", "evet"}
    )

    if satis_bedeli <= 0 or kasko_bedeli <= 0:
        return jsonify({
            "ok": False,
            "error": "Satış bedeli ve kasko bedeli sıfırdan büyük olmalıdır."
        }), 400

    matrah = max(satis_bedeli, kasko_bedeli)
    if satis_bedeli > kasko_bedeli:
        matrah_kaynagi = "Satış Bedeli"
    elif kasko_bedeli > satis_bedeli:
        matrah_kaynagi = "Kasko Bedeli"
    else:
        matrah_kaynagi = "Satış / Kasko Bedeli"

    if yetki_belgesi:
        harc = 0.0
    else:
        harc = max(FEE_CONFIG["min_harc"], matrah * FEE_CONFIG["harc_rate"])

    noter_ucreti = FEE_CONFIG["noter_ucreti"]
    artes_tescil = FEE_CONFIG["artes_tescil"]
    kdv = (noter_ucreti + artes_tescil) * FEE_CONFIG["kdv_rate"]
    darphane = FEE_CONFIG["darphane"]
    tescil = FEE_CONFIG["tescil"]

    genel_toplam = harc + noter_ucreti + artes_tescil + kdv + darphane + tescil

    return jsonify({
        "ok": True,
        "tarife_yili": FEE_CONFIG["year"],
        "matrah": round(matrah, 2),
        "matrah_kaynagi": matrah_kaynagi,
        "harc": round(harc, 2),
        "noter_ucreti": round(noter_ucreti, 2),
        "artes_tescil": round(artes_tescil, 2),
        "kdv": round(kdv, 2),
        "darphane": round(darphane, 2),
        "tescil": round(tescil, 2),
        "genel_toplam": round(genel_toplam, 2),
    })


@app.get("/api/status")
def status():
    return jsonify({
        "ok": True,
        "csv": ACTIVE_CSV,
        "rows": int(len(df)),
        "gemini_configured": bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")),
        "model": MODEL,
        "version": APP_VERSION,
        "tarife_yili": FEE_CONFIG["year"]
    })


@app.post("/api/search")
def search():
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message", "")).strip()
    state = payload.get("state") or {}

    if not message:
        return jsonify({"ok": False, "error": "Lütfen bir araç bilgisi yazın."}), 400

    try:
        result = process_search(message, state)
        return jsonify({"ok": True, **result})
    except Exception as exc:
        # API anahtarı, kota, Python exception vb. teknik ayrıntıları kullanıcıya gösterme.
        text = str(exc).upper()
        if "AI_QUOTA" in text or "429" in text or "RESOURCE_EXHAUSTED" in text:
            return jsonify({
                "ok": True,
                "status": "need_info",
                "question": "Şu anda yapay zekâ hizmeti kısa süreli yoğunluk yaşıyor. Araç bilgilerinizi biraz daha ayrıntılı yazarak tekrar deneyebilirsiniz.",
                "options": [],
                "state": state
            })
        return jsonify({
            "ok": True,
            "status": "need_info",
            "question": "Aracı tam eşleştiremedim. Marka, model, model yılı, motor veya donanım bilgilerinden bildiklerinizi biraz daha ayrıntılı yazar mısınız?",
            "options": [],
            "state": state
        })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
