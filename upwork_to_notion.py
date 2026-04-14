#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Upwork search-result HTML -> Notion database importer

What this script does
---------------------
1. Parse one or more Upwork search-result HTML files
2. Extract job cards into structured records
3. Create a Notion database (optional)
4. Upsert records into the database using Job UID as the de-duplication key

Environment variables
---------------------
Required:
  NOTION_TOKEN=secret_xxx

For creating a new database:
  NOTION_PARENT_PAGE_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

Optional:
  NOTION_DATASOURCE_ID=<existing data source id>   # preferred (Notion API 2025-09-03+)
  NOTION_DATABASE_ID=<existing database id>        # database container id (will resolve to a data source)
  UPWORK_DATABASE_TITLE=Upwork案件候補
  DEBUG=1

Usage
-----
Create a new database and import all html files in ./input:
  python upwork_to_notion.py --input-dir ./input --create-db

Use an existing database:
  python upwork_to_notion.py --input-dir ./input --database-id YOUR_DATABASE_ID

Use an existing data source (preferred on Notion API 2025-09-03+):
  python upwork_to_notion.py --input-dir ./input --datasource-id YOUR_DATASOURCE_ID

Single file:
  python upwork_to_notion.py --html "./AI Automation Workflows Setup on Make.com.html" --create-db

Dependencies
------------
pip install beautifulsoup4 requests
(.env から自動読込したい場合も追加インストール不要)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


def load_dotenv_fallback(env_path: str = ".env") -> None:
    """
    Minimal .env loader (no extra dependencies).
    - Reads KEY=VALUE lines from env_path if it exists
    - Does not override already-set environment variables
    """
    try:
        p = Path(env_path)
        if not p.exists() or not p.is_file():
            return
        # Windows editors sometimes save .env as UTF-16 (or with BOM).
        # Try a few common encodings to avoid silently failing to load env vars.
        text: Optional[str] = None
        for enc in ("utf-8-sig", "utf-8", "utf-16", "cp932"):
            try:
                text = p.read_text(encoding=enc)
                break
            except UnicodeError:
                continue
        if text is None:
            return

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().lstrip("\ufeff")
            value = value.strip()
            if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
                value = value[1:-1]
            if not key:
                continue
            os.environ.setdefault(key, value)
    except Exception:
        # .env is a convenience; do not break execution if it's malformed.
        return


# Load environment variables from .env early (before reading os.getenv below).
load_dotenv_fallback(".env")

# Use the latest Notion API version that supports data sources.
NOTION_API_VERSION = "2025-09-03"
UPWORK_BASE_URL = "https://www.upwork.com"
DEFAULT_DB_TITLE = os.getenv("UPWORK_DATABASE_TITLE", "Upwork案件候補")
DEBUG = os.getenv("DEBUG", "0") == "1"


def debug(*args: Any) -> None:
    if DEBUG:
        print("[DEBUG]", *args, file=sys.stderr)


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable is required: {name}")
    return value


def normalize_uuid(raw: str) -> str:
    raw = raw.replace("-", "").strip()
    if len(raw) != 32:
        raise ValueError(f"Invalid Notion id length: {raw}")
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def first_or_none(items: List[str]) -> Optional[str]:
    return items[0] if items else None


def parse_number_or_none(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def contains_any(haystack: str, keywords: Iterable[str]) -> bool:
    hay = haystack.lower()
    return any(k.lower() in hay for k in keywords)


@dataclass
class JobRecord:
    title: str
    job_uid: str
    posted: str = ""
    search_position: Optional[int] = None
    detail_page_url: str = ""
    opening_url: str = ""
    client_payment_verified: Optional[bool] = None
    client_rating: Optional[float] = None
    client_spent: str = ""
    client_location: str = ""
    budget_type: str = ""
    budget: str = ""
    experience_level: str = ""
    est_time: str = ""
    proposals: str = ""
    skills: List[str] = None
    description: str = ""
    source_type: str = "HTML"
    raw_html_snippet: str = ""
    match_score: Optional[int] = None
    priority: str = "中"
    red_flag: bool = False
    notes: str = ""
    proposal_seed: str = ""
    client_summary: str = ""
    video_meeting_detected: bool = False
    is_quick_win: bool = False
    proposal_count_floor: Optional[int] = None

    def __post_init__(self) -> None:
        if self.skills is None:
            self.skills = []

    @property
    def platform(self) -> str:
        return "Upwork"

    @property
    def status(self) -> str:
        return "pending"


class UpworkParser:
    SKILL_CANDIDATES = [
        "Automation", "Business Process Automation", "API Integration", "Dashboard",
        "Make.com", "AI Trading", "Node.js", "n8n", "CRM Automation",
        "Advertising Automation", "Lead Management Automation", "Marketing Automation",
        "Legal Tech", "Clio", "Docketwise", "Lawmatics", "Filevine", "Softr",
        "Airtable", "HubSpot", "Notion", "PDF", "Google Sheets", "Glide Apps",
        "Fillout", "JavaScript", "Generative AI Prompt", "Supabase", "Shopify",
        "Amazon SP-API", "OpenAI", "Anthropic", "WhatsApp", "Slack", "Email",
    ]

    def parse_file(self, path: Path) -> List[JobRecord]:
        html = path.read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        articles = soup.select('article[data-test="JobTile"]')
        if not articles and soup.name == "article":
            articles = [soup]
        results: List[JobRecord] = []
        for article in articles:
            try:
                results.append(self.parse_article(article))
            except Exception as exc:
                print(f"[WARN] Failed to parse article in {path.name}: {exc}", file=sys.stderr)
        return results

    def parse_article(self, article: BeautifulSoup) -> JobRecord:
        job_uid = article.get("data-ev-job-uid", "").strip()
        if not job_uid:
            raise ValueError("Job UID not found")

        position_raw = article.get("data-ev-position")
        search_position = int(position_raw) if position_raw and position_raw.isdigit() else None

        title_link = article.select_one('[data-test="job-tile-title-link UpLink"]')
        title = clean_text(title_link.get_text(" ", strip=True)) if title_link else ""
        relative_href = title_link.get("href", "") if title_link else ""
        opening_url = self._absolute_url(relative_href)
        detail_page_url = f"{UPWORK_BASE_URL}/jobs/~02{job_uid}"

        posted_spans = article.select('[data-test="job-pubilshed-date"] span')
        posted = clean_text(" ".join(s.get_text(" ", strip=True) for s in posted_spans[1:])) or (
            clean_text(" ".join(s.get_text(" ", strip=True) for s in posted_spans))
        )

        payment_text = clean_text(
            (article.select_one('[data-test="payment-verified"]') or article).get_text(" ", strip=True)
        )
        if "Payment verified" in payment_text:
            payment_verified = True
        elif "Payment unverified" in payment_text:
            payment_verified = False
        else:
            payment_verified = None

        rating_sr = article.select_one('.air3-rating-background .sr-only')
        client_rating = parse_number_or_none(rating_sr.get_text(" ", strip=True) if rating_sr else "")

        spent_el = article.select_one('[data-test="total-spent"] strong')
        client_spent = clean_text(spent_el.get_text(" ", strip=True)) if spent_el else ""

        location_el = article.select_one('[data-test="location"] .rr-mask')
        client_location = clean_text(location_el.get_text(" ", strip=True)) if location_el else ""

        budget_type, budget, experience_level, est_time = self._parse_job_info(article)
        description_el = article.select_one('[data-test="UpCLineClamp JobDescription"]')
        description = clean_text(description_el.get_text(" ", strip=True)) if description_el else ""

        proposals_el = article.select_one('[data-test="proposals-tier"] strong')
        proposals = clean_text(proposals_el.get_text(" ", strip=True)) if proposals_el else ""

        proposal_count_floor = self._parse_proposal_floor(proposals)

        tag_els = article.select('[data-test="TokenClamp JobAttrs"] [data-test="token"]')
        raw_tags = [clean_text(tag.get_text(" ", strip=True)) for tag in tag_els]
        raw_tags = [t for t in raw_tags if t and not t.startswith("+")]
        skills = self._normalize_skills(raw_tags, title, description)

        video_meeting_detected = self._detect_video_meetings(description)
        is_quick_win = self._check_quick_win(budget, client_rating)

        client_summary = self._build_client_summary(payment_verified, client_location, client_rating, client_spent)
        match_score = self._estimate_match_score(title, description, skills, budget, proposals, payment_verified, client_rating, client_spent)
        priority = self._estimate_priority(match_score)
        red_flag = self._estimate_red_flag(payment_verified, client_rating, client_spent, budget)
        notes = self._build_notes(description, budget, payment_verified, client_rating, client_spent)
        proposal_seed = self._build_proposal_seed(title, description, skills)
        raw_html_snippet = self._make_raw_snippet(article)

        return JobRecord(
            title=title,
            job_uid=job_uid,
            posted=posted,
            search_position=search_position,
            detail_page_url=detail_page_url,
            opening_url=opening_url,
            client_payment_verified=payment_verified,
            client_rating=client_rating,
            client_spent=client_spent,
            client_location=client_location,
            budget_type=budget_type,
            budget=budget,
            experience_level=experience_level,
            est_time=est_time,
            proposals=proposals,
            skills=skills,
            description=description,
            source_type="HTML",
            raw_html_snippet=raw_html_snippet,
            match_score=match_score,
            priority=priority,
            red_flag=red_flag,
            notes=notes,
            proposal_seed=proposal_seed,
            client_summary=client_summary,
            video_meeting_detected=video_meeting_detected,
            is_quick_win=is_quick_win,
            proposal_count_floor=proposal_count_floor,
        )

    def _absolute_url(self, href: str) -> str:
        if not href:
            return ""
        if href.startswith("http"):
            return href
        return UPWORK_BASE_URL + href

    def _parse_job_info(self, article: BeautifulSoup) -> Tuple[str, str, str, str]:
        budget_type = ""
        budget = ""
        experience_level = ""
        est_time = ""
        info_items = article.select('[data-test="JobInfo"] li')
        for li in info_items:
            text = clean_text(li.get_text(" ", strip=True))
            test_name = li.get("data-test", "")
            if test_name == "job-type-label":
                if text.startswith("Hourly:"):
                    budget_type = "Hourly"
                    budget = text.replace("Hourly:", "", 1).strip()
                elif "Fixed price" in text:
                    budget_type = "Fixed"
            elif test_name == "is-fixed-price":
                budget = text.replace("Est. budget:", "", 1).strip()
            elif test_name == "experience-level":
                experience_level = text
            elif test_name == "duration-label":
                est_time = text.replace("Est. time:", "", 1).strip()
        return budget_type, budget, experience_level, est_time

    def _normalize_skills(self, raw_tags: List[str], title: str, description: str) -> List[str]:
        text_blob = " ".join([title, description] + raw_tags)
        results: List[str] = []
        for skill in self.SKILL_CANDIDATES:
            if skill in raw_tags or contains_any(text_blob, [skill]):
                results.append(skill)
        deduped = []
        seen = set()
        for s in results:
            if s not in seen:
                deduped.append(s)
                seen.add(s)
        return deduped

    def _detect_video_meetings(self, description: str) -> bool:
        keywords = [
            "Zoom", "Call", "Interview", "Meeting", "Video",
            "Google Meet", "Microsoft Teams", "Talk on the phone",
        ]
        return contains_any(description or "", keywords)

    def _check_quick_win(self, budget: str, client_rating: Optional[float]) -> bool:
        """
        $1戦略（実績作り）候補:
        - 予算（Budget）が $50 以下
        - クライアント評価が 4.8 以上
        """
        if client_rating is None:
            return False
        budget_num = parse_number_or_none(budget)
        if budget_num is None:
            return False
        return budget_num <= 50 and client_rating >= 4.8

    def _parse_proposal_floor(self, proposals: str) -> Optional[int]:
        """
        Examples:
          - "15 to 20" -> 15
          - "20 to 50" -> 20
          - "50+" -> 50
        """
        n = parse_number_or_none(proposals)
        return int(n) if n is not None else None

    def _build_client_summary(self, payment_verified: Optional[bool], location: str, rating: Optional[float], spent: str) -> str:
        payment = "Verified" if payment_verified else "Unverified" if payment_verified is False else "Unknown"
        rating_str = f"{rating:.1f}" if rating is not None else "N/A"
        pieces = [payment]
        if location:
            pieces.append(location)
        pieces.append(rating_str)
        if spent:
            pieces.append(f"{spent} spent")
        return " / ".join(pieces)

    def _estimate_match_score(
        self,
        title: str,
        description: str,
        skills: List[str],
        budget: str,
        proposals: str,
        payment_verified: Optional[bool],
        client_rating: Optional[float],
        client_spent: str,
    ) -> int:
        score = 45

        strong_keywords = [
            "Make.com", "n8n", "API Integration", "Supabase", "Shopify",
            "Amazon SP-API", "OpenAI", "Anthropic", "Automation",
        ]
        for kw in strong_keywords:
            if kw in skills or contains_any(f"{title} {description}", [kw]):
                score += 5

        if contains_any(description, ["100+ users", "50+ users", "OAuth", "rate limits", "token refresh", "modular"]):
            score += 10

        if payment_verified:
            score += 8
        elif payment_verified is False:
            score -= 8

        if client_rating and client_rating >= 4.8:
            score += 8
        elif client_rating == 0:
            score -= 4

        if client_spent:
            if "$80K+" in client_spent or "$10K+" in client_spent:
                score += 8
            elif "$0" in client_spent:
                score -= 8

        if budget:
            if "$55.00" in budget or "$5.00" in budget:
                score -= 12
            elif "$20.00 - $40.00" in budget:
                score += 6

        if proposals:
            if "50+" in proposals:
                score -= 5
            elif "10 to 15" in proposals:
                score += 3

        return max(0, min(100, score))

    def _estimate_priority(self, score: Optional[int]) -> str:
        if score is None:
            return "中"
        if score >= 80:
            return "高"
        if score >= 60:
            return "中"
        return "低"

    def _estimate_red_flag(
        self,
        payment_verified: Optional[bool],
        client_rating: Optional[float],
        client_spent: str,
        budget: str,
    ) -> bool:
        if payment_verified is False:
            return True
        if client_rating == 0 and "$0" in client_spent:
            return True
        if budget in {"$55.00", "$5.00 - $15.00"}:
            return True
        return False

    def _build_notes(self, description: str, budget: str, payment_verified: Optional[bool], client_rating: Optional[float], client_spent: str) -> str:
        note_parts = []
        if contains_any(description, ["OAuth", "token refresh", "revoked access"]):
            note_parts.append("OAuthや失敗分離の論点あり")
        if contains_any(description, ["100+ users", "50+ users", "individual schedules"]):
            note_parts.append("スケール設計が重要")
        if payment_verified is False:
            note_parts.append("支払い未認証")
        if client_rating == 0 and "$0" in client_spent:
            note_parts.append("新規クライアント警戒")
        if budget in {"$55.00", "$5.00 - $15.00"}:
            note_parts.append("予算低め")
        return " / ".join(note_parts) or "案件説明から要件整理推奨"

    def _build_proposal_seed(self, title: str, description: str, skills: List[str]) -> str:
        seeds = []
        if "Make.com" in skills and "n8n" in skills:
            seeds.append("Make.com と n8n の比較理由を先に示す")
        elif "Make.com" in skills:
            seeds.append("Make.com での実装経験を先頭で示す")

        if contains_any(description, ["OAuth", "token refresh"]):
            seeds.append("OAuth更新と失敗分離の設計方針を書く")

        if contains_any(description, ["Supabase"]):
            seeds.append("Supabase を中心にしたデータ設計を説明する")

        if contains_any(description, ["Shopify", "Amazon SP-API"]):
            seeds.append("外部API連携の実績を具体例で出す")

        if contains_any(description, ["Slack", "WhatsApp", "Email"]):
            seeds.append("配信チャネル抽象化を提案する")

        return " / ".join(seeds) or "類似自動化案件の実績と再利用可能な構成を短く示す"

    def _make_raw_snippet(self, article: BeautifulSoup, max_len: int = 600) -> str:
        text = clean_text(str(article))
        return text[:max_len]


class NotionClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_API_VERSION,
            "Content-Type": "application/json",
        })

    def _request(self, method: str, url: str, **kwargs: Any) -> Dict[str, Any]:
        resp = self.session.request(method, url, timeout=60, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text}")
        return resp.json()

    def create_database(self, parent_page_id: str, title: str) -> Tuple[str, str]:
        url = "https://api.notion.com/v1/databases"
        payload = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            # As of Notion-Version 2025-09-03, properties belong to the initial data source.
            "initial_data_source": {"properties": self._database_properties()},
        }
        data = self._request("POST", url, json=payload)
        database_id = data["id"]
        data_sources = data.get("data_sources") or []
        if not data_sources:
            raise RuntimeError("Created database has no data_sources in response.")
        data_source_id = data_sources[0]["id"]
        return database_id, data_source_id

    def retrieve_database(self, database_id: str) -> Dict[str, Any]:
        url = f"https://api.notion.com/v1/databases/{database_id}"
        return self._request("GET", url)

    def resolve_data_source_id(self, *, data_source_id: Optional[str], database_id: Optional[str]) -> str:
        """
        Resolve the data source to operate on.
        Priority:
          1) explicit data_source_id
          2) first data source under the given database_id (container)
        """
        if data_source_id:
            return normalize_uuid(data_source_id)
        if not database_id:
            raise RuntimeError("Provide datasource id or database id to resolve a data source.")
        db = self.retrieve_database(normalize_uuid(database_id))
        data_sources = db.get("data_sources") or []
        if not data_sources:
            raise RuntimeError("Database has no data_sources.")
        return normalize_uuid(data_sources[0]["id"])

    def _database_properties(self) -> Dict[str, Any]:
        return {
            "Title": {"title": {}},
            "Platform": {"select": {"options": [{"name": "Upwork", "color": "blue"}]}},
            "Status": {"select": {"options": [{"name": x, "color": c} for x, c in [
                ("pending", "blue"), ("未確認", "gray"), ("候補", "default"), ("応募予定", "yellow"), ("応募済み", "green"), ("見送り", "red")
            ]]}},
            "Budget Type": {"select": {"options": [{"name": "Fixed", "color": "purple"}, {"name": "Hourly", "color": "orange"}]}},
            "Budget": {"rich_text": {}},
            "Client": {"rich_text": {}},
            "Posted": {"rich_text": {}},
            "Description": {"rich_text": {}},
            "Video Meeting Detected": {"checkbox": {}},
            "Quick Win Candidate": {"checkbox": {}},
            "Proposal Floor": {"number": {"format": "number"}},
            "Skills": {"multi_select": {"options": [{"name": s, "color": "default"} for s in UpworkParser.SKILL_CANDIDATES]}},
            "Match Score": {"number": {"format": "number"}},
            "Priority": {"select": {"options": [{"name": "高", "color": "red"}, {"name": "中", "color": "yellow"}, {"name": "低", "color": "gray"}]}},
            "URL": {"url": {}},
            "Notes": {"rich_text": {}},
            "Proposal Seed": {"rich_text": {}},
            "Red Flag": {"checkbox": {}},
            "Skip Reason": {"rich_text": {}},
            "Proposals": {"rich_text": {}},
            "Job UID": {"rich_text": {}},
            "Search Position": {"number": {"format": "number"}},
            "Opening URL": {"url": {}},
            "Detail Page URL": {"url": {}},
            "Client Rating": {"number": {"format": "number"}},
            "Client Spent": {"rich_text": {}},
            "Client Location": {"rich_text": {}},
            "Est. Time": {"rich_text": {}},
            "Experience Level": {"rich_text": {}},
            "Source Type": {"select": {"options": [{"name": "Text", "color": "gray"}, {"name": "HTML", "color": "blue"}, {"name": "Text+HTML", "color": "green"}]}},
            "Raw HTML Snippet": {"rich_text": {}},
        }

    def query_data_source_by_job_uid(self, data_source_id: str, job_uid: str) -> Optional[Dict[str, Any]]:
        url = f"https://api.notion.com/v1/data_sources/{data_source_id}/query"
        payload = {
            "filter": {
                "property": "Job UID",
                "rich_text": {"equals": job_uid}
            },
            "page_size": 1,
        }
        data = self._request("POST", url, json=payload)
        results = data.get("results", [])
        return results[0] if results else None

    def create_page(self, data_source_id: str, record: JobRecord) -> Dict[str, Any]:
        url = "https://api.notion.com/v1/pages"
        payload = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": self._record_to_properties(record),
            "children": self._record_to_children(record),
        }
        return self._request("POST", url, json=payload)

    def update_page(self, page_id: str, record: JobRecord) -> Dict[str, Any]:
        url = f"https://api.notion.com/v1/pages/{page_id}"
        payload = {"properties": self._record_to_properties(record)}
        return self._request("PATCH", url, json=payload)

    # Notion rich_text: each text object "content" is limited to 2000 characters.
    _NOTION_RICH_TEXT_MAX = 2000

    def _rt(self, text: str) -> List[Dict[str, Any]]:
        text = text or ""
        if not text:
            return []
        # Notion validates this limit using UTF-16 code units (surrogate pairs count as 2).
        # Python's len() counts Unicode code points, so we must chunk by UTF-16 length.
        out: List[Dict[str, Any]] = []
        max_units = self._NOTION_RICH_TEXT_MAX

        start = 0
        current_units = 0
        for i, ch in enumerate(text):
            units = 2 if ord(ch) > 0xFFFF else 1
            if current_units + units > max_units:
                out.append({"type": "text", "text": {"content": text[start:i]}})
                start = i
                current_units = 0
            current_units += units

        if start < len(text):
            out.append({"type": "text", "text": {"content": text[start:]}})
        return out

    def _record_to_properties(self, record: JobRecord) -> Dict[str, Any]:
        props: Dict[str, Any] = {
            "Title": {"title": self._rt(record.title)},
            "Platform": {"select": {"name": record.platform}},
            "Status": {"select": {"name": record.status}},
            "Budget": {"rich_text": self._rt(record.budget)},
            "Client": {"rich_text": self._rt(record.client_summary)},
            "Posted": {"rich_text": self._rt(record.posted)},
            "Description": {"rich_text": self._rt(record.description)},
            "Video Meeting Detected": {"checkbox": record.video_meeting_detected},
            "Quick Win Candidate": {"checkbox": record.is_quick_win},
            "Proposal Floor": {"number": record.proposal_count_floor},
            "Match Score": {"number": record.match_score},
            "Priority": {"select": {"name": record.priority}},
            "Notes": {"rich_text": self._rt(record.notes)},
            "Proposal Seed": {"rich_text": self._rt(record.proposal_seed)},
            "Red Flag": {"checkbox": record.red_flag},
            "Skip Reason": {"rich_text": self._rt("")},
            "Proposals": {"rich_text": self._rt(record.proposals)},
            "Job UID": {"rich_text": self._rt(record.job_uid)},
            "Search Position": {"number": record.search_position},
            "Client Rating": {"number": record.client_rating},
            "Client Spent": {"rich_text": self._rt(record.client_spent)},
            "Client Location": {"rich_text": self._rt(record.client_location)},
            "Est. Time": {"rich_text": self._rt(record.est_time)},
            "Experience Level": {"rich_text": self._rt(record.experience_level)},
            "Source Type": {"select": {"name": record.source_type}},
            "Raw HTML Snippet": {"rich_text": self._rt(record.raw_html_snippet)},
            "URL": {"url": record.opening_url or None},
            "Opening URL": {"url": record.opening_url or None},
            "Detail Page URL": {"url": record.detail_page_url or None},
        }
        if record.budget_type:
            props["Budget Type"] = {"select": {"name": record.budget_type}}
        if record.skills:
            props["Skills"] = {"multi_select": [{"name": s} for s in record.skills]}
        return props

    def _record_to_children(self, record: JobRecord) -> List[Dict[str, Any]]:
        paragraphs = [
            f"Source Type: {record.source_type}",
            f"Description: {record.description[:1800]}" if record.description else "",
            f"Relevant Skills: {', '.join(record.skills)}" if record.skills else "",
        ]
        blocks = []
        for text in paragraphs:
            if text:
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": self._rt(text)},
                })
        return blocks


def collect_html_files(single_html: Optional[str], input_dir: Optional[str]) -> List[Path]:
    files: List[Path] = []
    if single_html:
        files.append(Path(single_html))
    if input_dir:
        for p in sorted(Path(input_dir).glob("*.html")):
            files.append(p)
    deduped: List[Path] = []
    seen = set()
    for f in files:
        if f.resolve() not in seen:
            deduped.append(f)
            seen.add(f.resolve())
    return deduped


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Upwork search-result HTML into Notion.")
    parser.add_argument("--html", help="Single html file to parse")
    parser.add_argument("--input-dir", help="Directory containing html files")
    parser.add_argument("--create-db", action="store_true", help="Create a new database under NOTION_PARENT_PAGE_ID")
    parser.add_argument("--database-id", help="Existing Notion database ID")
    parser.add_argument("--datasource-id", help="Existing Notion data source ID (preferred on Notion API 2025-09-03+)")
    parser.add_argument("--dry-run", action="store_true", help="Parse only; do not call Notion")
    parser.add_argument("--dump-json", help="Write parsed jobs to a JSON file")
    args = parser.parse_args()

    files = collect_html_files(args.html, args.input_dir)
    if not files:
        raise SystemExit("No HTML files found. Use --html or --input-dir.")

    parser_obj = UpworkParser()
    jobs: List[JobRecord] = []
    for f in files:
        parsed = parser_obj.parse_file(f)
        debug(f"Parsed {len(parsed)} jobs from {f.name}")
        jobs.extend(parsed)

    # de-dupe by Job UID, keep latest occurrence
    by_uid: Dict[str, JobRecord] = {}
    for job in jobs:
        by_uid[job.job_uid] = job
    jobs = list(by_uid.values())

    if args.dump_json:
        Path(args.dump_json).write_text(
            json.dumps([asdict(j) for j in jobs], ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    print(f"Parsed jobs: {len(jobs)}")
    for j in jobs:
        print(f"- {j.title} [{j.job_uid}]")

    if args.dry_run:
        return

    token = env_required("NOTION_TOKEN")
    notion = NotionClient(token)

    database_id = args.database_id or os.getenv("NOTION_DATABASE_ID")
    data_source_id = args.datasource_id or os.getenv("NOTION_DATASOURCE_ID")
    if args.create_db:
        parent_page_id = normalize_uuid(env_required("NOTION_PARENT_PAGE_ID"))
        created_database_id, created_data_source_id = notion.create_database(parent_page_id, DEFAULT_DB_TITLE)
        database_id = created_database_id
        data_source_id = created_data_source_id
        print(f"Created database: {database_id}")
        print(f"Created data source: {data_source_id}")
    elif data_source_id or database_id:
        data_source_id = notion.resolve_data_source_id(data_source_id=data_source_id, database_id=database_id)
    else:
        raise SystemExit("Provide --create-db or --datasource-id / NOTION_DATASOURCE_ID or --database-id / NOTION_DATABASE_ID")

    created = 0
    updated = 0
    skipped = 0
    for job in jobs:
        existing = notion.query_data_source_by_job_uid(data_source_id, job.job_uid)
        if existing:
            skipped += 1
            print(f"Skipped (already exists): {job.title}")
        else:
            notion.create_page(data_source_id, job)
            created += 1
            print(f"Created: {job.title}")
        time.sleep(0.25)

    print(f"Done. created={created}, updated={updated}, skipped={skipped}, data_source_id={data_source_id}")


if __name__ == "__main__":
    main()
