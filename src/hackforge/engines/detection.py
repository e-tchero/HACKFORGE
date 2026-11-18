"""Opportunity detection and field extraction (Phase 1).

Detection is deliberately separate from extraction:

* ``Detector`` decides *whether* a message is an opportunity and what kind.
* ``FieldExtractor`` pulls structured fields out of a message it has already
  accepted.

Both are deterministic and dependency-free so behaviour is testable. Anything
that cannot be established is left ``None``; nothing is guessed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..adapters.sources import RawMessage
from ..core.models import (
    EvidenceRef,
    EvidenceState,
    Opportunity,
    OpportunityStatus,
    Provenance,
    SourceRef,
    Timeline,
    now_iso,
)

# Weighted keywords per opportunity type. Weight reflects specificity: a
# multi-word phrase is far stronger evidence than a single common word.
TYPE_SIGNALS: dict[str, list[tuple[str, float]]] = {
    "hackathon": [
        ("hackathon", 3.0),
        ("24 hour", 2.0),
        ("24-hour", 2.0),
        ("build period", 1.5),
        ("team of", 1.0),
        ("submit your project", 2.0),
        ("project submission", 2.0),
        ("devpost", 2.5),
        ("judging criteria", 1.5),
        ("participant registration", 1.0),
    ],
    "competition": [
        ("competition", 3.0),
        ("contest", 2.5),
        ("challenge", 1.5),
        ("prize pool", 2.0),
        ("participants will be judged", 1.5),
        ("register for the competition", 1.5),
    ],
    "scholarship": [
        ("scholarship", 3.5),
        ("financial aid", 2.0),
        ("tuition", 1.5),
        ("fellowship", 2.0),
        ("award of", 1.0),
    ],
    "internship": [
        ("internship", 3.5),
        ("summer analyst", 2.0),
        ("intern cohort", 2.0),
        ("recruiting", 1.0),
        ("application received", 1.5),
    ],
    "grant": [
        ("grant application", 3.0),
        ("request for proposals", 2.5),
        ("rfp", 2.0),
        ("proposal deadline", 2.0),
        ("funding opportunity", 2.0),
    ],
    "accelerator": [
        ("accelerator", 3.0),
        ("cohort", 1.5),
        ("seed round", 1.5),
        ("investment term sheet", 2.0),
        ("demo day", 2.0),
    ],
    "developer_program": [
        ("developer program", 3.0),
        ("ambassador program", 3.0),
        ("advocate program", 2.5),
        ("early access program", 2.0),
    ],
    "research": [
        ("research opportunity", 3.0),
        ("phd position", 3.0),
        ("postdoctoral", 3.0),
        ("principal investigator", 2.0),
        ("call for papers", 3.0),
    ],
    "startup": [
        ("startup competition", 3.0),
        ("pitch competition", 3.0),
        ("foundry", 2.0),
    ],
}

# Signals that confirm the user already registered, not just heard about it.
REGISTRATION_CONFIRMED_SIGNALS = [
    ("you are registered", 3.0),
    ("you're registered", 3.0),
    ("registration confirmed", 3.0),
    ("registration is confirmed", 3.0),
    ("registration complete", 3.0),
    ("successfully registered", 3.0),
    ("your registration", 2.0),
    ("thank you for registering", 3.0),
    ("registration id", 2.0),
    ("you have been registered", 3.0),
    ("seat is reserved", 2.0),
    ("spot is confirmed", 2.0),
    ("we have registered you", 3.0),
]

ANNOUNCEMENT_SIGNALS = [
    ("registration is now open", 2.0),
    ("applications are open", 2.0),
    ("call for applications", 2.0),
    ("register now", 1.5),
    ("join us", 1.0),
    ("save the date", 2.0),
    ("announcing", 1.5),
]

ORGANIZER_STOPWORDS = {
    "the", "team", "hello", "hi", "no-reply", "noreply", "notifications",
    "notification", "support", "info", "events", "event", "admin", "mail",
    "do-not-reply", "donotreply", "updates", "news", "newsletter",
}

DISQUALIFIERS = [
    "unsubscribe",
    "your receipt",
    "invoice",
    "password reset",
    "security alert",
    "your order has shipped",
    "promotion",
]


@dataclass
class DetectionResult:
    """Why the detector decided what it decided. Retained for traceability."""

    is_opportunity: bool
    opportunity_type: str
    confidence: float
    registered: bool
    reasons: list[str]
    scores: dict[str, float]


class Detector:
    """Scores a message against the opportunity type signals."""

    threshold = 2.5

    def classify(self, message: RawMessage) -> DetectionResult:
        text = message.searchable_text
        lowered_subject = message.subject.lower()

        for term in DISQUALIFIERS:
            if term in lowered_subject:
                return DetectionResult(False, "none", 0.0, False, [f"disqualifier: {term}"], {})

        scores: dict[str, float] = {}
        reasons: list[str] = []

        for opportunity_type, signals in TYPE_SIGNALS.items():
            score = 0.0
            for term, weight in signals:
                if term in text:
                    score += weight
                    if term in lowered_subject:
                        score += weight * 0.5
                        reasons.append(f"{opportunity_type}: '{term}' in subject")
                    else:
                        reasons.append(f"{opportunity_type}: '{term}' in body")
            if score > 0:
                scores[opportunity_type] = round(score, 2)

        if not scores:
            return DetectionResult(False, "none", 0.0, False, ["no opportunity signals"], {})

        best_type = max(scores, key=lambda key: scores[key])
        best_score = scores[best_type]

        if best_score < self.threshold:
            return DetectionResult(
                False,
                "none",
                round(best_score / self.threshold, 2),
                False,
                reasons + [f"score {best_score} below threshold {self.threshold}"],
                scores,
            )

        registered, reg_reasons = self._detect_registration(text)
        reasons.extend(reg_reasons)
        reasons.append(f"classified '{best_type}' at {best_score} (threshold {self.threshold})")

        return DetectionResult(
            True,
            best_type,
            round(min(best_score / (self.threshold * 2), 1.0), 2),
            registered,
            reasons,
            scores,
        )

    @staticmethod
    def _detect_registration(text: str) -> tuple[bool, list[str]]:
        score = 0.0
        reasons: list[str] = []
        for phrase, weight in REGISTRATION_CONFIRMED_SIGNALS:
            if phrase in text:
                score += weight
                reasons.append(f"registration signal: '{phrase}'")
        if score >= 2.0:
            return True, reasons
        if score > 0:
            reasons.append("registration signals present but below confirmation threshold")
        else:
            reasons.append("no registration confirmation found")
        return False, reasons

    @staticmethod
    def detect_announcement(text: str) -> tuple[bool, list[str]]:
        score = 0.0
        matched: list[str] = []
        for phrase, weight in ANNOUNCEMENT_SIGNALS:
            if phrase in text:
                score += weight
                matched.append(phrase)
        return score >= 2.0, matched


_DATE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
    (
        re.compile(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?"
            r"(january|february|march|april|may|june|july|august|september|october|"
            r"november|december)\.?,?\s*(\d{4})?\b",
            re.IGNORECASE,
        ),
        "dmy",
    ),
    (
        re.compile(
            r"\b(january|february|march|april|may|june|july|august|september|october|"
            r"november|december)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?\b",
            re.IGNORECASE,
        ),
        "mdy",
    ),
]

_MONTHS = {
    name.lower(): index
    for index, name in enumerate(
        [
            "january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december",
        ],
        start=1,
    )
}
_MONTHS.update({key[:3]: value for key, value in list(_MONTHS.items())})

SUBMISSION_DEADLINE_HINTS = [
    "submission deadline", "submit by", "deadline to submit", "final submission",
    "project submission", "submission closes", "submissions close", "due by",
    "deadline:", "last day to submit",
]
REGISTRATION_DEADLINE_HINTS = [
    "registration deadline", "register by", "registration closes",
    "apply by", "applications close", "registration ends",
]
EVENT_DATE_HINTS = [
    "event date", "finals", "finale", "demo day", "happens on", "takes place on",
    "awards ceremony", "pitching",
]
BUILD_START_HINTS = [
    "build period", "hackathon starts", "starts on", "beginning", "kick off",
    "kickoff", "development starts",
]

URL_RE = re.compile(r"https?://[^\s<>\"')]+")

# Marketing email is full of infrastructure links that are never the organiser's
# site: XML DTDs, schema locations, tracking pixels, unsubscribe endpoints and
# tag/asset hosts. Taking the first link blindly records one of these as the
# "official website", which is worse than recording none.
_NON_SITE_HOSTS = (
    "w3.org",
    "schemas.org",
    "schema.org",
    "purl.org",
    "xmlsoap.org",
    "openxmlformats.org",
    "googleapis.com",
    "gstatic.com",
    "google.com",
    "doubleclick.net",
    "googletagmanager.com",
    "google-analytics.com",
    "list-manage.com",
    "mailchimp.com",
    "sendgrid.net",
    "mandrillapp.com",
    "mailgun.org",
    # Marketing automation and collaboration links that appear early in the
    # HTML but are never the organiser's site.
    "sendibm1.com",
    "vialoops.com",
    "salesforce.com",
    "exacttarget.com",
    "marketo.net",
    "hubspot.com",
    "slack.com",
    "linkedin.com",
    "lnkd.in",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
)
# Asset extensions that are never a landing page.
_ASSET_SUFFIX_RE = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|css|js|mjs|ico|woff2?|ttf|eot|otf|mp4|webm|pdf|zip)(\?|$)",
    re.IGNORECASE,
)
_NON_SITE_PATH_RE = re.compile(
    r"/(unsubscribe|optout|opt-out|preferences|manage|track|open|click|px)"
    r"|/trk/|/tf-|/ss/|/CI0/|/mk/|/wf/",
    re.IGNORECASE,
)


class FieldExtractor:
    """Pulls structured fields from an accepted message.

    Year inference is deliberately restricted: a date without a year is only
    accepted when it is unambiguous given the message date (same year, or
    December leading into January of the next year). Otherwise it stays None.
    """

    def __init__(self, reference_year: date | None = None) -> None:
        self._reference_date = reference_year

    def extract_name(self, message: RawMessage) -> str:
        subject = (message.subject or "").strip()
        cleaned = re.sub(
            r"^\s*(re|fw|fwd|forward)\s*[:\-]\s*", "", subject, flags=re.IGNORECASE
        )
        # Confirmation framing usually wraps the real name: strip it from either end.
        framing = (
            r"(registration confirmed|registration complete|you are registered|"
            r"you're registered|successfully registered|thank you for registering|"
            r"your registration|registration receipt|confirmed|receipt|invitation|"
            r"invite|welcome)"
        )
        cleaned = re.sub(rf"^\s*{framing}\s*[:\-–—]\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(
            rf"\s*[-–—]\s*({framing})[\s.!]*$", "", cleaned, flags=re.IGNORECASE
        ).strip()
        if cleaned:
            return self._smart_case(cleaned)
        if message.sender_name:
            return message.sender_name.strip()
        return self._smart_case(message.sender.split("@")[0]) if message.sender else "unknown"

    @staticmethod
    def _smart_case(text: str) -> str:
        """Preserve intentional acronyms (AI, API, ML, IoT) and existing casing."""

        known_acronyms = {
            "ai", "api", "ml", "iot", "ui", "ux", "sdk", "web3", "llm", "gpu",
            "html", "css", "vr", "ar", "nlp", "ocr", "sql", "http", "qa",
        }
        words = []
        for word in text.split():
            bare = word.strip(".,:;!?()[]").lower()
            if bare in known_acronyms:
                words.append(word.strip(".,:;!?()[]").upper() + word[len(word.strip(".,:;!?()[]")):])
            elif word.isupper() and len(word) > 2:
                words.append(word)
            elif any(char.isupper() for char in word):
                words.append(word)
            else:
                words.append(word.capitalize())
        return " ".join(words)

    def extract_organizer(self, message: RawMessage) -> str | None:
        if message.sender_name:
            return message.sender_name.strip()
        match = re.match(r"^[^\s@]+@([^\s@]+)$", message.sender or "")
        if match:
            domain = match.group(1).split(".")[0]
            if domain.lower() in ORGANIZER_STOPWORDS:
                return None
            return self._smart_case(domain)
        return None

    def extract_url(self, message: RawMessage) -> str | None:
        links = URL_RE.findall(message.html_body or message.body)
        for link in links:
            cleaned = link.rstrip(".,);")
            if re.search(r"(unsubscribe|mailto:|preferences)", cleaned, re.IGNORECASE):
                continue
            if not self._is_site_url(cleaned):
                continue
            return cleaned
        return None

    @staticmethod
    def _is_site_url(url: str) -> bool:
        """True when a URL could plausibly be the organiser's own website."""

        host = url.split("://", 1)[-1].split("/", 1)[0].split("@")[-1].split(":")[0]
        if not host or "." not in host:
            return False
        if any(host == bad or host.endswith("." + bad) for bad in _NON_SITE_HOSTS):
            return False
        if _ASSET_SUFFIX_RE.search(url):
            return False
        return not _NON_SITE_PATH_RE.search(url)

    def extract_dates(
        self, message: RawMessage
    ) -> tuple[dict[str, date | None], list[str]]:
        """Return ``(timeline_fields, reasons)``. Missing fields are ``None``."""
        lines = message.body.splitlines()
        fields: dict[str, date | None] = {
            "submission_deadline": None,
            "registration_deadline": None,
            "event_date": None,
            "build_start": None,
        }
        reasons: list[str] = []

        for line in lines:
            lowered = line.lower()
            parsed = self._find_date(line)
            if parsed is None:
                continue
            if fields["submission_deadline"] is None and any(
                hint in lowered for hint in SUBMISSION_DEADLINE_HINTS
            ):
                fields["submission_deadline"] = parsed
                reasons.append(f"submission_deadline={parsed} from: {line.strip()[:90]}")
            elif fields["registration_deadline"] is None and any(
                hint in lowered for hint in REGISTRATION_DEADLINE_HINTS
            ):
                fields["registration_deadline"] = parsed
                reasons.append(f"registration_deadline={parsed} from: {line.strip()[:90]}")
            elif fields["event_date"] is None and any(
                hint in lowered for hint in EVENT_DATE_HINTS
            ):
                fields["event_date"] = parsed
                reasons.append(f"event_date={parsed} from: {line.strip()[:90]}")
            elif fields["build_start"] is None and any(
                hint in lowered for hint in BUILD_START_HINTS
            ):
                fields["build_start"] = parsed
                reasons.append(f"build_start={parsed} from: {line.strip()[:90]}")

        if all(value is None for value in fields.values()):
            reasons.append(
                "no labelled deadline found in message body; leaving dates UNKNOWN"
            )
        return fields, reasons

    def _find_date(self, text: str) -> date | None:
        for pattern, kind in _DATE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            if kind == "ymd":
                try:
                    return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                except ValueError:
                    continue
            if kind == "dmy":
                day_token, month_token, year_token = match.groups()
            else:
                month_token, day_token, year_token = match.groups()
            month = _MONTHS.get(month_token.lower().rstrip("."))
            if month is None:
                continue
            try:
                day = int(day_token)
            except ValueError:
                continue
            year = self._resolve_year(year_token, month, day)
            if year is None:
                continue
            try:
                return date(year, month, day)
            except ValueError:
                continue
        return None

    def _resolve_year(
        self, year_token: str | None, month: int, day: int
    ) -> int | None:
        if year_token:
            try:
                return int(year_token)
            except ValueError:
                return None
        reference = self._reference_date
        if reference is None:
            # No reference point available: a bare "March 15" cannot be dated
            # honestly, so we decline rather than guess.
            return None
        candidate = date(reference.year, month, day)
        if candidate < reference:
            try:
                candidate = date(reference.year + 1, month, day)
            except ValueError:
                return None
        return candidate.year

    def extract_technologies(self, message: RawMessage) -> list[str]:
        known = [
            "python", "javascript", "typescript", "react", "next.js", "vue", "svelte",
            "flutter", "dart", "swift", "kotlin", "android", "ios", "node.js", "django",
            "flask", "fastapi", "rust", "go", "golang", "java", "c++", "unity", "unreal",
            "tensorflow", "pytorch", "opencv", "blockchain", "solidity", "web3", "aws",
            "gcp", "azure", "firebase", "supabase", "mongodb", "postgres", "mysql",
            "redis", "docker", "kubernetes", "llm", "openai", "langchain", "arduino",
            "raspberry pi", "figma", "swiftui", "kotlin multiplatform",
        ]
        text = message.searchable_text
        found = [tech for tech in known if tech in text]
        return sorted(set(found))

    def extract_tracks(self, message: RawMessage) -> list[str]:
        return self._extract_labeled_list(message, r"tracks?\s*[:\-]\s*(.+)")

    def extract_judging_criteria(self, message: RawMessage) -> list[str]:
        return self._extract_labeled_list(
            message, r"judging criteria\s*[:\-]\s*(.+)"
        )

    def extract_deliverables(self, message: RawMessage) -> list[str]:
        return self._extract_labeled_list(
            message, r"(?:required )?(?:deliverables|submission requirements)\s*[:\-]\s*(.+)"
        )

    def extract_registration_date(self, message: RawMessage) -> date | None:
        """A registration date only counts when explicitly labelled in the body.

        The message's received date is *not* a substitute: an email received in
        January may reference a registration that closed earlier. Callers that
        want the received date use the provenance record instead.
        """

        patterns = (
            r"registration\s+date\s*[:\-]\s*(.+)",
            r"registered\s+on\s+(.+)",
            r"you\s+registered\s+on\s+(.+)",
        )
        for pattern in patterns:
            match = re.search(pattern, message.body, re.IGNORECASE)
            if not match:
                continue
            parsed = self._find_date(match.group(1))
            if parsed is not None:
                return parsed
        return None

    def build_registration_evidence(
        self, message: RawMessage, result: "DetectionResult"
    ) -> EvidenceRef:
        """Record why this message is treated as a registration confirmation.

        The passage is quoted verbatim from the message so the claim can be
        re-checked against the original mail.
        """

        passage = self._registration_passage(message, result)
        state = EvidenceState.VERIFIED if passage else EvidenceState.UNKNOWN
        return EvidenceRef(
            claim=f"{message.subject or '(no subject)'} - registration confirmation",
            url=f"{message.source_label}:{message.message_id}",
            passage=passage,
            state=state,
            retrieval_status="retrieved" if passage else "no_confirming_text_found",
            retrieved_at=(
                message.received_at.isoformat() if message.received_at else now_iso()
            ),
        )

    def _registration_passage(
        self, message: RawMessage, result: "DetectionResult"
    ) -> str:
        """Find the line that actually confirms registration."""

        needles = [phrase for phrase, _ in REGISTRATION_CONFIRMED_SIGNALS]
        for line in message.body.splitlines():
            lowered = line.lower()
            if any(needle in lowered for needle in needles):
                return line.strip()[:280]
        return ""

    def _extract_labeled_list(
        self, message: RawMessage, pattern: str
    ) -> list[str]:
        match = re.search(pattern, message.body, re.IGNORECASE)
        if not match:
            return []
        raw = match.group(1).split("\n")[0]
        parts = [part.strip(" .;-") for part in re.split(r"[,|]", raw)]
        return [part for part in parts if part and 0 < len(part) < 60][:10]


class OpportunityDetector:
    """Combines classification and extraction into an Opportunity record."""

    def __init__(self) -> None:
        self._detector = Detector()
        self._extractor = FieldExtractor()

    def from_message(self, message: RawMessage) -> tuple[Opportunity | None, DetectionResult]:
        result = self._detector.classify(message)
        if not result.is_opportunity:
            return None, result

        extractor = FieldExtractor(reference_year=message.received_at)
        fields, date_reasons = extractor.extract_dates(message)

        timeline = Timeline(
            registration_deadline=fields["registration_deadline"],
            build_start=fields["build_start"],
            submission_deadline=fields["submission_deadline"],
            event_date=fields["event_date"],
        )

        website = extractor.extract_url(message)
        sources = [
            SourceRef(
                kind="registration_email",
                reference=f"{message.source_label}:{message.message_id}",
                retrieved_at=(
                    message.received_at.isoformat() if message.received_at else None
                ),
                verified=True,
            )
        ]
        if website and not any(source.reference == website for source in sources):
            sources.append(SourceRef(kind="official_website", reference=website))

        name = extractor.extract_name(message)
        opportunity = Opportunity(
            id=self.build_id(message, name),
            name=name,
            type=result.opportunity_type,
            organizer=extractor.extract_organizer(message),
            status=(
                OpportunityStatus.REGISTERED
                if result.registered
                else OpportunityStatus.DISCOVERED
            ),
            registration_date=message.received_at,
            timeline=timeline,
            technologies=extractor.extract_technologies(message),
            tracks=extractor.extract_tracks(message),
            judging_criteria=extractor.extract_judging_criteria(message),
            deliverables=extractor.extract_deliverables(message),
            sources=sources,
            provenance=Provenance(
                source_label=message.source_label,
                message_id=message.message_id,
                thread_id=message.thread_id,
                received_at=(
                    message.received_at.isoformat() if message.received_at else None
                ),
                detection_confidence=result.confidence,
                reasons=list(result.reasons) + date_reasons,
                scored_types=dict(result.scores),
            ),
        )

        if result.registered:
            evidence = extractor.build_registration_evidence(message, result)
            opportunity.registration_evidence = evidence
            opportunity.registration_evidence_history = [evidence]
        else:
            # No registration claim is being made, so there is no registration
            # evidence to record. This is not an omission - it is the truth.
            opportunity.registration_evidence = None

        return opportunity, result

    @staticmethod
    def build_id(message: RawMessage, name: str) -> str:
        digest = hashlib.sha256(
            f"{message.sender}|{name}".encode("utf-8")
        ).hexdigest()[:8]
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "opportunity"
        return f"{slug[:48]}-{digest}"