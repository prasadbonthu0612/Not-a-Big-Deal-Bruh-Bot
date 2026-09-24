"""Phase 8 - Groq Instagram caption/hashtag rewriting."""
#groq_ai.py
import json
import os
import re
from typing import Any

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()

# The bot can be configured with the Instagram page that should receive
# all calls-to-action and account/profile references.
OUR_INSTAGRAM_PAGE_URL = os.getenv(
    "OUR_INSTAGRAM_PAGE_URL",
    "",
).strip()

OUR_PAGE_TOKEN = "__OUR_INSTAGRAM_PAGE__"

# Deterministic post-validation keeps the model in the intended light-rewrite
# lane. It prevents long AI expansions and invented hashtag sets.
MAX_REWRITE_LENGTH_RATIO = 1.25
MAX_REWRITE_EXTRA_CHARS = 80
MAX_REWRITE_WORD_RATIO = 1.40
MAX_REWRITE_EXTRA_WORDS = 10

# URLs and @mentions are removed/replaced before the AI sees the source.
URL_PATTERN = re.compile(
    r"https?://[^\s<>\]\[(){}\"']+",
    re.IGNORECASE,
)

MENTION_PATTERN = re.compile(
    r"(?<![\w])@[A-Za-z0-9._]{1,50}",
)


SYSTEM_PROMPT = """
You rewrite Instagram Reel metadata for an automated posting system.

The MOST IMPORTANT requirement is LANGUAGE AND WRITING-STYLE PRESERVATION.
The rewritten caption must stay in the same language, script, and linguistic
style as the source caption. Do NOT translate the caption into English unless
the source itself is English.

Rules:
1. Detect the language(s), script(s), and writing style used in the original
   caption and preserve them in the rewrite.
2. If the source is English, rewrite it in natural English.
3. If the source is Telugu written in Telugu script, keep it in Telugu script.
4. If the source is Telugu written using English/Roman letters, keep it in
   Roman/Latin letters. Do NOT convert it into Telugu script or English.
5. If the source is Hindi written in Devanagari, keep it in Devanagari.
6. If the source is Hindi written using English/Roman letters, keep it in
   Roman/Latin letters. Do NOT translate it into English or Devanagari.
7. Apply the same principle to other languages: preserve the original script
   and transliteration style instead of translating the content into English.
8. If the caption intentionally mixes languages, preserve the same
   code-switching pattern. Do not normalize the whole caption into one language.
9. Preserve the same tone, register, slang, colloquial wording, and informality.
   The goal is a LIGHT REWRITE, not a new article.
10. The supplied title is context only. Do not copy the title into the caption
    or turn it into an extra headline unless that text already appears in the
    source caption.
11. Rewrite only the existing caption sentence-by-sentence. Do not add new
    sentences, explanations, analysis, impact sections, summaries, conclusions,
    context, calls to action, or facts that are not already present.
12. Never invent names, people, places, dates, events, claims, statistics,
    locations, causes, consequences, or other facts.
13. Keep approximately the same length and information density as the source.
    A short caption must remain short. Do not expand a headline into an article.
14. Do not silently remove meaningful information just because it is in another
    language or written in transliterated form.
15. Preserve important usernames/account references only when they are replaced
    by the required page placeholder below.
16. Every external URL or account mention in the source has already been replaced
    with __OUR_INSTAGRAM_PAGE__. Keep that exact placeholder where the source
    contains a follow/account reference. Never output any other URL or @username.
    Never output the literal words OUR_PAGE_TOKEN.
17. Do not create links to other pages, websites, accounts, or social profiles.
18. Hashtags are NOT to be invented. If the source has no hashtags, return an
    empty hashtags field. If the source has hashtags, preserve those hashtags
    and do not add new ones.
19. Do not put ordinary prose inside the hashtags field. It should contain only
    hashtags separated by spaces.
20. Preserve useful emojis when appropriate.
21. If the source caption is empty, return an empty caption.
22. Return only the requested structured fields.

Examples:
- English source -> natural English rewrite, not a translation.
- Romanized Telugu -> Romanized Telugu, not Telugu script and not English.
- Hinglish -> Hinglish, not pure English and not Devanagari.
- Telugu-script source -> Telugu script, not Romanized Telugu and not English.

When rewriting transliterated language, do not assume that Romanized Telugu,
Romanized Hindi, or another Romanized language is English merely because it uses
Latin letters. Identify the underlying language from the words and context.""".strip()


SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "instagram_metadata_rewrite",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "caption": {"type": "string"},
                "hashtags": {"type": "string"},
            },
            "required": ["caption", "hashtags"],
            "additionalProperties": False,
        },
    },
}


def _fallback(
    caption: str,
    hashtags: str,
    error: str,
    source_had_external_reference: bool = False,
) -> dict[str, Any]:
    """Return safe source metadata when Groq is unavailable."""
    safe_caption = _replace_external_references(caption)
    safe_hashtags = _clean_hashtags(hashtags)

    safe_caption = _ensure_page_link(
        safe_caption,
        source_had_external_reference,
    )

    return {
        "status": "FALLBACK",
        "caption": safe_caption,
        "hashtags": safe_hashtags,
        "error": error[:1500],
    }


def _replace_external_references(text: str) -> str:
    """
    Replace external URLs and @account mentions with the configured page URL.

    If OUR_INSTAGRAM_PAGE_URL is empty, the references are simply removed.
    """
    text = str(text or "")

    replacement = OUR_PAGE_TOKEN

    text = URL_PATTERN.sub(replacement, text)
    text = MENTION_PATTERN.sub(replacement, text)

    # Collapse repeated page tokens caused by several adjacent references.
    token_pattern = re.compile(
        rf"(?:{re.escape(OUR_PAGE_TOKEN)}(?:\s*[\n,|•-]?\s*)?)+"
    )
    text = token_pattern.sub(OUR_PAGE_TOKEN, text)

    return text.strip()


def _normalize_page_token_variants(text: str) -> str:
    """Normalize model-written variants of the internal page placeholder."""
    text = str(text or "")
    for variant in (
        "OUR_PAGE_TOKEN",
        "__OUR_PAGE_TOKEN__",
        "[OUR_PAGE_TOKEN]",
        "OUR_INSTAGRAM_PAGE_TOKEN",
        "__OUR_INSTAGRAM_PAGE_TOKEN__",
    ):
        text = text.replace(variant, OUR_PAGE_TOKEN)
    return text


def _restore_our_page_link(text: str) -> str:
    """
    Replace the internal token with the configured page URL.
    If no page URL is configured, remove the token cleanly.
    """
    text = _normalize_page_token_variants(text)

    if OUR_INSTAGRAM_PAGE_URL:
        text = text.replace(
            OUR_PAGE_TOKEN,
            OUR_INSTAGRAM_PAGE_URL,
        )
    else:
        text = text.replace(OUR_PAGE_TOKEN, "")

    return text.strip()


def _remove_unwanted_ai_references(text: str) -> str:
    """
    Final safety pass: the AI must not introduce another URL or @account.
    Any such reference is replaced by our configured page URL/token.
    """
    text = _normalize_page_token_variants(text)

    replacement = (
        OUR_PAGE_TOKEN
        if OUR_INSTAGRAM_PAGE_URL
        else ""
    )

    text = URL_PATTERN.sub(replacement, text)
    text = MENTION_PATTERN.sub(replacement, text)

    return text.strip()


def _clean_hashtags(hashtags: str) -> str:
    """Keep only hashtag tokens and remove accidental URLs/account mentions."""
    hashtags = _remove_unwanted_ai_references(hashtags)

    # Remove page tokens from the hashtag field. The page link belongs in
    # the caption, not in the hashtag field.
    hashtags = hashtags.replace(OUR_PAGE_TOKEN, " ")

    # Keep only actual hashtag tokens.
    tags = re.findall(
        r"#[A-Za-z0-9_\u0080-\uffff]+",
        hashtags,
    )

    # Deduplicate while preserving order.
    unique_tags = []
    seen = set()

    for tag in tags:
        normalized = tag.lower()
        if normalized in seen:
            continue

        seen.add(normalized)
        unique_tags.append(tag)

    return " ".join(unique_tags)


def _ensure_page_link(
    caption: str,
    source_had_external_reference: bool,
) -> str:
    """
    Ensure the configured page URL replaces source account/link references.

    If the source had no external reference, do not add a promotional link
    merely because the environment variable is configured.
    """
    caption = _restore_our_page_link(caption)

    if (
        OUR_INSTAGRAM_PAGE_URL
        and source_had_external_reference
        and OUR_INSTAGRAM_PAGE_URL not in caption
    ):
        if caption:
            caption = (
                f"{caption.rstrip()}\n\n"
                f"Follow us: {OUR_INSTAGRAM_PAGE_URL}"
            )
        else:
            caption = (
                f"Follow us: {OUR_INSTAGRAM_PAGE_URL}"
            )

    return caption.strip()


def _rewrite_is_reasonably_light(source_caption: str, rewritten_caption: str) -> bool:
    """Reject obvious AI expansions instead of publishing them automatically."""
    source = str(source_caption or "").strip()
    rewritten = str(rewritten_caption or "").strip()
    if not source:
        return not rewritten
    max_len = max(240, int(len(source) * MAX_REWRITE_LENGTH_RATIO) + MAX_REWRITE_EXTRA_CHARS)
    if len(rewritten) > max_len:
        return False

    source_words = re.findall(r"\S+", source)
    rewritten_words = re.findall(r"\S+", rewritten)
    max_words = max(30, int(len(source_words) * MAX_REWRITE_WORD_RATIO) + MAX_REWRITE_EXTRA_WORDS)
    if len(rewritten_words) > max_words:
        return False

    # A light rewrite should not turn one or two source sentences into a long
    # multi-section article. Ellipses are counted as one punctuation group.
    source_sentences = max(1, len(re.findall(r"[.!?]+", source)))
    rewritten_sentences = max(1, len(re.findall(r"[.!?]+", rewritten)))
    return rewritten_sentences <= source_sentences + 1


def _finalize_hashtags(source_hashtags: str, ai_hashtags: str) -> str:
    """Never let the model invent hashtags that were absent from the source."""
    source_tags = _clean_hashtags(source_hashtags)
    if not source_tags:
        return ""
    return source_tags


def rewrite_instagram_metadata(
    title: str,
    caption: str,
    hashtags: str,
) -> dict[str, Any]:
    """
    Rewrite caption/hashtags with Groq.

    The AI is not a hard dependency: if Groq fails, sanitized source metadata
    is returned so the job can continue safely.
    """
    caption = str(caption or "").strip()
    hashtags = str(hashtags or "").strip()
    title = str(title or "").strip()

    # Detect this before sanitizing so we know whether the source contained
    # an external account/page reference that must be replaced.
    source_had_external_reference = bool(
        URL_PATTERN.search(caption)
        or MENTION_PATTERN.search(caption)
        or URL_PATTERN.search(hashtags)
        or MENTION_PATTERN.search(hashtags)
    )

    safe_caption = _replace_external_references(caption)
    safe_hashtags = _clean_hashtags(hashtags)

    if not GROQ_API_KEY:
        return _fallback(
            safe_caption,
            safe_hashtags,
            "GROQ_API_KEY is not configured; sanitized original metadata retained.",
            source_had_external_reference,
        )

    # Nothing to rewrite. Avoid spending an API request on an empty source.
    if not safe_caption and not safe_hashtags:
        return {
            "status": "SKIPPED_EMPTY",
            "caption": "",
            "hashtags": "",
            "error": "",
        }

    # Hashtags alone do not need an LLM call. Preserve them without spending
    # tokens on a response whose only safe output is the source hashtag set.
    if not safe_caption and safe_hashtags:
        return {
            "status": "SKIPPED_HASHTAGS_ONLY",
            "caption": "",
            "hashtags": safe_hashtags,
            "error": "",
        }

    try:
        client = Groq(api_key=GROQ_API_KEY)

        user_payload = {
            "title": title,
            "caption": safe_caption,
            "hashtags": safe_hashtags,
            "OUR_PAGE_TOKEN": (
                OUR_PAGE_TOKEN
                if OUR_INSTAGRAM_PAGE_URL
                else ""
            ),
        }

        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": (
                        "Rewrite ONLY the existing caption lightly. Preserve "
                        "the source language, script, transliteration, "
                        "code-switching, tone, facts, and approximate length. "
                        "Do NOT add explanations, analysis, new facts, new "
                        "sentences, conclusions, or a longer article. Do NOT "
                        "translate any non-English phrase. Keep the exact "
                        "__OUR_INSTAGRAM_PAGE__ placeholder wherever a source "
                        "account/page reference belongs. Never output the words "
                        "OUR_PAGE_TOKEN. Return no invented hashtags.\n\n"
                        + json.dumps(
                            user_payload,
                            ensure_ascii=False,
                        )
                    ),
                },
            ],
            response_format=SCHEMA,
            temperature=0.4,
        )

        content = (
            response.choices[0].message.content
            or "{}"
        )

        result = json.loads(content)

        rewritten_caption = str(
            result.get("caption", "")
        ).strip()

        rewritten_hashtags = str(
            result.get("hashtags", "")
        ).strip()

        # Final deterministic safety pass.
        rewritten_caption = _remove_unwanted_ai_references(
            rewritten_caption
        )

        # If the model expanded a short source into an article, do not publish
        # the expansion. Keep the sanitized original caption instead.
        if not _rewrite_is_reasonably_light(
            safe_caption,
            rewritten_caption,
        ):
            rewritten_caption = safe_caption
            final_status = "SUCCESS_GUARDED"
        else:
            final_status = "SUCCESS"

        # The source hashtag set is the only allowed hashtag source of truth.
        rewritten_hashtags = _finalize_hashtags(
            safe_hashtags,
            rewritten_hashtags,
        )

        rewritten_caption = _ensure_page_link(
            rewritten_caption,
            source_had_external_reference,
        )

        return {
            "status": final_status,
            "caption": rewritten_caption,
            "hashtags": rewritten_hashtags,
            "error": "" if final_status == "SUCCESS" else "AI rewrite exceeded the light-rewrite length guard; sanitized source caption retained.",
        }

    except Exception as exc:
        return _fallback(
            safe_caption,
            safe_hashtags,
            f"Groq request failed; sanitized original metadata retained: {exc}",
            source_had_external_reference,
        )
