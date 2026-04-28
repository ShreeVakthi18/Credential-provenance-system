"""
cert_checker.py — Resume Verification Engine (Production)
==========================================================
Multi-layer evidence ranking:
  1. Local PDF proof  (strongest)
  2. Link / GitHub verification (strong)
  3. DuckDuckGo plausibility search (weak)
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

import pdfplumber
import requests
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
from fpdf import FPDF
from fuzzywuzzy import fuzz

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("cert_checker")


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


TEXT_MATCH_THRESHOLD   = _int_env("RESUME_TEXT_MATCH_THRESHOLD",   75)
ONLINE_TITLE_THRESHOLD = _int_env("RESUME_ONLINE_TITLE_THRESHOLD", 62)
ONLINE_BODY_THRESHOLD  = _int_env("RESUME_ONLINE_BODY_THRESHOLD",  58)
GITHUB_REPO_THRESHOLD  = _int_env("RESUME_GITHUB_REPO_THRESHOLD",  45)
DDG_MAX_RESULTS        = _int_env("RESUME_DDG_MAX_RESULTS",         5)
REQUESTS_TIMEOUT       = _int_env("RESUME_REQUESTS_TIMEOUT",        8)

HTTP_HEADERS = {"User-Agent": "ResumeVerifier/2.0 (+https://github.com/resumeverify)"}


# ---------------------------------------------------------------------------
# CLAIM TYPES
# ---------------------------------------------------------------------------

class ClaimType:
    CERTIFICATION = "certification"
    PROJECT       = "project"
    INTERNSHIP    = "internship"
    EDUCATION     = "education"
    UNKNOWN       = "unknown"


CERT_KEYWORDS  = {"certif", "course", "program", "completed", "awarded",
                  "trained", "diploma", "license", "credential", "badge"}
PROJECT_KW     = {"project", "built", "developed", "created", "designed",
                  "deployed", "implemented", "engineered", "launched", "automated"}
INTERN_KW      = {"internship", "intern", "interned", "trainee", "apprentice"}
EDU_KW         = {"bachelor", "master", "phd", "degree", "btech", "mtech",
                  "b.sc", "m.sc", "university", "college", "graduated", "gpa", "cgpa"}
ALL_TRIGGER_KW = CERT_KEYWORDS | PROJECT_KW | INTERN_KW | EDU_KW

SKIP_PATTERNS  = {"http://", "https://", "www.", "view certificate",
                  "issued to", "issued by", "linkedin.com/in/"}

# Minimal noise words — only grammatical glue, preserve course/company names
NOISE_WORDS = {
    "at", "with", "in", "of", "the", "a", "an", "from", "by",
    "for", "as", "and", "or",
}

# Section headers to skip — both simple and compound
SECTION_HEADERS = {
    "certifications", "certification", "projects", "project",
    "education", "experience", "internships", "internship",
    "skills", "achievements", "awards", "summary", "objective",
    "work experience", "academic projects", "technical skills",
    "extra curricular", "extracurricular", "activities", "languages",
    "publications", "references", "hobbies", "interests",
    "internship experience", "internship experiences",
    "professional experience", "project experience", "academic experience",
    "certification & courses", "certifications & courses",
    "education & qualifications", "personal details", "contact",
    "core competencies", "key skills", "career objective",
}


# ---------------------------------------------------------------------------
# DATA MODEL
# ---------------------------------------------------------------------------

@dataclass
class Claim:
    text:   str
    type:   str
    result: str = "pending"
    reason: str = ""

    def label(self) -> str:
        return f"[{self.type.upper()}] {self.text}"


# ---------------------------------------------------------------------------
# UNIFIED NORMALIZE
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Normalize unicode, lowercase, strip — applied identically to claim AND proof."""
    return _clean_text(text).lower().strip()


# ---------------------------------------------------------------------------
# CLAIM CLASSIFICATION
# ---------------------------------------------------------------------------

def classify_claim(line: str) -> str:
    ll = line.lower()
    if any(k in ll for k in INTERN_KW):
        return ClaimType.INTERNSHIP
    if any(k in ll for k in EDU_KW):
        return ClaimType.EDUCATION
    if any(k in ll for k in CERT_KEYWORDS):
        return ClaimType.CERTIFICATION
    if any(k in ll for k in PROJECT_KW):
        return ClaimType.PROJECT
    return ClaimType.UNKNOWN


# ---------------------------------------------------------------------------
# TEXT EXTRACTION
# ---------------------------------------------------------------------------

def extract_text_from_pdf(pdf_path: str) -> str:
    text_parts: list[str] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        total = sum(len(t) for t in text_parts)
        log.info("Extracted %d chars from %s", total, os.path.basename(pdf_path))
    except Exception as exc:
        log.error("Could not read PDF %s: %s", pdf_path, exc)
    return "\n".join(text_parts)


# ---------------------------------------------------------------------------
# SECTION HEADER DETECTION
# ---------------------------------------------------------------------------

def _is_section_header(line: str) -> bool:
    """Return True if line looks like a section heading, not a real claim."""
    normalized = line.lower().rstrip(":").strip()

    # Exact match in known headers set
    if normalized in SECTION_HEADERS:
        return True

    # Title-case ≤4 words with no digits
    words = line.split()
    if len(words) <= 4 and not any(ch.isdigit() for ch in line):
        if line.rstrip(":").strip().istitle():
            return True

    # ALL CAPS short label
    if line.isupper() and len(words) <= 5:
        return True

    return False


# ---------------------------------------------------------------------------
# CLAIM EXTRACTION
# ---------------------------------------------------------------------------

def extract_claims(text: str) -> list[Claim]:
    lines = text.split("\n")
    seen:   set[str] = set()
    claims: list[Claim] = []

    for raw in lines:
        line = raw.strip()

        # Strip bullet/dash prefixes
        line = re.sub(r"^[\-\u2022\u25cf\u25aa\u25e6*]\s*", "", line).strip()

        if not (10 <= len(line) <= 250):
            continue

        if _is_section_header(line):
            log.debug("Skipping section header: %r", line)
            continue

        # Skip pure label lines  e.g. "Technical Skills:"
        if re.match(r"^[A-Za-z\s]{3,40}:\s*$", line):
            continue

        ll = line.lower()
        if any(skip in ll for skip in SKIP_PATTERNS):
            continue
        if not any(kw in ll for kw in ALL_TRIGGER_KW):
            continue

        cleaned = _clean_text(line)
        if len(cleaned.strip()) < 10 or cleaned in seen:
            continue

        seen.add(cleaned)
        claims.append(Claim(text=cleaned, type=classify_claim(cleaned)))

    log.info("Extracted %d claims from resume", len(claims))
    for c in claims:
        log.debug("  [%s] %s", c.type.upper(), c.text)
    return claims


# ---------------------------------------------------------------------------
# STRONG MATCH — single fuzz.partial_ratio strategy
# ---------------------------------------------------------------------------

def _strong_match(claim_text: str, proof_text: str, threshold: int = TEXT_MATCH_THRESHOLD) -> bool:
    norm_claim = normalize(claim_text)
    norm_proof = normalize(proof_text)
    score = fuzz.partial_ratio(norm_claim, norm_proof)
    log.debug("partial_ratio=%d for '%s'", score, norm_claim[:60])
    return score >= threshold


def _extract_core_keywords(text: str) -> str:
    words = re.sub(r"[^a-z0-9\s]", " ", text.lower()).split()
    return " ".join(w for w in words if w not in NOISE_WORDS and len(w) > 2)


def _strip_candidate_name(text: str, name: str) -> str:
    result = text.lower()
    for part in name.lower().split():
        if len(part) > 2:
            result = re.sub(rf"\b{re.escape(part)}\b", "", result)
    return result.strip()


# ---------------------------------------------------------------------------
# INTERNSHIP SEMANTIC MATCH
# ---------------------------------------------------------------------------

def _internship_semantic_match(claim: Claim, proof_texts: list[str]) -> bool:
    """
    Relaxed internship matching:
    intern keyword signal + >=2 meaningful claim words found in proof.
    Handles OCR noise, reordering, and verbose certificate language.
    """
    claim_norm  = normalize(claim.text)
    claim_words = [w for w in claim_norm.split() if w not in NOISE_WORDS and len(w) > 3]

    for proof in proof_texts:
        proof_norm = normalize(proof)

        has_intern = any(kw in proof_norm for kw in ("intern", "internship", "trainee", "apprentice"))
        if not has_intern:
            continue

        matched = [w for w in claim_words if w in proof_norm]
        if len(matched) >= 2:
            log.debug("Internship semantic match: words=%s", matched[:5])
            return True

        # Fallback: strong fuzzy
        if _strong_match(claim.text, proof):
            return True

    return False


# ---------------------------------------------------------------------------
# LAYER 1 — LOCAL PDF PROOF
# ---------------------------------------------------------------------------

def match_claim_in_proofs(claim: Claim, proof_texts: list[str], name: str = "") -> bool:
    if not proof_texts:
        return False

    log.info("PDF CONTENT SAMPLE:\n%s", proof_texts[0][:1000])

    if claim.type == ClaimType.INTERNSHIP:
        return _internship_semantic_match(claim, proof_texts)

    for proof in proof_texts:
        if _strong_match(claim.text, proof):
            log.debug("Strong match for '%s'", claim.text[:60])
            return True

    return False


# ---------------------------------------------------------------------------
# LAYER 2a — CERTIFICATE / VERIFICATION LINKS
# ---------------------------------------------------------------------------

def verify_claim_via_link(claim: Claim, cert_links: list[str], name: str = "") -> bool:
    if not cert_links:
        return False

    name_parts = [p for p in name.lower().split() if len(p) > 2]

    for url in cert_links:
        if not url.startswith("http"):
            continue
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUESTS_TIMEOUT)
            if resp.status_code != 200:
                log.debug("Non-200 from %s (%d)", url, resp.status_code)
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "meta", "head", "noscript"]):
                tag.decompose()
            page_text = soup.get_text(separator=" ", strip=True)

            keywords_found = _strong_match(claim.text, page_text)
            name_found     = any(part in normalize(page_text) for part in name_parts)

            if keywords_found and name_found:
                log.debug("Link verified '%s' at %s", claim.text[:60], url)
                return True

            if keywords_found and claim.type in (ClaimType.CERTIFICATION, ClaimType.PROJECT):
                log.debug("Link partial match for '%s' at %s", claim.text[:60], url)
                return True

            if claim.type == ClaimType.INTERNSHIP:
                if _internship_semantic_match(claim, [page_text]):
                    log.debug("Internship link match for '%s' at %s", claim.text[:60], url)
                    return True

        except requests.RequestException as exc:
            log.debug("Could not fetch %s: %s", url, exc)

    return False


# ---------------------------------------------------------------------------
# LAYER 2b — GITHUB REPO VERIFICATION
# ---------------------------------------------------------------------------

def verify_github_link(claim: Claim, cert_links: list[str]) -> bool:
    keywords = _extract_core_keywords(claim.text.lower())

    for url in cert_links:
        if "github.com" not in url:
            continue
        m = re.search(r"github\.com/([^/\s]+)/([^/?\s#]+)", url)
        if not m:
            continue

        owner, repo = m.group(1), m.group(2).rstrip("/")
        repo_readable = repo.replace("-", " ").replace("_", " ").lower()

        if fuzz.token_set_ratio(keywords, repo_readable) < GITHUB_REPO_THRESHOLD:
            continue

        api_url = f"https://api.github.com/repos/{owner}/{repo}/commits?per_page=1"
        try:
            resp = requests.get(api_url, headers=HTTP_HEADERS, timeout=REQUESTS_TIMEOUT)
            if resp.status_code == 200 and resp.json():
                log.debug("GitHub repo %s/%s verified.", owner, repo)
                return True
        except requests.RequestException as exc:
            log.debug("GitHub API error for %s/%s: %s", owner, repo, exc)

    return False


# ---------------------------------------------------------------------------
# LAYER 3 — ONLINE PLAUSIBILITY SEARCH (DuckDuckGo)
# ---------------------------------------------------------------------------

def search_online_for_claim(claim: Claim) -> bool:
    query = claim.text

    if claim.type == ClaimType.INTERNSHIP:
        m = re.search(
            r"(?:at|with)\s+([A-Za-z0-9\s&.]+?)(?:\s+as|\s+for|\s+during|$)",
            query, re.IGNORECASE
        )
        if m:
            query = m.group(1).strip() + " company official site"
    elif claim.type == ClaimType.EDUCATION:
        query = claim.text + " university official"

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=DDG_MAX_RESULTS))

        claim_lower = claim.text.lower()
        for result in results:
            title_sim = fuzz.partial_ratio(claim_lower, result.get("title", "").lower())
            body_sim  = fuzz.partial_ratio(claim_lower, result.get("body",  "").lower())
            if title_sim >= ONLINE_TITLE_THRESHOLD or body_sim >= ONLINE_BODY_THRESHOLD:
                log.debug("Online match for '%s' (title=%d, body=%d)",
                          claim.text[:60], title_sim, body_sim)
                return True

        return False

    except Exception as exc:
        log.error("DDG search failed for '%s': %s", query[:80], exc)
        return False


# ---------------------------------------------------------------------------
# DECISION ENGINE
# ---------------------------------------------------------------------------

def verify_claim(
    claim: Claim,
    proof_texts: list[str],
    cert_links: list[str],
    name: str,
) -> Claim:
    ctype = claim.type

    def _set(result: str, reason: str) -> Claim:
        claim.result = result
        claim.reason = reason
        return claim

    if ctype == ClaimType.PROJECT:
        if verify_github_link(claim, cert_links):
            return _set("verified", "GitHub repository confirmed with commits")
        if verify_claim_via_link(claim, cert_links, name):
            return _set("verified", "Verification link confirmed")
        if match_claim_in_proofs(claim, proof_texts, name):
            return _set("verified", "Matched in uploaded proof document")

    elif ctype in (ClaimType.CERTIFICATION, ClaimType.EDUCATION):
        if match_claim_in_proofs(claim, proof_texts, name):
            return _set("verified", "Matched in uploaded proof document")
        if verify_claim_via_link(claim, cert_links, name):
            return _set("verified", "Verification link confirmed")

    elif ctype == ClaimType.INTERNSHIP:
        if match_claim_in_proofs(claim, proof_texts, name):
            return _set("verified", "Matched in offer/completion letter")
        if verify_claim_via_link(claim, cert_links, name):
            return _set("verified", "Verification link confirmed")

    else:
        if match_claim_in_proofs(claim, proof_texts, name):
            return _set("verified", "Matched in uploaded proof document")

    if search_online_for_claim(claim):
        return _set("unknown", "Exists online but no personal proof submitted")

    return _set("suspicious", "No evidence found locally or online")


# ---------------------------------------------------------------------------
# TEXT CLEANING UTILITY
# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")

    replacements = {
        "\u2022": "-", "\u25cf": "-", "\u25aa": "-", "\u25e6": "-",
        "\u2013": "-", "\u2014": "--",
        "\u2018": "'", "\u2019": "'",
        "\u201c": '"', "\u201d": '"',
        "\u2026": "...",
    }
    for orig, rep in replacements.items():
        text = text.replace(orig, rep)

    emoji_re = re.compile(
        "["
        "\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251"
        "\U0001F926-\U0001F937\U00002600-\U000026FF\U0000FE00-\U0000FE0F"
        "\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0000200D\U000020E3"
        "]+",
        flags=re.UNICODE,
    )
    text = emoji_re.sub("", text)
    return "".join(ch if ord(ch) < 256 else "-" for ch in text)


# ---------------------------------------------------------------------------
# PDF REPORT GENERATION
# ---------------------------------------------------------------------------

def generate_pdf_report(
    report_path: str,
    name: str,
    email: str,
    trust_score: int,
    verified:   list[str],
    unknown:    list[str],
    suspicious: list[str],
    claims_detail: Optional[list[Claim]] = None,
) -> None:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, "Resume Verification Report", ln=True, align="C")
    pdf.set_draw_color(30, 78, 216)
    pdf.set_line_width(0.5)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)

    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, _clean_text(f"Name:   {name}"),  ln=True)
    pdf.cell(0, 8, _clean_text(f"Email:  {email}"), ln=True)
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(30, 78, 216)
    pdf.cell(0, 10, _clean_text(f"Trust Score: {trust_score}%"), ln=True)
    pdf.set_text_color(0, 0, 0)
    total = len(verified) + len(unknown) + len(suspicious)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, _clean_text(
        f"Based on {total} extracted claims — "
        f"{len(verified)} verified, {len(unknown)} unverifiable, {len(suspicious)} suspicious"
    ), ln=True)
    pdf.ln(6)

    def write_section(title: str, items: list[str], color: tuple) -> None:
        r, g, b = color
        pdf.set_fill_color(r, g, b)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 9, _clean_text(title), ln=True, fill=True)
        pdf.set_font("Helvetica", "", 10)
        if items:
            for item in items:
                content = _clean_text(item.replace("\n", " ").strip())
                if content:
                    pdf.multi_cell(0, 7, f"  {content}")
                    pdf.ln(1)
        else:
            pdf.cell(0, 7, "  None in this category.", ln=True)
        pdf.ln(4)

    write_section("Verified Claims",                   verified,   (220, 252, 231))
    write_section("Could Not Verify (Unknown)",        unknown,    (254, 249, 195))
    write_section("Suspicious / Unverifiable Claims",  suspicious, (254, 226, 226))

    if claims_detail:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 9, "Detailed Claim Breakdown", ln=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.ln(2)
        for c in claims_detail:
            icon = {"verified": "[OK]", "unknown": "[?]", "suspicious": "[!]"}.get(c.result, "[?]")
            pdf.multi_cell(0, 6, _clean_text(f"{icon} [{c.type.upper()}] {c.text}"))
            if c.reason:
                pdf.set_text_color(100, 100, 100)
                pdf.multi_cell(0, 5, _clean_text(f"       Reason: {c.reason}"))
                pdf.set_text_color(0, 0, 0)
            pdf.ln(2)

    try:
        pdf.output(report_path)
        log.info("PDF report written to %s", report_path)
    except Exception as exc:
        log.error("FPDF output failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def process_resume(
    name:        str,
    email:       str,
    resume_path: str,
    proof_paths: list[str],
    cert_links:  list[str],
) -> tuple[str, str, int, list[str], list[str], list[str]]:
    log.info("Starting verification for %s <%s>", name, email)

    resume_text = extract_text_from_pdf(resume_path)
    if not resume_text.strip():
        raise ValueError(
            "Could not extract any text from the resume PDF. "
            "Is it scanned/image-only? Try a text-based PDF."
        )

    claims = extract_claims(resume_text)
    if not claims:
        raise ValueError(
            "No verifiable claims found in the resume. "
            "Ensure it contains project/certification/internship details."
        )

    proof_texts: list[str] = []
    for path in proof_paths:
        if path.lower().endswith(".pdf"):
            t = extract_text_from_pdf(path)
            if t.strip():
                proof_texts.append(t)
            else:
                log.warning("Proof file %s yielded no text (scanned?)", os.path.basename(path))

    verified_list:   list[str] = []
    unknown_list:    list[str] = []
    suspicious_list: list[str] = []
    verified_claims: list[Claim] = []

    for claim in claims:
        verify_claim(claim, proof_texts, cert_links, name=name)
        log.info("  -> %-12s %s", claim.result.upper(), claim.text[:80])
        verified_claims.append(claim)

        label = claim.label()
        if claim.result == "verified":
            verified_list.append(label)
        elif claim.result == "unknown":
            unknown_list.append(label)
        else:
            suspicious_list.append(label)

    total = len(claims)
    trust_score = int(len(verified_list) / total * 100) if total > 0 else 0
    log.info("Trust Score: %d%% (%d/%d verified)", trust_score, len(verified_list), total)

    safe_name       = re.sub(r"[^A-Za-z0-9_]", "_", name)
    report_filename = f"report_{safe_name}.pdf"
    reports_dir     = os.environ.get("RESUME_REPORTS_DIR", "reports")
    os.makedirs(reports_dir, exist_ok=True)
    report_path     = os.path.join(reports_dir, report_filename)

    generate_pdf_report(
        report_path, name, email, trust_score,
        verified_list, unknown_list, suspicious_list,
        claims_detail=verified_claims,
    )

    if not os.path.exists(report_path) or os.path.getsize(report_path) == 0:
        raise RuntimeError(f"PDF report not created or is empty: {report_path}")

    return report_path, report_filename, trust_score, verified_list, unknown_list, suspicious_list