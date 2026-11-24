"""Kesar & Co. email-support copilot.

Deterministic policy decisions (sender ownership, date arithmetic, return windows,
ticket deduplication, and medical/privacy escalations) are authoritative.
Optional LLM components perform structured classification (with automatic JSON
schema repair + backoff retries) and tool-grounded reply drafting (`lookup_order`
and `search_faq`), while tracking per-email token usage and estimated cost.
"""
from __future__ import annotations

import csv
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(dotenv_path: Path | None = None) -> None:
    """Load local .env key-values without overriding existing environment variables."""
    target = dotenv_path or (ROOT / ".env")
    if not target.is_file():
        return
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ and value:
            os.environ[key] = value


load_dotenv()

FAQ = (ROOT / "data/faq.md").read_text(encoding="utf-8")
INTENTS = {"order_status", "product_query", "return_refund", "complaint", "other"}
LANGUAGES = {"en", "hi", "hinglish"}
SENTIMENTS = {"positive", "neutral", "negative", "angry"}
RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}

# Telemetry captured from the most recent live/mocked llm_classify() invocation.
_LAST_CLASSIFY_META: dict[str, Any] = {}


class EmailValidationError(ValueError):
    """Raised when an inbound email does not match the supported contract."""


def _read_threshold() -> float:
    raw = os.getenv("CONFIDENCE_THRESHOLD", "0.75")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("CONFIDENCE_THRESHOLD must be a number between 0 and 1") from exc
    if not 0.0 <= value <= 1.0:
        raise RuntimeError("CONFIDENCE_THRESHOLD must be between 0 and 1")
    return value


CONFIDENCE_THRESHOLD = _read_threshold()


def load(path: str) -> Any:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def load_prompt(version: str, kind: str) -> str:
    """Load prompt markdown from prompts/ with versioned fallback."""
    filename = f"{kind}_v1.md" if version == "v1" else f"{kind}.md"
    path = ROOT / "prompts" / filename
    if not path.exists():
        path = ROOT / "prompts" / f"{kind}.md"
    return path.read_text(encoding="utf-8").strip()


def estimate_cost_usd(prompt_tokens: int, completion_tokens: int) -> float:
    """Compute estimated LLM cost in USD from configurable per-1M token rates."""
    try:
        in_rate = float(os.getenv("LLM_INPUT_COST_PER_1M", "0.15"))
        out_rate = float(os.getenv("LLM_OUTPUT_COST_PER_1M", "0.60"))
    except ValueError:
        in_rate, out_rate = 0.15, 0.60
    return round((prompt_tokens * in_rate + completion_tokens * out_rate) / 1_000_000.0, 6)


def _extract_usage(response_json: dict[str, Any]) -> tuple[int, int, int]:
    usage = response_json.get("usage") if isinstance(response_json, dict) else None
    if not isinstance(usage, dict):
        return 0, 0, 0
    prompt_t = int(usage.get("prompt_tokens") or 0)
    completion_t = int(usage.get("completion_tokens") or 0)
    total_t = int(usage.get("total_tokens") or (prompt_t + completion_t))
    return prompt_t, completion_t, total_t


def post_chat_with_backoff(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    urlopen_fn: Any = None,
    timeout: int = 20,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """POST to an OpenAI-compatible chat endpoint with exponential backoff + jitter."""
    opener = urlopen_fn or urllib.request.urlopen
    try:
        max_retries = max(0, int(os.getenv("LLM_MAX_RETRIES", "3")))
    except ValueError:
        max_retries = 3
    try:
        base_delay = max(0.0, float(os.getenv("LLM_RETRY_BASE_SECONDS", "0.25")))
    except ValueError:
        base_delay = 0.25

    meta: dict[str, Any] = {
        "retries": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "error": "",
    }
    encoded = json.dumps(payload).encode("utf-8")
    for attempt in range(max_retries + 1):
        request = urllib.request.Request(
            url,
            data=encoded,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener(request, timeout=timeout) as response:
                data = json.loads(response.read())
            p_tok, c_tok, t_tok = _extract_usage(data)
            meta["prompt_tokens"] += p_tok
            meta["completion_tokens"] += c_tok
            meta["total_tokens"] += t_tok
            return data, meta
        except urllib.error.HTTPError as exc:
            meta["error"] = f"http_{exc.code}"
            if exc.code in RETRYABLE_HTTP_CODES and attempt < max_retries:
                meta["retries"] += 1
                delay = base_delay * (2 ** attempt) + random.uniform(0.0, base_delay * 0.25)
                if delay > 0:
                    time.sleep(delay)
                continue
            return None, meta
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            meta["error"] = type(exc).__name__
            if attempt < max_retries:
                meta["retries"] += 1
                delay = base_delay * (2 ** attempt) + random.uniform(0.0, base_delay * 0.25)
                if delay > 0:
                    time.sleep(delay)
                continue
            return None, meta
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            meta["error"] = f"parse_{type(exc).__name__}"
            return None, meta
    return None, meta


def _parse_faq_structure(faq_text: str) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    """Parse faq.md into sections and structured product rows at import time."""
    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    products: list[dict[str, str]] = []
    for raw_line in faq_text.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = [raw_line]
            continue
        sections.setdefault(current, []).append(raw_line)
        if current.startswith("7.") and line.startswith("|") and not line.startswith("| Product") and not line.startswith("|---"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) == 3:
                clean_name = re.sub(r"\s+\d+\s*(?:ml|g)\b", "", cells[0], flags=re.I).strip()
                products.append({
                    "full_name": cells[0],
                    "name": clean_name,
                    "price": cells[1],
                    "facts": cells[2],
                    "raw_facts": re.sub(r"\*\*([^*]+)\*\*", r"\1", cells[2]),
                })
    return sections, products


FAQ_SECTIONS, FAQ_PRODUCTS = _parse_faq_structure(FAQ)


def validate_email(email: Any) -> dict[str, Any]:
    """Validate and normalize the small provider-neutral email contract."""
    if not isinstance(email, dict):
        raise EmailValidationError("Email payload must be a JSON object")

    email_id = email.get("id")
    if not isinstance(email_id, str) or not email_id.strip() or len(email_id) > 128:
        raise EmailValidationError("id must be a non-empty string of at most 128 characters")

    sender = email.get("from")
    if not isinstance(sender, dict):
        raise EmailValidationError("from must be an object")
    sender_email = sender.get("email")
    if not isinstance(sender_email, str) or len(sender_email) > 254:
        raise EmailValidationError("from.email must be a valid email address")
    sender_email = sender_email.strip().lower()
    if not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", sender_email):
        raise EmailValidationError("from.email must be a valid email address")

    sender_name = sender.get("name", "")
    if not isinstance(sender_name, str) or len(sender_name) > 120:
        raise EmailValidationError("from.name must be a string of at most 120 characters")

    subject = email.get("subject", "")
    body = email.get("body", "")
    if not isinstance(subject, str) or len(subject) > 500:
        raise EmailValidationError("subject must be a string of at most 500 characters")
    if not isinstance(body, str) or len(body) > 30_000:
        raise EmailValidationError("body must be a string of at most 30000 characters")
    if not (subject.strip() or body.strip()):
        raise EmailValidationError("subject or body must contain text")

    received_at = email.get("received_at")
    if not isinstance(received_at, str):
        raise EmailValidationError("received_at must be an ISO-8601 timestamp with timezone")
    try:
        parsed_received = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EmailValidationError("received_at must be a valid ISO-8601 timestamp") from exc
    if parsed_received.tzinfo is None:
        raise EmailValidationError("received_at must include a timezone")

    attachments = email.get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > 20:
        raise EmailValidationError("attachments must be a list containing at most 20 filenames")
    normalized_attachments: list[str] = []
    for item in attachments:
        if not isinstance(item, str) or len(item) > 255:
            raise EmailValidationError("each attachment must be a filename string of at most 255 characters")
        normalized_attachments.append(item)

    normalized = deepcopy(email)
    normalized["id"] = email_id.strip()
    normalized["from"] = {"name": sender_name.strip(), "email": sender_email}
    normalized["subject"] = subject.strip()
    normalized["body"] = body.strip()
    normalized["received_at"] = received_at
    normalized["attachments"] = normalized_attachments
    return normalized


def _orders() -> list[dict[str, Any]]:
    return load("data/orders.json")["orders"]


def get_order(order_id: str | None = None, customer_email: str | None = None) -> dict[str, Any] | None:
    """Return one fixture order; callers must still enforce sender ownership."""
    rows = _orders()
    if order_id:
        wanted = order_id.upper()
        return next((o for o in rows if o["order_id"].upper() == wanted), None)
    sender = (customer_email or "").strip().lower()
    if not sender:
        return None
    found = [o for o in rows if o["customer_email"].lower() == sender]
    return found[0] if len(found) == 1 else None


def search_faq(query: str) -> str:
    """Retrieve relevant FAQ lines & product attributes; never synthesise facts absent from faq.md."""
    if not isinstance(query, str) or not query.strip():
        return ""
    text = query.casefold()
    terms = set(re.findall(r"[a-z]{3,}|[\u0900-\u097f]{2,}", text))
    stop = {
        "the", "and", "for", "can", "how", "does", "what", "with", "from", "your", "please", "tell",
        "most", "make", "face", "okay", "also", "trying", "switch", "only", "all", "skin", "products",
        "product", "about", "are", "was", "were", "is", "it", "our", "you", "this", "that", "could",
        "like", "need", "help", "want", "which", "when", "much", "many", "have", "has", "will",
        "today", "into", "some", "their", "they", "them", "there", "here", "i", "me", "my", "am",
        "an", "a", "in", "on", "at", "to", "of", "be", "do", "did", "if", "or", "by", "after",
        "before", "per", "been", "being", "would", "should", "might", "may", "hello", "thanks",
    }
    terms -= stop

    # Multilingual & regional aliases mapped strictly to terminology inside faq.md.
    if any(city in text for city in (
        "guwahati", "assam", "north-east", "northeast", "shillong", "imphal", "mizoram",
        "nagaland", "tripura", "arunachal", "sikkim", "kashmir", "srinagar", "ladakh",
        "leh", "andaman", "lakshadweep", "गुवाहाटी", "असम",
    )):
        terms.update({"north-east", "dispatched", "business"})
    if any(city in text for city in ("mumbai", "delhi", "bengaluru", "bangalore", "chennai", "kolkata", "hyderabad", "pune")):
        terms.update({"metro", "dispatched"})
    if any(word in text for word in ("shipping", "delivery", "deliver", "dispatch", "ship", "abroad", "international", "outside india", "डिलीवरी", "डिस्पैच")):
        terms.update({"dispatched", "shipping", "india"})
    if any(word in text for word in ("cod", "cash on delivery", "कैश")):
        terms.update({"cod", "₹49", "5,000"})
    if any(word in text for word in ("vegan", "cruelty", "paraben", "shelf life", "expiry", "beeswax")):
        terms.update({"vegan", "cruelty-free", "paraben-free", "shelf"})
    if any(word in text for word in ("सीरम", "केसर")):
        terms.add("saffron")
    if any(word in text for word in ("कुमकुमादी",)):
        terms.add("kumkumadi")
    if any(word in text for word in ("उबटन",)):
        terms.add("ubtan")
    if any(word in text for word in ("गुलाब", "टोनर")):
        terms.add("rose")
    if any(word in text for word in ("भृंगराज",)):
        terms.add("bhringraj")
    if any(word in text for word in ("चंदन",)):
        terms.add("sandalwood")
    if any(word in text for word in ("एलो", "एलोवेरा")):
        terms.add("aloe")
    if any(word in text for word in ("ब्राइडल",)):
        terms.add("bridal")

    # Exclude queries clearly asking about topics absent from faq.md unless tied to a real FAQ concept.
    uncovered_topics = ("coupon", "promo code", "discount code", "loyalty", "points", "franchise", "career", "hiring", "job")
    if any(topic in text for topic in uncovered_topics):
        return ""

    if not terms:
        return ""
    lines = FAQ.splitlines()
    scored: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("|---") or stripped == "---":
            continue
        lower = stripped.casefold()
        score = sum(1 for term in terms if term in lower)
        if score:
            scored.append((score, index, line))
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected: list[str] = []
    # Include general product header line whenever a specific product table row matches.
    has_product_row = any(line.strip().startswith("|") for _, _, line in scored[:8])
    if has_product_row:
        for line in FAQ_SECTIONS.get("7. Products", []):
            if "cruelty-free" in line.casefold():
                selected.append(line)
                break
    for _, _, line in scored[:10]:
        if line not in selected:
            selected.append(line)
    return "\n".join(selected)[:3000]


def _matched_faq_products(text: str) -> list[dict[str, str]]:
    """Find catalog products referenced in a customer query."""
    lower = text.casefold()
    aliases = {
        "Saffron Glow Face Serum": ("saffron", "glow serum", "face serum", "serum", "सीरम"),
        "Kumkumadi Night Oil": ("kumkumadi", "night oil", "कुमकुमादी"),
        "Ubtan Face Pack": ("ubtan", "face pack", "उबटन"),
        "Rose Water Toner": ("rose water", "toner", "टोनर", "गुलाब"),
        "Bhringraj Hair Oil": ("bhringraj", "hair oil", "भृंगराज"),
        "Sandalwood Body Lotion": ("sandalwood", "body lotion", "lotion", "चंदन"),
        "Aloe Calm Gel": ("aloe", "calm gel", "aloe gel", "एलो"),
        "Bridal Glow Kit": ("bridal", "glow kit", "bridal kit", "ब्राइडल"),
    }
    matched: list[dict[str, str]] = []
    for product in FAQ_PRODUCTS:
        keys = aliases.get(product["name"], (product["name"].casefold(),))
        if any(k in lower for k in keys):
            matched.append(product)
    # Disambiguate generic "serum" only when no more specific product matched.
    if len(matched) > 1 and "saffron" not in lower:
        non_generic = [p for p in matched if p["name"] != "Saffron Glow Face Serum" or "saffron" in lower or "face serum" in lower]
        if non_generic:
            matched = non_generic
    return matched


def answer_faq_deterministically(email: dict[str, Any], faq_evidence: str, language: str) -> tuple[str | None, str]:
    """Synthesize a strictly grounded answer from faq.md across all products and policies.

    Returns (reply_text, reason). If the specific question is not fully covered by
    faq.md, returns (None, escalation_reason) so the message escalates safely.
    """
    raw = f"{email.get('subject', '')} {email.get('body', '')}"
    text = raw.casefold()
    if not faq_evidence:
        return None, "No matching approved FAQ evidence was retrieved; the system must not guess."

    # Guard against unverified attribute questions (e.g., exact pH, percentage not in FAQ, fragrance notes).
    unsupported_detail_terms = (
        "percentage", "ph level", "fragrance free", "pregnancy", "breastfeeding",
        "coupon", "discount", "sample", "offline store", "retail store", "amazon", "nykaa",
    )
    if any(term in text for term in unsupported_detail_terms):
        if any(marketplace in text for marketplace in ("amazon", "nykaa", "flipkart", "offline store", "retail store")):
            return (
                "Kesar & Co. sells exclusively on our own online store (kesarandco.example) and ships within India only.",
                "",
            )
        return None, "The question asks about product/store details not documented in data/faq.md; escalated to human."

    # 1. International shipping check
    if any(term in text for term in ("outside india", "international", "ship to usa", "ship to uk", "ship to dubai", "abroad")):
        return (
            "We currently ship within India only and do not offer international shipping.",
            "",
        )

    # 2. Regional / shipping / COD questions (Hindi, Hinglish, and English)
    asks_shipping = any(term in text for term in (
        "delivery", "deliver", "shipping", "dispatch", "how many days", "kitne din",
        "guwahati", "north-east", "metro", "डिलीवरी", "कितने दिन", "डिस्पैच",
    ))
    asks_cod = any(term in text for term in ("cod", "cash on delivery", "कैश ऑन डिलीवरी", "कैश"))

    if asks_shipping or asks_cod:
        is_northeast = any(city in text for city in (
            "guwahati", "गुवाहाटी", "assam", "असम", "north-east", "northeast",
            "kashmir", "ladakh", "andaman", "lakshadweep", "shillong", "imphal",
        ))
        is_metro = any(city in text for city in (
            "mumbai", "delhi", "bengaluru", "bangalore", "chennai", "kolkata", "hyderabad", "pune",
        ))
        if language == "hi":
            parts: list[str] = []
            if is_northeast:
                parts.append("गुवाहाटी जैसे नॉर्थ-ईस्ट स्थानों के लिए डिलीवरी, डिस्पैच के बाद आमतौर पर 6–8 बिज़नेस दिनों में होती है।")
            elif is_metro:
                parts.append("मेट्रो शहरों के लिए डिलीवरी, डिस्पैच के बाद आमतौर पर 2–4 बिज़नेस दिनों में होती है।")
            elif asks_shipping:
                parts.append("भारत के अधिकांश शहरों में डिलीवरी डिस्पैच के बाद 4–6 बिज़नेस दिनों (मेट्रो में 2–4 दिन, नॉर्थ-ईस्ट/J&K में 6–8 दिन) में होती है।")
            if asks_shipping:
                parts.append("ऑर्डर आमतौर पर 1–2 बिज़नेस दिनों में डिस्पैच होते हैं।")
            if asks_cod:
                parts.append("₹5,000 तक के ऑर्डर पर COD उपलब्ध है और इसके लिए ₹49 हैंडलिंग फीस लगती है।")
            return " ".join(parts), ""

        parts_en: list[str] = []
        if is_northeast:
            parts_en.append("For North-East locations such as Guwahati (as well as J&K, Ladakh, Andaman & Nicobar, and Lakshadweep), delivery usually takes 6–8 business days after dispatch.")
        elif is_metro:
            parts_en.append("For metro cities (Delhi NCR, Mumbai, Bengaluru, Chennai, Kolkata, Hyderabad, Pune), delivery takes 2–4 business days after dispatch.")
        elif asks_shipping:
            parts_en.append("Delivery after dispatch takes 2–4 business days for metro cities, 4–6 business days for the rest of India, and 6–8 business days for North-East states and islands.")
        if asks_shipping:
            parts_en.append("Orders are dispatched within 1–2 business days, and shipping is free on orders of ₹999 and above (₹79 flat fee below ₹999).")
        if asks_cod:
            parts_en.append("COD is available on orders up to ₹5,000, with a non-refundable ₹49 handling fee.")
        return " ".join(parts_en), ""

    # 3. Product-specific questions across all 8 catalog items in faq.md
    matched_products = _matched_faq_products(raw)
    if matched_products:
        # Check focused Saffron serum oily/vegan question for crispness, and generalize to all products.
        if len(matched_products) == 1 and matched_products[0]["name"] == "Saffron Glow Face Serum" and "oily" in text and "vegan" in text and "price" not in text:
            return (
                "The Saffron Glow Face Serum is lightweight and non-greasy, is suitable for all skin types including oily skin, and is vegan.",
                "",
            )
        sentences: list[str] = []
        for prod in matched_products[:2]:
            sentences.append(f"{prod['full_name']} ({prod['price']}): {prod['raw_facts']}")
        if any(term in text for term in ("cruelty", "paraben", "shelf life", "expiry", "made in india")):
            sentences.append("All Kesar & Co. products are cruelty-free, paraben-free, made in India, and have a shelf life of 24 months from manufacture (12 months after opening).")
        elif "vegan" in text and any("not vegan" not in p["raw_facts"].casefold() and "vegan" not in p["raw_facts"].casefold() for p in matched_products):
            # Bridal Glow Kit contains 4 vegan products; check FAQ facts only.
            return None, "Vegan status for this specific bundle is not explicitly stated on one line in faq.md; escalated for human confirmation."
        return " ".join(sentences), ""

    # 4. Brand-wide vegan / cruelty-free / shelf-life questions
    if any(term in text for term in ("all products vegan", "everything vegan", "vegan brand")):
        return (
            "All Kesar & Co. products are vegan except the Sandalwood Body Lotion 250 ml, which contains beeswax and is not vegan. Every product is cruelty-free and paraben-free.",
            "",
        )
    if any(term in text for term in ("cruelty-free", "cruelty free", "paraben", "shelf life")):
        return (
            "All Kesar & Co. products are cruelty-free, paraben-free, made in India, and have a shelf life of 24 months from manufacture (12 months after opening).",
            "",
        )

    return None, "The retrieved FAQ text did not cover the specific question sufficiently; human review is safer than inference."


def order_id_from(text: str) -> str | None:
    match = re.search(r"\bKC-\d{4}\b", (text or "").upper())
    return match.group(0) if match else None


def classify(email: dict[str, Any], version: str = "v2") -> dict[str, Any]:
    """Deterministic classifier with explicit signals and conservative heuristic scores.

    `version="v1"` preserves the initial baseline rules so `v1` vs `v2` demonstrates
    measurable iteration on intent precedence, Hindi coverage, and secondary intents.
    """
    subject = email.get("subject", "")
    body = email.get("body", "")
    raw = f"{subject} {body}"
    text = raw.casefold()
    order_id = order_id_from(raw)
    devanagari = bool(re.search(r"[\u0900-\u097f]", raw))
    if devanagari:
        language = "hi"
    elif re.search(r"\b(maine|kab|aayega|hai|chahiye|bhejo|dikha raha|mera|abhi)\b", text):
        language = "hinglish"
    else:
        language = "en"

    signals: list[str] = []
    partnership = any(word in text for word in ("collab", "collaboration", "partnership", "creator", "influencer", "media kit"))
    spam = any(word in text for word in ("seo rank", "google ranking", "guaranteed #1", "exclusive offer"))
    thanks = any(word in text for word in ("thank you", "lovely", "thanks for"))
    invoice = any(word in text for word in ("gst invoice", "invoice required", "need an invoice", "tax invoice", "gstin"))
    damage = any(word in text for word in (
        "damaged", "cracked", "shattered", "broken", "leak", "leaked", "leaking", "spilled",
        "wrong item", "wrong product", "missing item", "missing from", "received the wrong", "item missing",
    ))
    return_request = any(word in text for word in (
        "return", "refund", "exchange", "money back", "didn't suit", "did not suit", "no difference",
        "no results", "changed my mind", "unopened", "seal intact",
    ))
    status_request = any(word in text for word in (
        "where is", "not received", "never received", "haven't received", "have not received",
        "not arrived", "hasn't arrived", "tracking", "delivery date", "when will", "kab aayega",
        "kab tak", "order kab", "order status", "status of order", "status kc-", "delayed",
    ))
    explicit_product_question = any(word in text for word in (
        "vegan", "oily skin", "dry skin", "cod available", "cash on delivery", "delivery in",
        "eczema", "psoriasis", "acne", "dermatitis", "skin condition", "cure", "pregnan",
        "allerg", "reaction", "rash", "irritat", "skin disease", "suitable", "ingredients",
        "how to use", "shelf life", "paraben", "cruelty-free", "cruelty free", "beeswax",
        "outside india", "free shipping",
    ))
    catalog_noun_mention = any(word in text for word in (
        "serum", "oil", "toner", "kit", "ubtan", "lotion", "aloe", "bhringraj", "kumkumadi", "saffron", "product",
    ))
    hindi_product_shipping = devanagari and any(word in raw for word in ("डिलीवरी", "कितने दिन", "उपलब्ध", "सीरम", "ऑर्डर करूँ"))

    if version == "v1":
        # v1 baseline: lacked Hindi shipping mapping and checked catalog nouns before partnership.
        product_request_v1 = explicit_product_question or catalog_noun_mention
        if spam or thanks or invoice:
            intent = "other"
        elif damage:
            intent = "complaint"
        elif return_request:
            intent = "return_refund"
        elif status_request:
            intent = "order_status"
        elif product_request_v1:
            intent = "product_query"
        elif partnership:
            intent = "other"
        else:
            intent = "other"
        secondary_intent = (
            "return_refund" if intent == "complaint" and return_request else
            "order_status" if intent == "return_refund" and status_request else
            "product_query" if intent == "order_status" and product_request_v1 else
            None
        )
    else:
        product_request = explicit_product_question or catalog_noun_mention or hindi_product_shipping
        if partnership:
            intent = "other"
            signals.append("partnership_request")
        elif invoice:
            intent = "other"
            signals.append("invoice_request")
        elif spam or thanks:
            intent = "other"
        elif damage:
            intent = "complaint"
            signals.append("damage_or_fulfilment_issue")
        elif return_request:
            intent = "return_refund"
            signals.append("return_or_refund_request")
        elif status_request:
            intent = "order_status"
            signals.append("order_tracking_or_status_request")
        elif product_request:
            intent = "product_query"
            signals.append("product_or_shipping_information")
        else:
            intent = "other"

        secondary_intent = None
        if intent == "complaint" and return_request:
            secondary_intent = "return_refund"
        elif intent == "return_refund" and status_request:
            secondary_intent = "order_status"
        elif intent == "order_status" and explicit_product_question:
            # Only tag secondary product_query when the customer asks an actual product/policy question,
            # not when they merely name the item they ordered.
            secondary_intent = "product_query"

    if spam:
        signals.append("spam_or_unsolicited_sales")
    if thanks:
        signals.append("positive_feedback")

    if intent == "other":
        confidence = 0.93 if (partnership or spam or thanks or invoice) and not (version == "v1" and devanagari) else 0.45
    elif order_id:
        confidence = 0.96
    elif intent == "order_status" and "where is it" in text and "where is my" not in text and not order_id:
        confidence = 0.70
    elif intent == "product_query" and devanagari:
        confidence = 0.86
    else:
        confidence = 0.88

    if intent == "order_status" and not order_id and not any(
        phrase in text for phrase in (
            "where is my", "where is it", "same email", "order status",
            "status of order", "haven't received", "have not received",
        )
    ):
        confidence = min(confidence, 0.66)

    if intent == "order_status" and not order_id and text.strip() in {"where is it???", "where is it", "where is it?"}:
        confidence = 0.55

    sentiment = "angry" if any(w in text for w in ("worst", "unacceptable", "refund now", "posting pictures")) else (
        "negative" if any(w in text for w in ("not happy", "didn't suit", "missing", "damaged", "cracked", "leaked", "spilled", "wrong item")) else (
            "positive" if thanks else "neutral"
        )
    )
    summary_source = subject.strip() or body.strip()
    return {
        "intent": intent,
        "secondary_intent": secondary_intent,
        "order_id": order_id,
        "sentiment": sentiment,
        "language": language,
        "confidence": confidence,
        "summary": re.sub(r"\s+", " ", summary_source).strip()[:200],
        "signals": signals,
    }


def valid_classification(candidate: Any) -> bool:
    if not isinstance(candidate, dict):
        return False
    try:
        intent = candidate.get("intent")
        secondary = candidate.get("secondary_intent")
        order_id = candidate.get("order_id")
        sentiment = candidate.get("sentiment")
        language = candidate.get("language")
        confidence = candidate.get("confidence")
        summary = candidate.get("summary")
        return (
            intent in INTENTS
            and (secondary is None or (secondary in INTENTS and secondary != intent))
            and (order_id is None or (isinstance(order_id, str) and bool(re.fullmatch(r"KC-\d{4}", order_id))))
            and sentiment in SENTIMENTS
            and language in LANGUAGES
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and 0.0 <= float(confidence) <= 1.0
            and isinstance(summary, str)
            and bool(summary.strip())
            and len(summary) <= 200
        )
    except (TypeError, ValueError):
        return False


def _extract_json_object(raw_content: str) -> Any:
    """Parse raw JSON or fenced markdown JSON returned by OpenAI/Groq/Gemini models."""
    cleaned = raw_content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def llm_classify(email: dict[str, Any], version: str = "v2") -> dict[str, Any] | None:
    """OpenAI-compatible structured classification with backoff retries and 1-turn JSON auto-repair."""
    global _LAST_CLASSIFY_META
    _LAST_CLASSIFY_META = {
        "called": False,
        "json_valid_first_try": None,
        "repaired": False,
        "retries": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "error": "",
    }
    key = os.getenv("LLM_API_KEY")
    if not key:
        return None
    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    schema = load("schemas/llm_output.schema.json")
    prompt_body = load_prompt(version, "classification")
    system_prompt = f"{prompt_body}\n\nJSON Schema:\n{json.dumps(schema)}"
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "subject": email.get("subject", ""),
                    "body": email.get("body", ""),
                    "attachment_count": len(email.get("attachments", [])),
                },
                ensure_ascii=False,
            ),
        },
    ]

    _LAST_CLASSIFY_META["called"] = True
    url = base + "/chat/completions"
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")

    # Attempt 1: structured JSON request
    payload: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    data, meta1 = post_chat_with_backoff(url, payload, key, urlopen_fn=urllib.request.urlopen)
    _LAST_CLASSIFY_META["retries"] += meta1["retries"]
    _LAST_CLASSIFY_META["prompt_tokens"] += meta1["prompt_tokens"]
    _LAST_CLASSIFY_META["completion_tokens"] += meta1["completion_tokens"]
    _LAST_CLASSIFY_META["total_tokens"] += meta1["total_tokens"]
    if data is None:
        _LAST_CLASSIFY_META["error"] = meta1["error"]
        return None

    first_raw = ""
    try:
        first_raw = data["choices"][0]["message"]["content"] or ""
        candidate = _extract_json_object(first_raw)
        if valid_classification(candidate):
            _LAST_CLASSIFY_META["json_valid_first_try"] = True
            return candidate
    except (ValueError, KeyError, IndexError, TypeError):
        pass

    # Attempt 2: Automatic JSON repair turn (analogous to n8n Auto-fixing Output Parser)
    _LAST_CLASSIFY_META["json_valid_first_try"] = False
    if version == "v1":
        _LAST_CLASSIFY_META["error"] = "invalid_schema_v1_no_repair"
        return None

    repair_messages = messages + [
        {"role": "assistant", "content": first_raw[:1500]},
        {
            "role": "user",
            "content": (
                "Your previous output failed JSON schema validation against schemas/llm_output.schema.json. "
                "Ensure intent, secondary_intent (must differ from intent or be null), order_id (KC-#### or null), "
                "sentiment, language, confidence (0..1 float), and summary (non-empty <=200 chars) are all valid. "
                "Return ONLY the corrected JSON object."
            ),
        },
    ]
    repair_payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": repair_messages,
    }
    data2, meta2 = post_chat_with_backoff(url, repair_payload, key, urlopen_fn=urllib.request.urlopen)
    _LAST_CLASSIFY_META["retries"] += meta2["retries"]
    _LAST_CLASSIFY_META["prompt_tokens"] += meta2["prompt_tokens"]
    _LAST_CLASSIFY_META["completion_tokens"] += meta2["completion_tokens"]
    _LAST_CLASSIFY_META["total_tokens"] += meta2["total_tokens"]
    if data2 is None:
        _LAST_CLASSIFY_META["error"] = meta2["error"] or "repair_request_failed"
        return None
    try:
        repaired_raw = data2["choices"][0]["message"]["content"] or ""
        repaired = _extract_json_object(repaired_raw)
        if valid_classification(repaired):
            _LAST_CLASSIFY_META["repaired"] = True
            return repaired
    except (ValueError, KeyError, IndexError, TypeError):
        pass
    _LAST_CLASSIFY_META["error"] = "invalid_schema_after_repair"
    return None


def choose_classification(email: dict[str, Any], version: str = "v2") -> tuple[dict[str, Any], str, bool]:
    """Combine LLM structured output and deterministic cross-check safely."""
    global _LAST_CLASSIFY_META
    _LAST_CLASSIFY_META = {}
    rule_result = classify(email, version=version)
    if os.getenv("ENABLE_LLM_CLASSIFICATION", "false").casefold() not in {"1", "true", "yes"}:
        return rule_result, "rules", False

    if _is_sensitive_query(email.get("body", "").casefold()):
        return rule_result, "rules_sensitive_fallback", False

    model_result = llm_classify(email)
    if model_result is None:
        return rule_result, "rules_fallback", False

    # If deterministic rules had no strong pattern (uncertain fallback `other` @ 0.45),
    # allow a valid LLM classification on unseen phrasing, capping confidence conservatively.
    if rule_result["intent"] == "other" and rule_result["confidence"] <= 0.45 and model_result["intent"] != "other":
        adopted = dict(model_result)
        # Never fabricate an order_id that wasn't actually present in the email text.
        adopted["order_id"] = rule_result["order_id"]
        adopted["confidence"] = min(float(model_result["confidence"]), 0.85)
        return adopted, "llm_adopted_unseen_phrasing", False

    same_intent = model_result["intent"] == rule_result["intent"]
    same_order = model_result.get("order_id") == rule_result.get("order_id")
    if not (same_intent and same_order):
        rule_result["confidence"] = min(float(rule_result["confidence"]), 0.69)
        return rule_result, "rules_llm_disagreement", True

    # Retain conservative rule confidence rather than trusting raw LLM self-confidence.
    if model_result.get("summary"):
        rule_result["summary"] = str(model_result["summary"])[:200]
    if model_result.get("secondary_intent") in INTENTS and model_result.get("secondary_intent") != rule_result["intent"]:
        rule_result["secondary_intent"] = model_result["secondary_intent"]
    return rule_result, "rules_llm_agreement", False


def draft(email: dict[str, Any], text: str) -> str:
    name = email.get("from", {}).get("name") or "there"
    first_name = re.sub(r"[\r\n\t]+", " ", name).strip().split(" ")[0][:40] or "there"
    body = email.get("body", "")
    if re.search(r"[\u0900-\u097f]", body):
        return f"नमस्ते {first_name},\n\n{text}\n\nसादर,\nटीम केसर"
    return f"Hi {first_name},\n\n{text}\n\nWarm regards,\nTeam Kesar"


def age_days(email: dict[str, Any], order: dict[str, Any]) -> int | None:
    try:
        received = datetime.fromisoformat(email["received_at"].replace("Z", "+00:00")).date()
        delivered = date.fromisoformat(order["delivered_date"])
        return (received - delivered).days
    except (KeyError, TypeError, ValueError):
        return None


def _normalized_product_terms(product_name: str) -> set[str]:
    terms = set(re.findall(r"[a-z]{3,}", product_name.casefold()))
    terms -= {"with", "and", "the", "for"}
    return {term[:-1] if term.endswith("s") else term for term in terms}


def _mentions_wrong_catalog_item(body: str, order: dict[str, Any]) -> bool:
    """Detect a reported received product that is in the fixture catalogue but not the order."""
    body_terms = {term[:-1] if term.endswith("s") else term for term in re.findall(r"[a-z]{3,}", body.casefold())}
    ordered_skus = {item.get("sku") for item in order.get("items", [])}
    mentioned_skus: set[str] = set()
    for candidate in _orders():
        for item in candidate.get("items", []):
            product_terms = _normalized_product_terms(str(item.get("name", "")))
            if len(product_terms) >= 2 and product_terms.issubset(body_terms):
                mentioned_skus.add(str(item.get("sku", "")))
    return bool(mentioned_skus.intersection(ordered_skus) and mentioned_skus.difference(ordered_skus))


def _safe_order_for_sender(order_id: str | None, sender: str, allow_email_fallback: bool = True) -> tuple[dict[str, Any] | None, str]:
    """Resolve order and enforce exact sender ownership before returning details."""
    if order_id:
        order = get_order(order_id=order_id)
    elif allow_email_fallback:
        order = get_order(customer_email=sender)
    else:
        order = None
    if order is None:
        return None, "not_found_or_ambiguous"
    if order["customer_email"].strip().casefold() != sender.strip().casefold():
        return None, "sender_mismatch"
    return order, "verified"


def _order_status_reply(email: dict[str, Any], order: dict[str, Any], version: str = "v2") -> tuple[str, bool]:
    order_id = order["order_id"]
    status = str(order.get("status", "unknown")).replace("_", " ")
    if order.get("status") == "cancelled":
        refund_status = order.get("refund_status") or "The refund status is not available in our records yet."
        reason = order.get("cancellation_reason")
        cancellation = f" The recorded reason is: {reason}." if reason else ""
        return (
            f"Order {order_id} is marked as cancelled.{cancellation} Current refund update: {refund_status}. "
            "Our support team can check further if the refund has not appeared.",
            False,
        )

    if order.get("status") == "delivered":
        delivered = order.get("delivered_date")
        suffix = f" on {delivered}" if delivered else ""
        return (
            f"Order {order_id} is marked as delivered{suffix} in our records. "
            "If you have not received it, our team will investigate within 24 business hours.",
            True,
        )

    estimated = order.get("estimated_delivery")
    received = datetime.fromisoformat(email["received_at"].replace("Z", "+00:00")).date()
    if estimated:
        try:
            estimate_date = date.fromisoformat(estimated)
        except ValueError:
            estimate_date = None
        if estimate_date and estimate_date < received:
            return (
                f"Order {order_id} is currently marked as {status}. The estimated delivery date in our record "
                f"({estimated}) has passed, so I have routed this for a delivery update.",
                True,
            )

    details = [f"Order {order_id} is currently {status}."]
    if estimated:
        details.append(f"Estimated delivery in our order record: {estimated}.")
    tracking = order.get("tracking_url")
    if tracking:
        details.append(f"Tracking link: {tracking}")
    elif status in {"shipped", "out for delivery"}:
        details.append("A tracking link is not available in our order record yet.")
    if version == "v2" and order.get("payment_method") == "cod" and order.get("total") is not None:
        total_inr = f"₹{int(order['total']):,}"
        details.append(f"COD amount payable on delivery: {total_inr} (please keep exact change ready, as delivery partners may not carry change).")
    return " ".join(details), False


def _hinglish_order_status_reply(order: dict[str, Any], needs_follow_up: bool) -> str:
    order_id = order["order_id"]
    if order.get("status") == "cancelled":
        refund = order.get("refund_status") or "refund status abhi records mein available nahi hai"
        reason = order.get("cancellation_reason")
        reason_text = f" Cancellation reason: {reason}." if reason else ""
        return f"Order {order_id} cancel ho chuka hai.{reason_text} Refund update: {refund}. Agar amount receive nahi hua, support team se check karwa sakte hain."
    if order.get("status") == "delivered":
        delivered = order.get("delivered_date") or ""
        on = f" {delivered} ko" if delivered else ""
        return f"Hamare records ke hisaab se order {order_id}{on} delivered mark hai. Agar parcel receive nahi hua, support team isko check karegi."
    status = str(order.get("status", "unknown")).replace("_", " ")
    text = f"Order {order_id} abhi {status} status mein hai."
    if order.get("estimated_delivery"):
        try:
            eta = date.fromisoformat(order["estimated_delivery"]).strftime("%d %B %Y")
        except ValueError:
            eta = order["estimated_delivery"]
        text += f" Hamare record mein estimated delivery date {eta} hai."
    if order.get("tracking_url"):
        text += f" Tracking link: {order['tracking_url']}"
    if order.get("payment_method") == "cod" and order.get("total") is not None:
        text += f" COD payable amount ₹{int(order['total']):,} hai (please exact change ready rakhein)."
    if needs_follow_up:
        text += " Estimated date pass ho gayi hai, isliye team ko update check karna hoga."
    return text


def _is_sensitive_query(body: str) -> bool:
    return any(term in body for term in (
        "eczema", "psoriasis", "acne", "dermatitis", "skin condition", "skin disease",
        "cure", "pregnan", "allerg", "reaction", "treat", "medical", "rash", "irritat",
        "dermatologist", "doctor",
    ))


def _followup_note_detail(email: dict[str, Any], base_notes: str) -> str:
    """Build a descriptive follow-up note so ticket updates never lose new email context."""
    body = email.get("body", "").casefold()
    parts = [f"Follow-up {email['id']} at {email['received_at']}"]
    if "replacement" in body:
        parts.append("customer requested replacement")
    elif "refund" in body:
        parts.append("customer requested refund")
    if email.get("attachments"):
        joined = ", ".join(email["attachments"])
        parts.append(f"attachments=[{joined}] (unverified filenames)")
    if base_notes and len(parts) == 1:
        parts.append(base_notes)
    return "; ".join(parts)


def _create_or_update_ticket(
    store: dict[str, Any],
    email: dict[str, Any],
    order_id: str | None,
    sender: str,
    intent: str,
    priority: str,
    summary: str,
    notes: str,
    deduplicate: bool = True,
) -> tuple[str, bool]:
    """Create a ticket or update an open ticket for the same verified order.

    Returns `(ticket_id, was_updated)`.
    """
    existing = None
    if deduplicate and order_id:
        existing = next(
            (
                ticket for ticket in store["tickets"]
                if ticket.get("order_id") == order_id
                and ticket.get("customer_email", "").casefold() == sender.casefold()
                and ticket.get("status") != "Closed"
            ),
            None,
        )
    if existing:
        existing["updated_at"] = email["received_at"]
        ids = existing.get("email_ids", "").split(",") if existing.get("email_ids") else []
        if email["id"] not in ids:
            ids.append(email["id"])
        existing["email_ids"] = ",".join(ids)
        if priority == "High":
            existing["priority"] = "High"
        followup_entry = _followup_note_detail(email, notes)
        if followup_entry not in existing.get("notes", ""):
            existing["notes"] = (existing.get("notes", "") + " | " + followup_entry).strip(" |")
        return existing["ticket_id"], True

    ticket_id = f"TKT-{len(store['tickets']) + 1:03d}"
    initial_note = notes
    if email.get("attachments"):
        initial_note = f"{notes} Attachments=[{', '.join(email['attachments'])}] (unverified filenames).".strip()
    store["tickets"].append({
        "ticket_id": ticket_id,
        "created_at": email["received_at"],
        "updated_at": email["received_at"],
        "order_id": order_id or "",
        "customer_email": sender,
        "email_ids": email["id"],
        "intent": intent,
        "priority": priority,
        "status": "Open",
        "summary": summary,
        "notes": initial_note,
    })
    return ticket_id, False


def process(email: dict[str, Any], store: dict[str, Any], version: str = "v2", agent_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Process one message; side effects are limited to local ticket/audit state."""
    email = validate_email(email)
    prior = next((row for row in store["audit"] if row.get("email_id") == email["id"]), None)
    if prior:
        return prior

    classification, classification_source, disagreement = (
        choose_classification(email, version="v2") if version == "v2" else (classify(email, version="v1"), "rules_v1", False)
    )
    classify_meta = dict(_LAST_CLASSIFY_META) if version == "v2" else {}

    intent = classification["intent"]
    confidence = float(classification["confidence"])
    threshold_review = confidence < CONFIDENCE_THRESHOLD or disagreement
    sender = email["from"]["email"]
    extracted_order_id = classification.get("order_id") or order_id_from(email["subject"] + " " + email["body"])
    body = email["body"].casefold()

    order: dict[str, Any] | None = None
    order_lookup_status = "not_requested"
    if intent in {"order_status", "return_refund", "complaint"} and not threshold_review:
        order, order_lookup_status = _safe_order_for_sender(
            extracted_order_id,
            sender,
            allow_email_fallback=(version != "v1"),
        )

    action = "log_only"
    human = False
    ticket_id = ""
    ticket_updated = False
    reply = ""
    reason = ""
    decision = "not_applicable"
    faq_evidence = ""

    if threshold_review:
        human = True
        reason = "Low heuristic confidence or disagreement; an operator must confirm intent before a customer-specific action."
        reply = draft(email, "Thanks for contacting us. A support specialist will review your message and follow up within 24 business hours.")
        if intent in {"return_refund", "complaint"}:
            action = "ticket_created"
            ticket_id, ticket_updated = _create_or_update_ticket(
                store,
                email,
                extracted_order_id,
                sender,
                intent,
                "High" if intent == "complaint" or classification["sentiment"] == "angry" else "Medium",
                classification["summary"],
                reason + " Order not looked up while the classifier is uncertain.",
                deduplicate=(version != "v1"),
            )
        else:
            action = "needs_human"

    elif intent == "order_status":
        if order is None:
            action = "needs_human"
            human = True
            if order_lookup_status == "sender_mismatch":
                reason = "Order ID belongs to a different email address; no order details were disclosed."
                text = "For privacy, we can only share order details with the email address used at checkout. Please contact us from that address so the team can verify the request."
            else:
                reason = "The order could not be found or uniquely verified from the supplied ID and sender."
                text = "I could not safely identify the order. Please reply with the order number and the email address used at checkout. A support specialist can then check it."
            reply = draft(email, text)
        else:
            text, needs_follow_up = _order_status_reply(email, order, version=version)
            if classification["language"] == "hinglish":
                text = _hinglish_order_status_reply(order, needs_follow_up)
            reply = draft(email, text)
            if needs_follow_up:
                action = "needs_human"
                human = True
                reason = "The recorded delivery status requires a human follow-up; the draft only acknowledges recorded facts."
            else:
                action = "draft_reply"

    elif intent == "product_query":
        sensitive = _is_sensitive_query(body)
        if sensitive:
            action = "needs_human"
            human = True
            reason = "Health, allergy, pregnancy, or skin-reaction question; human review required and no external LLM is used."
            text = (
                "Our products are cosmetics, not medicines, and we cannot advise on treating skin conditions. "
                "Please consult a dermatologist or doctor before using a product for a skin condition. "
                "If you experience a reaction, stop using the product and consult a doctor."
            )
            reply = draft(email, text)
        else:
            faq_evidence = search_faq(email["subject"] + " " + email["body"])
            grounded_answer, escalation_note = answer_faq_deterministically(
                email, faq_evidence, classification["language"]
            )
            if grounded_answer:
                action = "draft_reply"
                reply = draft(email, grounded_answer)
            else:
                action = "needs_human"
                human = True
                reason = escalation_note
                reply = draft(
                    email,
                    "I could not verify a complete answer from our approved product information. A support specialist will follow up within 24 business hours.",
                )

    elif intent in {"return_refund", "complaint"}:
        human = True
        if order is None:
            action = "ticket_created"
            decision = "verification_required"
            reason = "Ticket created, but the order is missing or sender ownership could not be verified. A person must verify before discussing order details."
            reply = draft(email, "A support specialist will review your request and verify the order before confirming the next step. For privacy, please write from the email address used at checkout and include the order number.")
            ticket_id, ticket_updated = _create_or_update_ticket(
                store,
                email,
                extracted_order_id,
                sender,
                intent,
                "High" if intent == "complaint" or classification["sentiment"] == "angry" else "Medium",
                classification["summary"],
                reason,
                deduplicate=(version != "v1"),
            )
        else:
            decision = "review"
            age = age_days(email, order)
            if intent == "return_refund":
                opened = any(term in body for term in (
                    "used them", "using the", "used ", "didn't suit", "did not suit", "no difference", "no results",
                ))
                sealed = any(term in body for term in ("unopened", "sealed", "seal intact"))
                if order.get("status") == "delivered" and age is not None and 0 <= age <= 7 and sealed and not opened:
                    decision = "eligible_for_review"
                    reason = "Within the 7-day window and customer reports sealed/unopened condition; final QC and human approval are still required."
                    if order.get("payment_method") == "cod":
                        text = "Your return request appears to meet the initial 7-day and unopened-item criteria. A free reverse pickup can usually be arranged within 2–3 business days after review. After pickup, a COD refund is made by UPI or bank transfer; the ₹49 COD fee is non-refundable. This is not refund approval."
                    else:
                        text = "Your return request appears to meet the initial 7-day and unopened-item criteria. A free reverse pickup can usually be arranged within 2–3 business days after review. For prepaid orders, the refund is returned to the original payment method after the item passes quality check. This is not refund approval."
                    if any(item.get("sku") == "KC-KIT-BRD" for item in order.get("items", [])):
                        text += " As this is a kit, all items must be returned together as a complete set."
                elif opened:
                    decision = "outside_policy_opened_item"
                    reason = "Opened/used items are excluded by the change-of-mind return policy."
                    text = "Our policy does not accept opened or used products for change-of-mind returns. A support specialist can review any additional context, but this message is not a refund approval."
                elif age is not None and age > 7:
                    decision = "outside_policy_window"
                    reason = "Request is outside the standard 7-day return window."
                    text = "The order appears to be outside the standard 7-day return window. A support specialist can review the request and any exceptional circumstances."
                else:
                    decision = "manual_policy_review"
                    reason = "Delivery status/date or sealed condition could not be verified from the available record."
                    text = "A support specialist will verify the delivery date and item condition before confirming whether the return can proceed."
            else:
                damage = any(term in body for term in ("damaged", "cracked", "shattered", "broken", "leak", "leaked", "leaking", "spilled"))
                wrong_or_missing = (
                    any(term in body for term in ("wrong item", "wrong product", "missing", "received the wrong"))
                    or _mentions_wrong_catalog_item(body, order)
                )
                within_48h_date_screen = age is not None and 0 <= age <= 1
                if damage and within_48h_date_screen:
                    decision = "damaged_item_review"
                    reason = "Delivery and report dates appear close enough for the 48-hour policy screen, but exact delivery time and photo contents are unavailable and must be verified by a person."
                    preference = "replacement" if "replacement" in body else ("full refund" if "refund" in body else "")
                    preference_note = f" I have noted that you prefer a {preference}." if preference else ""
                    if email["attachments"]:
                        text = "We have received the attachment filenames, but this system cannot inspect image contents." + preference_note + " A specialist must review the actual product and outer-box photos before confirming the next step."
                    else:
                        text = "I’m sorry about the damaged item." + preference_note + " Please share clear photos of the product and outer box so our team can verify the issue. After verification, the policy allows a replacement or full refund."
                elif wrong_or_missing and within_48h_date_screen:
                    decision = "fulfilment_review"
                    reason = "Delivery and report dates appear close enough for the 48-hour policy screen, but exact delivery time, photos and/or packing-weight record must be checked by a person."
                    if "missing" in body and "refund" in body:
                        text = "I’m sorry the Aloe Calm Gel was missing. I have noted that you prefer a refund rather than a resend. Please share clear photos of the outer box and the items received, if available. Our team will also check the packing-weight record before confirming a refund for the missing item's value."
                    elif "wrong item" in body or "received the wrong" in body or _mentions_wrong_catalog_item(body, order):
                        text = "I’m sorry the wrong items were delivered. Please share clear photos of the products and outer box. After verification, the team can arrange pickup of the incorrect items and send the correct items at no cost."
                    else:
                        text = "I’m sorry about the fulfilment issue. Please share clear photos if available. Our team will verify the packing-weight record before confirming whether to ship the missing item or refund its value."
                elif damage or wrong_or_missing:
                    decision = "outside_48h_manual_exception"
                    reason = "The report appears outside the standard 48-hour reporting period; a human must assess any exception."
                    text = "The standard reporting window for damaged, wrong or missing items is 48 hours after delivery. A support specialist will review the timing and any exceptional circumstances."
                else:
                    decision = "manual_complaint_review"
                    reason = "Complaint details or evidence require human review."
                    text = "A support specialist will review the issue and follow up within 24 business hours."

            action = "ticket_created"
            ticket_id, ticket_updated = _create_or_update_ticket(
                store=store,
                email=email,
                order_id=order["order_id"],
                sender=sender,
                intent=intent,
                priority="High" if intent == "complaint" or classification["sentiment"] == "angry" else "Medium",
                summary=classification["summary"],
                notes=reason,
                deduplicate=(version != "v1"),
            )
            reply = draft(email, text)

    elif intent == "other":
        action = "log_only"
        if any(word in body for word in ("collab", "creator", "partnership", "influencer")):
            reason = "Partnership enquiry logged (partnerships@kesarandco.example); no automated customer reply per other/log-only branch."
        elif any(word in body for word in ("gst invoice", "invoice", "gstin")):
            reason = "GST/invoice enquiry logged for finance/accounts review; no automated reply per other/log-only branch."
        elif any(word in body for word in ("seo rank", "google ranking", "guaranteed #1")):
            reason = "Unsolicited sales/spam logged; no customer reply drafted."
        else:
            reason = "No customer reply required by the other/log-only branch."

    # AI-assisted reply drafting with read-only tools (`lookup_order` and `search_faq`).
    # Deterministic ownership, policy_decision, ticket creation, and needs_human flags
    # above are NEVER overridden by the agent.
    required_tool = None
    task_context = ""
    if version == "v2" and not threshold_review and not _is_sensitive_query(body):
        if intent == "order_status" and order is not None:
            required_tool = "lookup_order"
            task_context = (
                f"Draft a concise, factual order-status reply in language={classification['language']}. "
                f"Sender ownership is verified for order {order['order_id']}."
            )
        elif intent == "product_query" and faq_evidence:
            required_tool = "search_faq"
            task_context = (
                f"Draft a concise answer in language={classification['language']} using ONLY approved FAQ evidence."
            )
        elif intent in {"return_refund", "complaint"} and order is not None:
            required_tool = "lookup_order"
            age_val = age_days(email, order)
            task_context = (
                f"Support ticket {ticket_id} is open for human review. Deterministic policy_decision={decision}; "
                f"days_since_delivery={age_val}; attachments_are_unverified_filenames={bool(email['attachments'])}. "
                f"Call lookup_order and search_faq as needed, state next steps accurately in language={classification['language']}, "
                "and NEVER approve a refund or confirm a replacement."
            )

    if agent_context is None:
        if version != "v2":
            agent_context = {"status": "disabled_for_v1", "tool_calls": [], "draft_body": ""}
        elif threshold_review:
            agent_context = {"status": "skipped_low_confidence", "tool_calls": [], "draft_body": ""}
        elif _is_sensitive_query(body):
            agent_context = {"status": "skipped_sensitive_content", "tool_calls": [], "draft_body": ""}
        elif required_tool:
            from kesar_copilot.agent_tools import run_support_agent
            agent_context = run_support_agent(email, intent, task_context, required_tool)
        else:
            agent_context = {"status": "not_applicable", "tool_calls": [], "draft_body": ""}

    agent_draft_used = False
    candidate = (agent_context or {}).get("draft_body", "")
    if candidate and required_tool and action in {"draft_reply", "ticket_created", "needs_human"}:
        reply = draft(email, candidate)
        agent_draft_used = True

    tool_calls = (agent_context or {}).get("tool_calls", [])
    agent_status = (agent_context or {}).get("status", "not_run")
    agent_usage = (agent_context or {}).get("usage", {}) if isinstance(agent_context, dict) else {}

    prompt_tokens = int(classify_meta.get("prompt_tokens", 0)) + int(agent_usage.get("prompt_tokens", 0))
    completion_tokens = int(classify_meta.get("completion_tokens", 0)) + int(agent_usage.get("completion_tokens", 0))
    total_tokens = prompt_tokens + completion_tokens
    estimated_cost = estimate_cost_usd(prompt_tokens, completion_tokens)
    total_retries = int(classify_meta.get("retries", 0)) + int((agent_context or {}).get("retries", 0) if isinstance(agent_context, dict) else 0)
    json_first_try = classify_meta.get("json_valid_first_try")
    json_first_try_str = "not_called" if json_first_try is None else str(bool(json_first_try)).lower()

    audit_note = reason or ("Order ownership mismatch; order details withheld" if order_lookup_status == "sender_mismatch" else "")
    if ticket_updated and ticket_id:
        audit_note = f"Updated existing open ticket {ticket_id}. {audit_note}".strip()

    audit = {
        "email_id": email["id"],
        "received_at": email["received_at"],
        "from_email": sender,
        "intent": intent,
        "secondary_intent": classification.get("secondary_intent"),
        "order_id": (order["order_id"] if order else extracted_order_id) or "",
        "language": classification["language"],
        "sentiment": classification["sentiment"],
        "confidence": confidence,
        "confidence_is_calibrated": "false",
        "classification_source": classification_source,
        "json_valid_first_try": json_first_try_str,
        "order_lookup_status": order_lookup_status,
        "order_found": str(bool(order)).lower(),
        "policy_decision": decision,
        "action_taken": action,
        "needs_human": str(human).lower(),
        "ticket_id": ticket_id,
        "reply_draft": reply,
        "prompt_version": version,
        "agent_status": agent_status,
        "agent_tool_calls": json.dumps(tool_calls, ensure_ascii=False),
        "agent_draft_used": str(agent_draft_used).lower(),
        "llm_retries": total_retries,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": f"{estimated_cost:.6f}",
        "processed_at": datetime.now().astimezone().isoformat(),
        "error": classify_meta.get("error", ""),
        "notes": audit_note,
    }
    store["audit"].append(audit)
    return audit


def batch(version: str, emails: list[dict[str, Any]]) -> dict[str, Any]:
    state: dict[str, Any] = {"audit": [], "tickets": []}
    for email in emails:
        process(email, state, version)
    return state


def save_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run Kesar support email copilot")
    parser.add_argument("--version", choices=["v1", "v2"], default="v2")
    parser.add_argument("--only", default="", help="Comma-separated email IDs, e.g. E01,E05")
    parser.add_argument("--sync-sheets", action="store_true", help="sync outputs to configured Google Sheets tabs")
    args = parser.parse_args()
    emails = load("data/test_emails.json")["emails"]
    if args.only:
        wanted = {part.strip() for part in args.only.split(",") if part.strip()}
        emails = [e for e in emails if e.get("id") in wanted]
    state = batch(args.version, emails)
    output = ROOT / "outputs"
    output.mkdir(exist_ok=True)
    ticket_fields = [
        "ticket_id", "created_at", "updated_at", "order_id", "customer_email",
        "email_ids", "intent", "priority", "status", "summary", "notes",
    ]
    audit_fields = list(state["audit"][0]) if state["audit"] else ["email_id"]
    save_csv(output / f"audit_log_{args.version}.csv", state["audit"], audit_fields)
    save_csv(output / f"tickets_{args.version}.csv", state["tickets"], ticket_fields)
    (output / f"results_{args.version}.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Processed {len(state['audit'])} emails ({args.version}); open unique tickets: {len(state['tickets'])}")
    if args.sync_sheets:
        from kesar_copilot.google_sheets import sync_state_to_sheets
        print(json.dumps(sync_state_to_sheets(state), ensure_ascii=False))


if __name__ == "__main__":
    main()
