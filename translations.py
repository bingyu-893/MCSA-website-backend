"""Traditional Chinese is local; only new English copy needs a translation service."""

import json
import os
import re
from html import escape, unescape
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from opencc import OpenCC

TRADITIONAL = OpenCC("s2t")
ENDPOINT = "https://api.cognitive.microsofttranslator.com/translate?api-version=3.0&from=zh-Hans&to=en"


class TranslationError(Exception):
    def __init__(self, code, retryable=False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def translate_text(source, terms):
    """Translate plain text with explicit name mappings; never return HTML."""
    key = os.environ.get("AZURE_TRANSLATOR_KEY", "")
    if not key:
        raise TranslationError("not_configured")
    names = {t["zh"]: t["en"] for t in terms if t["enabled"]}
    placeholders = re.findall(r"\{[A-Za-z_][\w]*\}", source)
    names.update({token: token for token in placeholders})
    pattern = re.compile("|".join(re.escape(k) for k in sorted(names, key=len, reverse=True))) if names else None
    parts, cursor = [], 0
    for match in pattern.finditer(source) if pattern else []:
        parts.append(escape(source[cursor:match.start()]))
        parts.append('<mstrans:dictionary translation="' + escape(names[match.group()], quote=True)
                     + '">' + escape(match.group()) + '</mstrans:dictionary>')
        cursor = match.end()
    parts.append(escape(source[cursor:]))
    request = Request(ENDPOINT, data=json.dumps([{"Text": "".join(parts)}]).encode(), headers={
        "Content-Type": "application/json",
        "Ocp-Apim-Subscription-Key": key,
        "Ocp-Apim-Subscription-Region": os.environ.get("AZURE_TRANSLATOR_REGION", ""),
    }, method="POST")
    try:
        with urlopen(request, timeout=25) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise TranslationError("service_unavailable" if exc.code == 429 or exc.code >= 500 else "service_configuration",
                               retryable=exc.code == 429 or exc.code >= 500) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise TranslationError("network_error", retryable=True) from exc
    except (ValueError, TypeError) as exc:
        raise TranslationError("invalid_response") from exc
    try:
        if not isinstance(result, list) or len(result) != 1:
            raise ValueError()
        english = next(row["text"] for row in result[0]["translations"] if row["to"] == "en")
        if not isinstance(english, str) or not english.strip():
            raise ValueError()
        english = unescape(english)
        if len(english) > 12000 or sorted(re.findall(r"\{[A-Za-z_][\w]*\}", english)) != sorted(placeholders):
            raise ValueError()
        if "mstrans:dictionary" in english:
            raise ValueError()
        return english
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        raise TranslationError("invalid_response") from exc


def convert_legacy_languages(value):
    """Upgrade Cantonese-era content without changing its Simplified Chinese source."""
    if isinstance(value, dict):
        if "zh" in value and "en" in value:
            value.pop("yue", None)
            value["hant"] = TRADITIONAL.convert(value["zh"])
        else:
            for child in value.values():
                convert_legacy_languages(child)
    elif isinstance(value, list):
        for child in value:
            convert_legacy_languages(child)
    return value
