#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
factcheck_openalex.py (v2)
============================

Фактчек-лукап через OpenAlex API — Ур.0.5 каскада.
Бесплатная (CC0) замена Scopus/Web of Science.
Мгновенная проверка DOI без HTTP-парсинга страниц.

Использование:
    ./crawl4ai/.venv/Scripts/python.exe openalex/factcheck_openalex.py --targets targets.json [--prefix oa] [--timeout 15]

Зависимости: только стандартная библиотека (urllib.request)
Формат вывода совместим с Crawl4AI и Scrapling: openalex/{prefix}_{id}.txt

Изменения v1->v2 (аудит 2026-07-16):
  - Вывод кросс-идентификаторов (ids: PMID, PMCID, etc.) в выходной файл.
  - Вывод OA-статуса и PDF-ссылок (locations) в выходной файл.
"""

import argparse
import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# Shared pipeline utilities (pipeline/common.py)
sys.path.insert(0, str(Path(__file__).parent.parent))
from common import read_targets, validate_prefix, fix_windows_console

MAX_TEXT_CHARS = 50_000
TRUNCATE_KEEP_CHARS = 2_000
DOI_URL_RE = re.compile(r"doi\.org/(10\.[^/?#]+/[^/?#]+)")
DOI_EXPECT_RE = re.compile(r"(?:DOI|doi)\s*(10\.[^\s,;)\]]+)", re.IGNORECASE)
OPENALEX_URL = "https://api.openalex.org/works/https://doi.org/{}"
_DEFAULT_MAILTO = "factcheck@example.com"


# =================== ИЗВЛЕЧЕНИЕ DOI ===================

def extract_doi(target):
    """Извлечь DOI из URL или поля expect целевого объекта."""
    url = target.get("url", "")
    expect = target.get("expect", "")

    # 1. Прямой doi.org URL
    m = DOI_URL_RE.search(url)
    if m:
        return m.group(1)

    # 2. DOI в поле expect (например, "DOI 10.1038/s41591-018-0300-7")
    # Класс [^\s,;)\]] не захватывает trailing-пунктуацию (запятые, точки,
    # скобки), которая иначе ломала lookup (404 NOT_FOUND для реальных статей).
    # .rstrip(".,;:)]") добивает trailing-точки/двоеточия, которые regex пропускает
    # (т.к. DOI содержит '.') — напр. "DOI 10.1136/bmj.n71." → "10.1136/bmj.n71".
    m = DOI_EXPECT_RE.search(expect)
    if m:
        return m.group(1).rstrip(".,;:)]")

    return None


# =================== ЗАПРОС К OPENALEX ===================

def lookup_doi(doi, timeout, mailto):
    """Запросить метаданные работы по DOI через OpenAlex REST API.
    
    Возвращает (data: dict | None, error: str | None).
    """
    url = OPENALEX_URL.format(doi) + f"?mailto={mailto}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "factcheck-openalex/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw), None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, "NOT_FOUND"
        return None, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"NETWORK: {exc.reason}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def format_authors(data):
    """Форматировать список авторов из ответа OpenAlex."""
    authors = data.get("authorships", [])
    names = []
    for a in authors[:5]:  # первые 5 авторов
        name = a.get("author", {}).get("display_name", "")
        if name:
            names.append(name)
    suffix = " et al." if len(authors) > 5 else ""
    return "; ".join(names) + suffix


def format_biblio(data):
    """Форматировать библиографические данные: журнал, том, страницы, год."""
    loc = (data.get("primary_location") or {})
    source = loc.get("source") or {}
    biblio = loc.get("biblio") or {}

    journal = source.get("display_name", "?")
    year = data.get("publication_year", "?")
    volume = biblio.get("volume")
    first_page = biblio.get("first_page")
    last_page = biblio.get("last_page")

    parts = [f"Journal: {journal}", f"Year: {year}"]
    if volume:
        parts.append(f"Volume: {volume}")
    if first_page:
        pages = first_page
        if last_page:
            pages += f"-{last_page}"
        parts.append(f"Pages: {pages}")
    return ", ".join(parts)


# =================== СОХРАНЕНИЕ РЕЗУЛЬТАТА ===================

def _save_result(target, doi, data, error, status, prefix):
    """Сохранить результат в openalex/{prefix}_{id}.txt — формат совместим с Crawl4AI/Scrapling."""
    fname = Path(__file__).parent / f"{prefix}_{target['id']}.txt"
    fname.parent.mkdir(parents=True, exist_ok=True)

    with open(fname, "w", encoding="utf-8") as f:
        f.write(f"URL: {target['url']}\n")
        f.write(f"FACT: {target['fact']}\n")
        f.write(f"EXPECT: {target.get('expect', '')}\n")
        f.write(f"DOI: {doi or 'N/A'}\n")
        f.write(f"STATUS: {status}\n")
        f.write(f"SUCCESS: {status == 'CONFIRMED'}\n")
        if error:
            f.write(f"ERROR: {error}\n")

        f.write("\n" + "=" * 70 + "\n")

        if data:
            title = data.get("title", "?")
            authors = format_authors(data)
            biblio = format_biblio(data)
            oa_doi = data.get("doi", "?")
            doi_url = oa_doi.replace("https://doi.org/", "") if oa_doi else "?"

            f.write(f"OpenAlex Title      : {title}\n")
            f.write(f"OpenAlex Authors     : {authors}\n")
            f.write(f"OpenAlex DOI         : {doi_url}\n")
            f.write(f"OpenAlex Biblio      : {biblio}\n")
            f.write(f"OpenAlex Cited by    : {data.get('cited_by_count', '?')}\n")
            f.write(f"OpenAlex Type        : {data.get('type', '?')}\n")

            # Кросс-идентификаторы (PMID, PMCID, MAG, etc.)
            ids = data.get("ids", {})
            if ids:
                id_parts = [f"{k}={v}" for k, v in ids.items() if k != "openalex"]
                if id_parts:
                    f.write(f"OpenAlex IDs         : {'; '.join(id_parts)}\n")

            # OA-статус и PDF-ссылки
            loc = (data.get("primary_location") or {})
            if loc:
                is_oa = loc.get("is_oa", False)
                pdf_url = loc.get("pdf_url") or ""
                landing = loc.get("landing_page_url") or ""
                oa_status = "Open Access" if is_oa else "Closed"
                parts = [f"Status: {oa_status}"]
                if pdf_url:
                    parts.append(f"PDF: {pdf_url}")
                if landing:
                    parts.append(f"Landing: {landing}")
                f.write(f"OpenAlex Access      : {' | '.join(parts)}\n")
        elif error:
            f.write(f"[Error: {error}]\n")
    return fname


# =================== ОСНОВНОЙ ЦИКЛ ===================

def batch_lookup(targets, prefix, timeout, mailto):
    """Пакетный lookup DOI через OpenAlex API."""
    results = {}
    doi_count = 0
    skip_count = 0

    for target in targets:
        tid = target["id"]
        url = target["url"]
        fact = target["fact"]
        expect = target.get("expect", "")

        print(f"[{tid}] {url}", flush=True)
        print(f"    fact   : {fact}", flush=True)
        print(f"    expect : {expect}", flush=True)

        doi = extract_doi(target)
        if not doi:
            print(f"    status : SKIP (no DOI found in URL or expect)", flush=True)
            status = "SKIP"
            error = "No DOI to look up"
            data = None
            skip_count += 1
        else:
            doi_count += 1
            print(f"    doi    : {doi}", flush=True)

            start_ts = time.time()
            data, error = lookup_doi(doi, timeout, mailto)
            elapsed = time.time() - start_ts

            if error:
                status = "ERROR"
                print(f"    status : {status} — {error}  elapsed={elapsed:.1f}s", flush=True)
            else:
                title = data.get("title", "?")
                year = data.get("publication_year", "?")
                cited = data.get("cited_by_count", 0)
                status = "CONFIRMED"
                print(f"    status : {status}  title=\"{title[:80]}\"  year={year}  cited={cited}  elapsed={elapsed:.1f}s", flush=True)

        fname = _save_result(target, doi, data, error, status, prefix)
        print(f"    saved  : {fname}", flush=True)

        results[tid] = {
            "fact": fact,
            "url": url,
            "doi": doi,
            "status": status,
            "error": error,
        }

        # Вежливая пауза между запросами (rate limit OpenAlex)
        if doi and not error:
            time.sleep(0.15)

    # Summary
    print("\n" + "=" * 70, flush=True)
    print("SUMMARY (OpenAlex Ур.0.5)", flush=True)
    print("=" * 70, flush=True)
    confirmed = sum(1 for r in results.values() if r["status"] == "CONFIRMED")
    errors = sum(1 for r in results.values() if r["status"] == "ERROR")
    skipped = sum(1 for r in results.values() if r["status"] == "SKIP")
    print(f"CONFIRMED={confirmed}  ERROR={errors}  SKIP={skipped}  TOTAL={len(results)}", flush=True)
    for tid, r in results.items():
        flag = "OK" if r["status"] == "CONFIRMED" else ("ERR" if r["status"] == "ERROR" else "SKIP")
        doi_str = r['doi'] or '—'
        err_str = f"  err={r['error']}" if r.get("error") else ""
        print(f"  [{flag}] {tid}  doi:{doi_str}  {r['url']}{err_str}", flush=True)

    return results


# =================== MAIN ===================

def main():
    ap = argparse.ArgumentParser(description="OpenAlex fact-checker — Ур.0.5 каскада")
    ap.add_argument("--targets", required=True, help="path to JSON with TARGETS list")
    ap.add_argument("--prefix", default="oa", help="output filename prefix (default: oa)")
    ap.add_argument("--timeout", type=int, default=15, help="per-request timeout (s) (default: 15)")
    ap.add_argument("--mailto", default=_DEFAULT_MAILTO,
                    help=f"email for OpenAlex Polite Pool (default: {_DEFAULT_MAILTO})")
    args = ap.parse_args()

    fix_windows_console()

    validate_prefix(args.prefix)
    targets = read_targets(Path(args.targets), validate_url_https=False)
    print(f"Loaded {len(targets)} targets from {args.targets}", flush=True)
    print(f"Settings: timeout={args.timeout}s, prefix={args.prefix}", flush=True)

    batch_lookup(targets, args.prefix, args.timeout, args.mailto)


if __name__ == "__main__":
    main()
