"""Feed-shape scrapers for the config-driven ATS portal engine.

One small class per *feed shape*; per-portal differences are data (``PortalConfig``). The shared
title/location/work-mode filter gate lives in the base class so every portal applies the exact same
eligibility semantics (see ``docs/ATS_PORTALS.md`` §3.3).
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from backend.matchers import check_title_match

logger = logging.getLogger("scraper.ats")

PORTAL_CONFIG_PATH = Path(__file__).resolve().parents[2] / "data" / "ats_portals.json"

# Browser-ish UA for public ATS JSON APIs (mirrors backend.scraper.HEADERS).
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# Per-run shared pacer; set by the scrape dispatcher and read here so every engine request is
# rate-paced. None -> no pacing (unit-test / direct use).
_ACTIVE_PACER = None
_RATE_LIMIT_STATUSES = {429, 503}


def _parse_retry_after(res) -> Optional[float]:
    """Parses a numeric ``Retry-After`` header (seconds) if present."""
    raw = (getattr(res, "headers", None) or {}).get("Retry-After")
    if not raw:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _http(method: str, url: str, *, cfg: Optional[PortalConfig] = None, **kwargs):
    """Paced, permission-gated HTTP call shared by all shape scrapers.

    Returns the ``requests`` response, or ``None`` when the host is disallowed (robots/AI files)
    or has exhausted its per-run request budget. Callers treat ``None`` like a failed fetch.
    """
    from backend.scraper import may_fetch

    if cfg is not None and getattr(cfg, "respect_robots", True):
        if not may_fetch(url):
            logger.info(f"Permission gate: skipping '{url}' (robots/AI-use files).")
            return None

    pacer = _ACTIVE_PACER
    if pacer is not None:
        if not pacer.allow(url, delay=getattr(cfg, "pacing_seconds", None)):
            return None
        pacer.wait(url, delay=getattr(cfg, "pacing_seconds", None))

    try:
        if method == "GET":
            res = requests.get(url, headers=_HEADERS, **kwargs)
        elif method == "POST":
            res = requests.post(url, headers=_HEADERS, **kwargs)
        else:
            res = requests.request(method, url, headers=_HEADERS, **kwargs)
    except Exception as e:  # noqa: BLE001 - network errors are non-fatal per request
        if pacer is not None:
            pacer.note_failure(url)
        logger.debug(f"HTTP {method} {url} failed: {e}")
        return None

    if pacer is not None and res.status_code in _RATE_LIMIT_STATUSES:
        pacer.note_failure(url, retry_after=_parse_retry_after(res))
    return res

_SLUG_RESOLVERS: Dict[str, Callable[[str, str], str]] = {}
_TRANSFORMS: Dict[str, Callable[..., Any]] = {}


def register_slug_resolver(name: str) -> Callable:
    def _decorator(fn: Callable[[str, str], str]) -> Callable:
        _SLUG_RESOLVERS[name] = fn
        return fn

    return _decorator


def register_transform(name: str) -> Callable:
    def _decorator(fn: Callable[..., Any]) -> Callable:
        _TRANSFORMS[name] = fn
        return fn

    return _decorator


@dataclass(frozen=True)
class PortalConfig:
    """Declarative description of one ATS portal feed."""

    id: str
    label: str
    shape: str
    list_url: str
    slug_from: str = "careers_url"
    method: str = "GET"
    body: Optional[Dict[str, Any]] = None
    response_path: str = ""
    title_path: str = "title"
    location_path: str = ""
    location_join: List[str] = field(default_factory=list)
    url_path: str = ""
    alternate_url_path: str = ""
    description_path: str = ""
    description_html: bool = False
    max_pages: int = 5
    page_size: int = 100
    detail: Optional[Dict[str, Any]] = None
    transform: Optional[str] = None
    variant: Optional[str] = None
    risk_tier: str = "public"
    default_flag: Optional[str] = None
    enabled_by_default: bool = False
    timeout: int = 15
    pacing_seconds: Optional[float] = None
    respect_robots: bool = True


def _resolve_path(obj: Any, path: str) -> Any:
    """Resolve a dotted path (``jobAd.sections.jobDescription.text``) against ``obj``."""
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _clean_html(raw: str) -> str:
    if not raw:
        return ""
    return BeautifulSoup(raw, "html.parser").get_text(separator="\n").strip()


def _coerce_location(cfg: PortalConfig, item: Any) -> str:
    """Builds a display location string from the portal's declared paths."""
    if cfg.location_join:
        parts = []
        for p in cfg.location_join:
            val = _resolve_path(item, p)
            if val:
                parts.append(str(val))
        if parts:
            return ", ".join(parts)
    if cfg.location_path:
        val = _resolve_path(item, cfg.location_path)
        if val:
            return str(val)
    return ""


def load_portal_configs(path: Optional[Path] = None) -> List[PortalConfig]:
    """Loads all portal configs from ``data/ats_portals.json`` (list form)."""
    cfg_path = path or PORTAL_CONFIG_PATH
    if not cfg_path.exists():
        return []
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to load ATS portal config: {e}")
        return []

    entries = raw.get("portals", raw) if isinstance(raw, dict) else raw
    configs: List[PortalConfig] = []
    if isinstance(entries, list):
        for entry in entries:
            configs.append(PortalConfig(**entry))
    elif isinstance(entries, dict):
        for pid, entry in entries.items():
            configs.append(PortalConfig(id=pid, **entry))
    return configs


def _context_slug(resolved: Any) -> str:
    if isinstance(resolved, dict):
        return str(resolved.get("slug") or "")
    return resolved or ""


def _resolve_context(cfg: PortalConfig, company_name: str, careers_url: Optional[str]) -> Dict[str, Any]:
    """Full placeholder context for templating ``list_url``/``detail.url``.

    Always contains ``slug``; a dict-returning resolver may contribute additional placeholders.
    """
    resolver = _SLUG_RESOLVERS.get(cfg.slug_from)
    if resolver is None:
        return {"slug": ""}
    resolved = resolver(careers_url or company_name, cfg.id)
    if isinstance(resolved, dict):
        ctx = dict(resolved)
        ctx.setdefault("slug", _context_slug(resolved))
        return ctx
    return {"slug": resolved or ""}


def _format_template(template: str, ctx: Dict[str, Any], *, base: str = "", id: Any = "") -> str:
    """Formats a URL template with the resolver context plus ``base``/``id``.

    Unknown placeholders are left as-is rather than raising, so a missing context value degrades
    to an obviously-invalid URL instead of a swallowed exception.
    """
    fields = {**ctx, "base": base, "id": id}

    class _Safe(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    return template.format_map(_Safe(fields))


class ShapeScraper(ABC):
    """Base class implementing the shared template method.

    Subclasses own only fetch/pagination (:meth:`fetch_list`) and field extraction
    (:meth:`map_item`); slug resolution, the shared filter gate, detail fetch, and the canonical
    output dict live here.
    """

    method = "GET"

    def fetch_list(
        self, cfg: PortalConfig, ctx: Dict[str, Any], *, timeout: int
    ) -> List[Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    def map_item(self, cfg: PortalConfig, item: Any) -> Optional[Dict[str, str]]:
        """Map a raw row to ``{title, location, url, description}`` (or None to skip)."""
        title = _resolve_path(item, cfg.title_path) or "Untitled Role"
        title = str(title).strip()
        location = _coerce_location(cfg, item)

        url = ""
        if cfg.url_path:
            url = _resolve_path(item, cfg.url_path) or ""
        if not url and cfg.alternate_url_path:
            url = _resolve_path(item, cfg.alternate_url_path) or ""

        description = ""
        if cfg.description_path:
            raw = _resolve_path(item, cfg.description_path)
            if raw:
                description = _clean_html(str(raw)) if cfg.description_html else str(raw)

        return {
            "title": title,
            "location": location,
            "url": str(url),
            "description": description,
        }

    def fetch_detail(
        self, cfg: PortalConfig, item: Any, ctx: Dict[str, Any]
    ) -> Any:  # pragma: no cover - optional
        """Returns the detail description (``str``) or ``{"description", "url"}`` to also
        override the mapped URL (used when the apply URL only exists on the detail payload).

        The default implementation GETs ``cfg.detail["url"]`` (templated with ``base``/``slug``/
        ``id``) and concatenates the declared ``desc_paths``.
        """
        detail = cfg.detail or {}
        url_tmpl = detail.get("url")
        if not url_tmpl:
            return ""
        slug = ctx.get("slug", "")
        base = _format_template(cfg.list_url, ctx)
        item_id = ctx.get("item_id") or (item.get("id") if isinstance(item, dict) else "")
        url = _format_template(url_tmpl, ctx, base=base, id=item_id)
        if not url:
            return ""
        res = _http("GET", url, cfg=cfg, timeout=8)
        if res is None or res.status_code != 200:
            return ""
        data = res.json()
        parts: List[str] = []
        for path in detail.get("desc_paths", []):
            txt = _resolve_path(data, path)
            if txt:
                parts.append(_clean_html(str(txt)))
        return "\n\n".join([p for p in parts if p]).strip()

    # -- shared gate -------------------------------------------------------

    @staticmethod
    def _passes_filters(
        title: str,
        location: str,
        *,
        has_filters: bool,
        title_filters: Optional[List[str]],
        city_filters: Optional[List[str]],
        location_filters: Optional[List[str]],
        work_mode: Optional[str],
        secondary_locations: Optional[List[str]] = None,
    ) -> bool:
        if not has_filters:
            return True
        if title_filters and not check_title_match(title, title_filters):
            return False
        from backend.preference_filter import (
            check_location_match,
            check_workmode_match,
        )

        if not check_location_match(
            f"{title} {location}", location_filters, city_filters
        ):
            return False
        if not check_workmode_match(title, location, work_mode):
            return False
        return True

    # -- template method ---------------------------------------------------

    def scrape(
        self,
        cfg: PortalConfig,
        *,
        company_name: str,
        title_filters: Optional[List[str]] = None,
        city_filters: Optional[List[str]] = None,
        location_filters: Optional[List[str]] = None,
        work_mode: Optional[str] = None,
        max_matches: int = 35,
        careers_url: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        if not company_name or not company_name.strip():
            return []

        ctx = _resolve_context(cfg, company_name, careers_url)
        slug = ctx.get("slug") or ""
        if not slug:
            return []

        has_filters = bool(
            title_filters
            or city_filters
            or location_filters
            or (work_mode and work_mode != "all")
        )

        try:
            raw_items = self.fetch_list(cfg, ctx, timeout=cfg.timeout)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{cfg.label} list fetch failed for '{company_name}': {e}")
            return []

        jobs: List[Dict[str, str]] = []
        for item in raw_items:
            mapped = self.map_item(cfg, item)
            if not mapped:
                continue
            title = mapped["title"]
            location = mapped["location"] or "Remote / Various"

            if not self._passes_filters(
                title,
                location,
                has_filters=has_filters,
                title_filters=title_filters,
                city_filters=city_filters,
                location_filters=location_filters,
                work_mode=work_mode,
                secondary_locations=mapped.get("_secondary_locations") or [],
            ):
                continue

            description = mapped["description"]
            url = mapped["url"]
            if cfg.detail and not description:
                try:
                    detail_ctx = dict(ctx)
                    detail_ctx.update(
                        {
                            "cfg": cfg,
                            "item": item,
                            "item_id": mapped.get("_item_id")
                            or (item.get("id") if isinstance(item, dict) else None),
                        }
                    )
                    detail = self.fetch_detail(cfg, item, detail_ctx)
                    if isinstance(detail, dict):
                        description = detail.get("description") or ""
                        if detail.get("url"):
                            url = detail["url"]
                        if detail.get("location"):
                            location = detail["location"]
                    elif detail:
                        description = detail
                except Exception:  # noqa: BLE001 - detail is best-effort
                    pass

            jobs.append(
                {
                    "title": title,
                    "url": url,
                    "description": description
                    or f"{title} opening at {company_name} ({location}).",
                    "location": location,
                    "source": cfg.label,
                    "source_type": "Direct",
                }
            )
            if len(jobs) >= max_matches:
                break

        return jobs


class SimpleJsonScraper(ShapeScraper):
    """Single ``GET`` returning either a bare JSON list or an object with a list at ``response_path``."""

    method = "GET"

    def fetch_list(
        self, cfg: PortalConfig, ctx: Dict[str, Any], *, timeout: int
    ) -> List[Any]:
        url = _format_template(cfg.list_url, ctx)
        res = _http("GET", url, cfg=cfg, timeout=timeout)
        if res is None or res.status_code != 200:
            return []
        data = res.json()
        if isinstance(data, list):
            return data
        if cfg.response_path:
            page = _resolve_path(data, cfg.response_path) or []
        else:
            page = []
        return list(page) if isinstance(page, list) else []


class AshbyScraper(SimpleJsonScraper):
    """Ashby: multi-location rows where location OR work-mode eligibility may match any of the
    primary/secondary locations (differs from the base AND gate)."""

    def map_item(self, cfg: PortalConfig, item: Any) -> Optional[Dict[str, str]]:
        mapped = super().map_item(cfg, item)
        if mapped is None:
            return None
        # Stash the secondary locations on the row for the filter override.
        sec = item.get("secondaryLocations") if isinstance(item, dict) else None
        extra: List[str] = []
        for s in sec or []:
            if isinstance(s, dict) and s.get("location"):
                extra.append(s["location"])
            elif isinstance(s, str) and s:
                extra.append(s)
        mapped["_secondary_locations"] = extra  # type: ignore[assignment]
        return mapped

    def _passes_filters(self, title, location, **kw) -> bool:  # type: ignore[override]
        if not kw.get("has_filters"):
            return True
        title_filters = kw.get("title_filters")
        if title_filters and not check_title_match(title, title_filters):
            return False
        from backend.preference_filter import (
            check_location_match,
            check_workmode_match,
        )

        locations = [location] + list(kw.get("secondary_locations") or [])
        return any(
            check_location_match(
                job_location=loc,
                city_filters=kw.get("city_filters"),
                loc_filters=kw.get("location_filters"),
            )
            or check_workmode_match(title, loc, kw.get("work_mode"))
            for loc in locations
        )


class WorkableScraper(SimpleJsonScraper):
    """Workable: location is a city/state/country join and a remote role gets a ``(Remote)``
    suffix; description falls back to ``full_description`` and is HTML."""

    def map_item(self, cfg: PortalConfig, item: Any) -> Optional[Dict[str, str]]:
        mapped = super().map_item(cfg, item)
        if mapped is None:
            return None
        is_remote = bool(item.get("telecommuting")) or str(
            item.get("workplace_type") or ""
        ).lower() == "remote"
        loc = mapped["location"] or "Remote / Various"
        if is_remote and "remote" not in loc.lower():
            loc = f"{loc} (Remote)"
        mapped["location"] = loc
        if not mapped["description"]:
            raw = item.get("full_description") or ""
            if raw:
                mapped["description"] = _clean_html(str(raw))
        return mapped


class GreenhouseScraper(SimpleJsonScraper):
    """Greenhouse: list rows already carry embedded ``content`` HTML, so the per-posting detail
    call is made ONLY when the embedded description is empty (bandwidth optimisation)."""

    def map_item(self, cfg: PortalConfig, item: Any) -> Optional[Dict[str, str]]:
        mapped = super().map_item(cfg, item)
        if mapped is not None:
            mapped["_item_id"] = item.get("id", "") if isinstance(item, dict) else ""
        return mapped


class PaginatedJsonScraper(ShapeScraper):
    """``GET`` with ``limit``/``offset`` pagination and an optional per-posting detail call."""

    method = "GET"

    def fetch_list(
        self, cfg: PortalConfig, ctx: Dict[str, Any], *, timeout: int
    ) -> List[Any]:
        base = _format_template(cfg.list_url, ctx)
        items: List[Any] = []
        offset = 0
        for _ in range(max(1, cfg.max_pages)):
            res = _http(
                "GET",
                base,
                cfg=cfg,
                params={"limit": cfg.page_size, "offset": offset},
                timeout=timeout,
            )
            if res is None or res.status_code != 200:
                break
            data = res.json()
            page = _resolve_path(data, cfg.response_path) if cfg.response_path else data
            page = page if isinstance(page, list) else []
            items.extend(page)
            total = data.get("totalFound") if isinstance(data, dict) else None
            offset += cfg.page_size
            if not page or (isinstance(total, int) and offset >= total):
                break
        return items



class SmartRecruitersScraper(PaginatedJsonScraper):
    """SmartRecruiters: list rows omit the apply URL and description, so the detail call is
    mandatory — it supplies both the description sections and the ``applyUrl``/``postingUrl``
    (hence ``fetch_detail`` returns a dict to override the mapped URL)."""

    def map_item(self, cfg: PortalConfig, item: Any) -> Optional[Dict[str, str]]:
        mapped = super().map_item(cfg, item)
        if mapped is None:
            return None
        loc = item.get("location") or {} if isinstance(item, dict) else {}
        base_loc = mapped["location"] or "Remote / Various"
        if loc.get("remote") and "remote" not in base_loc.lower():
            base_loc = f"{base_loc} (Remote)"
        mapped["location"] = base_loc
        return mapped

    def fetch_detail(self, cfg: PortalConfig, item: Any, ctx: Dict[str, Any]) -> Dict[str, str]:
        """Single detail call: returns both the description sections and the apply URL."""
        detail = cfg.detail or {}
        url_tmpl = detail.get("url")
        if not url_tmpl or not isinstance(item, dict):
            return {}
        base = cfg.list_url.format(slug=ctx["slug"])
        url = url_tmpl.format(base=base, id=item.get("id", ""))
        if not url:
            return {}
        res = _http("GET", url, cfg=cfg, timeout=8)
        if res is None or res.status_code != 200:
            return {}
        data = res.json()
        parts: List[str] = []
        for path in detail.get("desc_paths", []):
            txt = _resolve_path(data, path)
            if txt:
                parts.append(_clean_html(str(txt)))
        return {
            "description": "\n\n".join([p for p in parts if p]).strip(),
            "url": data.get("applyUrl") or data.get("postingUrl") or "",
        }

_SHAPE_REGISTRY: Dict[str, ShapeScraper] = {
    "simple_json": SimpleJsonScraper(),
    "paginated_json": PaginatedJsonScraper(),
}

# Per-portal variants that need behaviour beyond the pure-config path (selected by
# PortalConfig.variant). Keep these tiny — a variant is justified only when the wire shape
# cannot express the difference declaratively.
_VARIANT_REGISTRY: Dict[str, ShapeScraper] = {
    "ashby": AshbyScraper(),
    "workable": WorkableScraper(),
    "greenhouse": GreenhouseScraper(),
    "smartrecruiters": SmartRecruitersScraper(),
}


def get_shape_scraper(shape: str, variant: Optional[str] = None) -> Optional[ShapeScraper]:
    if variant and variant in _VARIANT_REGISTRY:
        return _VARIANT_REGISTRY[variant]
    return _SHAPE_REGISTRY.get(shape)


def _ensure_default_resolvers() -> None:
    """Registers the built-in ``careers_url`` resolver if nothing has yet.

    Kept lazy (and error-tolerant) so the engine is usable standalone without a circular import
    of ``backend.scraper``; the scrapers register the same resolver explicitly at import time.
    """
    if "careers_url" in _SLUG_RESOLVERS:
        return
    try:
        from backend.scraper import extract_slug_from_careers_url
    except Exception:  # noqa: BLE001 - engine still works if the resolver cannot be loaded
        return
    register_slug_resolver("careers_url")(extract_slug_from_careers_url)


def set_active_pacer(pacer) -> None:
    """Sets the run-level pacer shared across portals (called by the scrape dispatcher)."""
    global _ACTIVE_PACER
    _ACTIVE_PACER = pacer


def run_portal(
    cfg: PortalConfig,
    *,
    company_name: str,
    title_filters: Optional[List[str]] = None,
    city_filters: Optional[List[str]] = None,
    location_filters: Optional[List[str]] = None,
    work_mode: Optional[str] = None,
    max_matches: int = 35,
    careers_url: Optional[str] = None,
    pacer=None,
) -> List[Dict[str, str]]:
    """Executes a portal config through the shape scraper for its ``shape``/``variant``.

    ``pacer`` is an optional shared :class:`backend.ats.pacing.HostPacer`; when omitted a fresh one
    is built from config so direct callers still get pacing + per-host ceilings.
    """
    _ensure_default_resolvers()
    global _ACTIVE_PACER
    if pacer is None:
        # Reuse a run-level pacer if the caller set one (shared delay + ceilings across portals);
        # otherwise build a fresh one so direct callers still get pacing.
        pacer = _ACTIVE_PACER
        if pacer is None:
            try:
                from backend.ats.pacing import build_pacer

                pacer = build_pacer()
            except Exception:  # noqa: BLE001 - pacing is best-effort, never fatal
                pacer = None
    _ACTIVE_PACER = pacer
    scraper = get_shape_scraper(cfg.shape, cfg.variant)
    if scraper is None:
        logger.warning(f"Unknown ATS portal shape '{cfg.shape}' for '{cfg.id}'")
        return []
    return scraper.scrape(
        cfg,
        company_name=company_name,
        title_filters=title_filters,
        city_filters=city_filters,
        location_filters=location_filters,
        work_mode=work_mode,
        max_matches=max_matches,
        careers_url=careers_url,
    )
