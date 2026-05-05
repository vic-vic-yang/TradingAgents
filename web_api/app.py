"""FastAPI application exposing TradingAgents for the web dashboard."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

load_dotenv()
load_dotenv(".env.enterprise", override=False)

from tradingagents.analysis_run_config import build_analysis_config
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.report_save import save_report_to_disk
from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows.utils import normalize_symbol_for_yfinance, safe_ticker_component

logger = logging.getLogger(__name__)

ANALYST_ORDER = ["market", "social", "news", "fundamentals"]
ALLOWED_ANALYSTS = set(ANALYST_ORDER)

_EXPORT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_EXPORT_FOLDER_RE = re.compile(r"^(.+)_(\d{8})_(\d{6})$")
RUN_PARAMETERS_FILENAME = "run_parameters.json"

_executor = ThreadPoolExecutor(max_workers=2)
_jobs_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def _checkpoint_backend_available() -> bool:
    return importlib.util.find_spec("langgraph.checkpoint.sqlite") is not None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_analysts(requested: list[str]) -> list[str]:
    s = {a.lower().strip() for a in requested if a}
    unknown = s - ALLOWED_ANALYSTS
    if unknown:
        raise ValueError(f"Unknown analyst keys: {sorted(unknown)}")
    return [a for a in ANALYST_ORDER if a in s]


def _results_root() -> Path:
    return Path(DEFAULT_CONFIG["results_dir"]).expanduser()


def _cli_reports_root() -> Path:
    """Directory for ``save_report_to_disk`` output.

    Matches the CLI default in ``cli.main`` (``Path.cwd() / "reports"`` when the user
    accepts the save prompt). Override with ``TRADINGAGENTS_CLI_REPORTS_DIR``.
    """
    env = os.getenv("TRADINGAGENTS_CLI_REPORTS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.cwd() / "reports").resolve()


def _export_bundle_path(export_id: str) -> Path:
    if not _EXPORT_ID_RE.match(export_id):
        raise HTTPException(status_code=400, detail="Invalid export id")
    root = _cli_reports_root()
    bundle = (root / export_id).resolve()
    if not str(bundle).startswith(str(root.resolve())):
        raise HTTPException(status_code=400, detail="Invalid export path")
    if not bundle.is_dir():
        raise HTTPException(status_code=404, detail="Export not found")
    return bundle


def _safe_export_relative(rel: str) -> Path:
    rel = rel.strip().replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        raise HTTPException(status_code=400, detail="Invalid file path")
    p = Path(rel)
    if p.is_absolute():
        raise HTTPException(status_code=400, detail="Invalid file path")
    return p


def _info_has_cjk(text: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in text)


def _eastmoney_cn_a_snapshot(code: str) -> dict[str, Any] | None:
    """A-share last price via Eastmoney ``push2`` API (no Yahoo). ``f43``/``f60`` are price * 100."""
    if not re.fullmatch(r"\d{6}", code):
        return None
    if code.startswith("6"):
        secid = f"1.{code}"
    elif code.startswith(("0", "3")):
        secid = f"0.{code}"
    else:
        return None
    url = "http://push2.eastmoney.com/api/qt/stock/get?" + urllib.parse.urlencode(
        {
            "secid": secid,
            "fields": "f43,f57,f58,f60,f170",
            "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        }
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        logger.warning("eastmoney quote %s: %s", code, e)
        return None
    row = payload.get("data")
    if not row or row.get("f43") is None:
        return None
    try:
        price = float(row["f43"]) / 100.0
    except (TypeError, ValueError):
        return None
    change_pct: float | None = None
    if row.get("f170") is not None:
        try:
            change_pct = float(row["f170"]) / 10000.0
        except (TypeError, ValueError):
            pass
    if change_pct is None and row.get("f60") is not None:
        try:
            prev = float(row["f60"]) / 100.0
            if prev != 0:
                change_pct = (price - prev) / prev
        except (TypeError, ValueError):
            pass
    hint = (row.get("f58") or "").strip() or None
    return {
        "price": price,
        "change_pct": change_pct,
        "currency": "CNY",
        "name_zh_hint": hint,
    }


def _mapping_get(m: Any, *keys: str) -> Any:
    """Read yfinance ``fast_info`` / dict-like objects without assuming attribute names."""
    if m is None:
        return None
    for k in keys:
        try:
            if hasattr(m, "__getitem__"):
                v = m[k]
                if v is not None and v == v:  # not NaN
                    return v
        except (KeyError, TypeError):
            pass
        v = getattr(m, k, None)
        if v is not None and v == v:
            return v
    return None


def _fill_price_from_hist_info(
    hist: Any,
    info: dict[str, Any],
    fast: Any | None = None,
) -> tuple[float | None, float | None, str | None]:
    """Last close from history; then ``fast_info``; then ``info`` (``info`` often empty/blocked in CN)."""
    price: float | None = None
    change_pct: float | None = None
    as_of: str | None = None
    if hist is not None and not getattr(hist, "empty", True) and len(hist.index) > 0:
        price = float(hist["Close"].iloc[-1])
        as_of = hist.index[-1].strftime("%Y-%m-%d")
        if len(hist.index) >= 2:
            prev = float(hist["Close"].iloc[-2])
            if prev != 0:
                change_pct = (price - prev) / prev
    if price is None:
        lp = _mapping_get(fast, "lastPrice", "last_price")
        if lp is not None:
            price = float(lp)
    if price is None:
        for key in (
            "currentPrice",
            "regularMarketPrice",
            "postMarketPrice",
            "preMarketPrice",
        ):
            p = info.get(key)
            if p is not None:
                price = float(p)
                break
    if price is None:
        pc_only = info.get("previousClose") or info.get("regularMarketPreviousClose")
        if pc_only is not None:
            price = float(pc_only)
    if change_pct is None and price is not None:
        pc = _mapping_get(
            fast,
            "previousClose",
            "previous_close",
            "regularMarketPreviousClose",
        )
        if pc is None:
            pc = info.get("previousClose") or info.get("regularMarketPreviousClose")
        if pc is not None and float(pc) != 0:
            change_pct = (price - float(pc)) / float(pc)
    if change_pct is None and info.get("regularMarketChangePercent") is not None:
        try:
            rpc = float(info["regularMarketChangePercent"])
            change_pct = rpc / 100.0 if abs(rpc) > 1.0 else rpc
        except (TypeError, ValueError):
            pass
    return price, change_pct, as_of


def _clean_zh_label(name: str, ticker: str) -> str:
    """If label is ``601800 (中国交建)`` or ``601800（中国交建）``, return ``中国交建`` only."""
    s = name.strip()
    t = ticker.strip()
    if not s or not t:
        return s
    if s == t:
        return ""
    m = re.match(rf"^{re.escape(t)}\s*[(（]\s*([^）)]+)\s*[)）]\s*$", s)
    if m:
        return m.group(1).strip()
    return s


def _quote_display_names(info: dict[str, Any], sym: str, raw: str) -> tuple[str, str | None]:
    """English/long fallback and optional Chinese short name for 6-digit A-share codes (yfinance)."""
    short = (info.get("shortName") or "").strip()
    long_n = (info.get("longName") or "").strip()
    raw_clean = raw.strip()
    name_zh: str | None = None
    if re.match(r"^\d{6}$", raw_clean):
        for cand in (short, long_n):
            if cand and _info_has_cjk(cand):
                name_zh = _clean_zh_label(cand, raw_clean) or cand
                break
    name_en = long_n or short or sym
    return name_en, name_zh


def _parse_export_folder_name(name: str) -> tuple[str | None, str | None]:
    """CLI folder ``{ticker}_{YYYYMMDD}_{HHMMSS}`` → ticker, ``YYYY-MM-DD`` (导出/保存日)."""
    m = _EXPORT_FOLDER_RE.match(name)
    if not m:
        return None, None
    ticker = m.group(1)
    ymd = m.group(2)
    date_iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
    return ticker, date_iso


def _company_zh_from_report_excerpt(text: str, ticker: str) -> str | None:
    """Parse Chinese company name from markdown H1 for the given ticker (dynamic, not a static map).

    Supports common patterns: ``# {name}（{ticker} or {ticker}.SS）`` and ``# {ticker}（{name}）``.
    """
    if not text or not ticker:
        return None
    t = ticker.strip()
    if not t:
        return None
    m = re.search(
        r"(?m)^#\s*([^（#\n]{1,40}?)\s*（\s*"
        + re.escape(t)
        + r"(?:\.[A-Z]{2})?\s*[）)]",
        text,
    )
    if m:
        name = m.group(1).strip()
        if name and not re.match(r"^\d{6}$", name):
            return _clean_zh_label(name, t) or name
    m = re.search(
        r"(?m)^#\s*" + re.escape(t) + r"\s*[（(]\s*([^）)\n]{1,40}?)\s*[）)]",
        text,
    )
    if m:
        inner = m.group(1).strip()
        return _clean_zh_label(inner, t) or inner
    return None


def _decision_rating_from_markdown(text: str) -> str:
    """Extract 5-tier rating from PM decision / full report (incl. Chinese prose)."""
    from tradingagents.agents.utils.rating import parse_rating

    return parse_rating(text)


def _read_run_parameters(bundle: Path) -> dict[str, Any] | None:
    """JSON snapshot written by the web API when saving a report (see ``RUN_PARAMETERS_FILENAME``)."""
    p = bundle / RUN_PARAMETERS_FILENAME
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return raw
    except (OSError, json.JSONDecodeError):
        return None


def _cli_export_row_meta(bundle: Path) -> dict[str, Any]:
    ticker, analysis_date = _parse_export_folder_name(bundle.name)
    decision = ""
    decision_path = bundle / "5_portfolio" / "decision.md"
    if not decision_path.is_file():
        decision_path = bundle / "complete_report.md"
    if decision_path.is_file():
        try:
            excerpt = decision_path.read_text(encoding="utf-8", errors="replace")[:12000]
            decision = _decision_rating_from_markdown(excerpt)
            if not ticker:
                tm = re.search(r"\*\*标的\*\*[：:]\s*\*?([^\s*]+)", excerpt)
                if tm:
                    ticker = tm.group(1).strip("*").strip()
        except OSError:
            pass
    tkey = ticker or ""
    display_name_zh = ""
    if tkey:
        for rel in (
            bundle / "1_analysts" / "market.md",
            bundle / "complete_report.md",
            decision_path,
        ):
            if not rel.is_file():
                continue
            try:
                bit = rel.read_text(encoding="utf-8", errors="replace")[:12000]
                found = _company_zh_from_report_excerpt(bit, tkey)
                if found:
                    display_name_zh = _clean_zh_label(found, tkey) or found
                    break
            except OSError:
                continue
    return {
        "ticker": tkey,
        "analysis_date": analysis_date or "",
        "decision": decision,
        "display_name_zh": display_name_zh,
    }


def _log_file_path(ticker: str, trade_date: str) -> Path:
    safe = safe_ticker_component(ticker)
    return (
        _results_root()
        / safe
        / "TradingAgentsStrategy_logs"
        / f"full_states_log_{trade_date}.json"
    )


class AnalysisRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=32)
    trade_date: str = Field(..., description="YYYY-MM-DD")
    analysts: list[str] = Field(default_factory=lambda: list(ANALYST_ORDER))
    max_debate_rounds: int | None = Field(default=None, ge=1, le=10)
    max_risk_discuss_rounds: int | None = Field(default=None, ge=1, le=10)
    checkpoint_enabled: bool = False
    debug: bool = True
    llm_provider: str | None = None
    deep_think_llm: str | None = None
    quick_think_llm: str | None = None
    output_language: str | None = None
    backend_url: str | None = Field(default=None, description="Override API base URL for OpenAI-compatible providers")
    google_thinking_level: str | None = None
    openai_reasoning_effort: str | None = None
    anthropic_effort: str | None = None


class JobCreated(BaseModel):
    job_id: str
    status: Literal["queued"]


class JobRecord(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    created_at: str
    updated_at: str
    ticker: str | None = None
    trade_date: str | None = None
    rating: str | None = None
    log_path: str | None = None
    report_dir: str | None = None
    complete_report_path: str | None = None
    report_save_error: str | None = None
    error: str | None = None


def _model_catalog_public() -> dict[str, Any]:
    """CLI-aligned model options for the web UI (label + id)."""
    out: dict[str, Any] = {}
    for prov, modes in MODEL_OPTIONS.items():
        out[prov] = {
            "quick": [{"label": pair[0], "id": pair[1]} for pair in modes["quick"]],
            "deep": [{"label": pair[0], "id": pair[1]} for pair in modes["deep"]],
        }
    return out


def _llm_providers_public() -> list[dict[str, str]]:
    labels = {
        "deepseek": "DeepSeek",
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "google": "Google (Gemini)",
        "xai": "xAI (Grok)",
        "qwen": "Qwen (DashScope)",
        "glm": "GLM (Zhipu)",
        "mimo": "Xiaomi MiMo",
        "ollama": "Ollama (local)",
    }
    order = (
        "deepseek",
        "openai",
        "anthropic",
        "google",
        "xai",
        "qwen",
        "glm",
        "mimo",
        "ollama",
    )
    seen = set()
    rows: list[dict[str, str]] = []
    for key in order:
        if key in MODEL_OPTIONS:
            rows.append({"id": key, "label": labels.get(key, key)})
            seen.add(key)
    for key in sorted(MODEL_OPTIONS.keys()):
        if key not in seen:
            rows.append({"id": key, "label": labels.get(key, key)})
    return rows


def _build_config(body: AnalysisRequest) -> dict[str, Any]:
    """Same merge rules as ``cli.main.run_analysis`` / ``get_user_selections``."""
    research_depth = (
        body.max_debate_rounds
        if body.max_debate_rounds is not None
        else DEFAULT_CONFIG["max_debate_rounds"]
    )
    prov = (
        str(body.llm_provider).strip().lower()
        if body.llm_provider and str(body.llm_provider).strip()
        else DEFAULT_CONFIG["llm_provider"]
    )
    deep_m = (
        str(body.deep_think_llm).strip()
        if body.deep_think_llm and str(body.deep_think_llm).strip()
        else DEFAULT_CONFIG["deep_think_llm"]
    )
    quick_m = (
        str(body.quick_think_llm).strip()
        if body.quick_think_llm and str(body.quick_think_llm).strip()
        else DEFAULT_CONFIG["quick_think_llm"]
    )
    out_lang = (
        str(body.output_language).strip()
        if body.output_language and str(body.output_language).strip()
        else DEFAULT_CONFIG.get("output_language", "English")
    )
    backend: str | None
    if body.backend_url is None:
        backend = DEFAULT_CONFIG.get("backend_url")
    elif str(body.backend_url).strip() == "":
        backend = None
    else:
        backend = str(body.backend_url).strip()

    checkpoint_enabled = bool(body.checkpoint_enabled)
    if checkpoint_enabled and not _checkpoint_backend_available():
        logger.warning(
            "Checkpoint requested but langgraph.checkpoint.sqlite is unavailable; "
            "falling back to checkpoint_enabled=False."
        )
        checkpoint_enabled = False

    cfg = build_analysis_config(
        research_depth=int(research_depth),
        shallow_thinker=quick_m,
        deep_thinker=deep_m,
        llm_provider=prov,
        backend_url=backend,
        output_language=out_lang,
        google_thinking_level=(
            str(body.google_thinking_level).strip()
            if body.google_thinking_level and str(body.google_thinking_level).strip()
            else None
        ),
        openai_reasoning_effort=(
            str(body.openai_reasoning_effort).strip()
            if body.openai_reasoning_effort
            and str(body.openai_reasoning_effort).strip()
            else None
        ),
        anthropic_effort=(
            str(body.anthropic_effort).strip()
            if body.anthropic_effort and str(body.anthropic_effort).strip()
            else None
        ),
        checkpoint_enabled=checkpoint_enabled,
    )
    return cfg


def _api_key_presence() -> dict[str, bool]:
    """Which provider env vars are set (values never exposed)."""
    mapping = {
        "OPENAI_API_KEY": "openai",
        "GOOGLE_API_KEY": "google",
        "ANTHROPIC_API_KEY": "anthropic",
        "XAI_API_KEY": "xai",
        "DEEPSEEK_API_KEY": "deepseek",
        "DASHSCOPE_API_KEY": "qwen",
        "ZHIPU_API_KEY": "glm",
        "OPENROUTER_API_KEY": "openrouter",
        "MIMO_API_KEY": "mimo",
        "ALPHA_VANTAGE_API_KEY": "alpha_vantage",
    }
    return {v: bool(os.getenv(k)) for k, v in mapping.items()}


def _checkpoint_stats() -> dict[str, Any]:
    base = Path(DEFAULT_CONFIG["data_cache_dir"]).expanduser()
    cp_dir = base / "checkpoints"
    n = len(list(cp_dir.glob("*.db"))) if cp_dir.is_dir() else 0
    return {
        "checkpoint_dir": str(cp_dir),
        "checkpoint_db_count": n,
    }


def _run_analysis_job(
    job_id: str,
    ticker: str,
    trade_date: str,
    analysts: list[str],
    body: AnalysisRequest,
) -> None:
    try:
        with _jobs_lock:
            rec = _jobs[job_id]
            rec["status"] = "running"
            rec["updated_at"] = _utc_now_iso()
        cfg = _build_config(body)
        # Match cli.main.run_analysis: debug=True there; body.debug defaults True for parity.
        graph = TradingAgentsGraph(
            analysts,
            config=cfg,
            debug=body.debug,
            callbacks=[],
        )
        final_state, rating = graph.propagate(ticker.strip().upper(), trade_date)
        log_path = _log_file_path(ticker.strip().upper(), trade_date)
        report_dir: str | None = None
        complete_report_path: str | None = None
        report_save_error: str | None = None
        try:
            safe_t = safe_ticker_component(ticker.strip().upper())
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_dir = _cli_reports_root() / f"{safe_t}_{ts}"
            complete = save_report_to_disk(final_state, ticker.strip().upper(), save_dir)
            try:
                snap = body.model_dump(mode="json")
                snap["analysts"] = list(analysts)
                (save_dir / RUN_PARAMETERS_FILENAME).write_text(
                    json.dumps(snap, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError as wexc:
                logger.warning("Could not write %s: %s", RUN_PARAMETERS_FILENAME, wexc)
            report_dir = str(save_dir.resolve())
            complete_report_path = str(complete.resolve())
            logger.info(
                "Saved analysis report for job %s to %s",
                job_id,
                complete_report_path,
            )
        except Exception as save_exc:
            logger.exception("Report save failed for job %s (%s)", job_id, ticker)
            report_save_error = str(save_exc)
        with _jobs_lock:
            _jobs[job_id].update(
                {
                    "status": "completed",
                    "updated_at": _utc_now_iso(),
                    "rating": rating,
                    "log_path": str(log_path) if log_path.exists() else None,
                    "report_dir": report_dir,
                    "complete_report_path": complete_report_path,
                    "report_save_error": report_save_error,
                }
            )
    except Exception as e:
        logger.exception("Analysis job %s failed", job_id)
        with _jobs_lock:
            _jobs[job_id].update(
                {
                    "status": "failed",
                    "updated_at": _utc_now_iso(),
                    "error": str(e),
                }
            )


def create_app() -> FastAPI:
    app = FastAPI(
        title="TradingAgents API",
        description="Backend for the TradingAgents web dashboard",
        version="0.1.0",
    )
    origins = os.getenv(
        "TRADINGAGENTS_CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    origin_regex = os.getenv(
        "TRADINGAGENTS_CORS_ORIGIN_REGEX",
        r"^https://.*\.(vercel\.app|trycloudflare\.com)$",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins if o.strip()],
        allow_origin_regex=origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/config")
    def get_public_config() -> dict[str, Any]:
        """Safe defaults for the UI (no secrets)."""
        cp = _checkpoint_stats()
        return {
            "analyst_order": ANALYST_ORDER,
            "default_analysts": ANALYST_ORDER,
            "results_dir": str(Path(DEFAULT_CONFIG["results_dir"]).expanduser()),
            "data_cache_dir": str(Path(DEFAULT_CONFIG["data_cache_dir"]).expanduser()),
            "memory_log_path": str(Path(DEFAULT_CONFIG["memory_log_path"]).expanduser()),
            "memory_log_max_entries": DEFAULT_CONFIG.get("memory_log_max_entries"),
            "default_llm_provider": DEFAULT_CONFIG["llm_provider"],
            "default_deep_think_llm": DEFAULT_CONFIG["deep_think_llm"],
            "default_quick_think_llm": DEFAULT_CONFIG["quick_think_llm"],
            "default_output_language": DEFAULT_CONFIG.get("output_language", "English"),
            "default_checkpoint_enabled": bool(DEFAULT_CONFIG.get("checkpoint_enabled")),
            "checkpoint_backend_available": _checkpoint_backend_available(),
            "data_vendors": dict(DEFAULT_CONFIG.get("data_vendors") or {}),
            "tool_vendors": dict(DEFAULT_CONFIG.get("tool_vendors") or {}),
            "llm_providers": _llm_providers_public(),
            "model_catalog": _model_catalog_public(),
            "cli_reports_dir": str(_cli_reports_root()),
            "api_keys_configured": _api_key_presence(),
            "checkpoint_dir": cp["checkpoint_dir"],
            "checkpoint_db_count": cp["checkpoint_db_count"],
            "docs_url": "/docs",
        }

    @app.post("/api/checkpoints/clear")
    def clear_checkpoints() -> dict[str, int]:
        """Remove all LangGraph checkpoint DBs (same as CLI ``--clear-checkpoints``)."""
        from tradingagents.graph.checkpointer import clear_all_checkpoints

        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        return {"removed": n}

    @app.post("/api/analyses", response_model=JobCreated)
    def start_analysis(body: AnalysisRequest) -> JobCreated:
        try:
            analysts = _normalize_analysts(body.analysts)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not analysts:
            raise HTTPException(
                status_code=400, detail="At least one analyst must be selected."
            )
        job_id = str(uuid.uuid4())
        now = _utc_now_iso()
        with _jobs_lock:
            _jobs[job_id] = {
                "job_id": job_id,
                "status": "queued",
                "created_at": now,
                "updated_at": now,
                "ticker": body.ticker.strip().upper(),
                "trade_date": body.trade_date,
            }
        _executor.submit(
            _run_analysis_job,
            job_id,
            body.ticker.strip().upper(),
            body.trade_date,
            analysts,
            body,
        )
        return JobCreated(job_id=job_id, status="queued")

    @app.get("/api/analyses/{job_id}", response_model=JobRecord)
    def get_job(job_id: str) -> JobRecord:
        with _jobs_lock:
            rec = _jobs.get(job_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Job not found")
        return JobRecord(
            job_id=rec["job_id"],
            status=rec["status"],
            created_at=rec["created_at"],
            updated_at=rec["updated_at"],
            ticker=rec.get("ticker"),
            trade_date=rec.get("trade_date"),
            rating=rec.get("rating"),
            log_path=rec.get("log_path"),
            report_dir=rec.get("report_dir"),
            complete_report_path=rec.get("complete_report_path"),
            report_save_error=rec.get("report_save_error"),
            error=rec.get("error"),
        )

    @app.get("/api/analyses")
    def list_jobs(limit: int = 50) -> list[JobRecord]:
        with _jobs_lock:
            items = sorted(
                _jobs.values(),
                key=lambda x: x["created_at"],
                reverse=True,
            )[:limit]
        return [
            JobRecord(
                job_id=r["job_id"],
                status=r["status"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                ticker=r.get("ticker"),
                trade_date=r.get("trade_date"),
                rating=r.get("rating"),
                log_path=r.get("log_path"),
                report_dir=r.get("report_dir"),
                complete_report_path=r.get("complete_report_path"),
                report_save_error=r.get("report_save_error"),
                error=r.get("error"),
            )
            for r in items
        ]

    @app.get("/api/results/tickers")
    def list_tickers() -> list[str]:
        root = _results_root()
        if not root.is_dir():
            return []
        return sorted(
            p.name
            for p in root.iterdir()
            if p.is_dir() and (p / "TradingAgentsStrategy_logs").is_dir()
        )

    @app.get("/api/results/tickers/{ticker}/logs")
    def list_ticker_logs(ticker: str) -> list[dict[str, str]]:
        safe = safe_ticker_component(ticker)
        log_dir = _results_root() / safe / "TradingAgentsStrategy_logs"
        if not log_dir.is_dir():
            raise HTTPException(status_code=404, detail="No logs for ticker")
        files = sorted(log_dir.glob("full_states_log_*.json"))
        return [
            {
                "name": f.name,
                "trade_date": f.stem.replace("full_states_log_", ""),
                "path": str(f),
            }
            for f in files
        ]

    @app.get("/api/results/tickers/{ticker}/logs/{log_name}")
    def get_ticker_log(ticker: str, log_name: str) -> Any:
        if not log_name.endswith(".json") or ".." in log_name:
            raise HTTPException(status_code=400, detail="Invalid log name")
        safe = safe_ticker_component(ticker)
        path = _results_root() / safe / "TradingAgentsStrategy_logs" / log_name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Log not found")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail=f"Invalid JSON: {e}") from e

    @app.get("/api/quotes/{ticker}")
    def quote_snapshot(ticker: str) -> dict[str, Any]:
        """Dashboard quote: tries **Yahoo Finance (yfinance)** first, then **Eastmoney** for bare 6-digit A-shares."""
        import yfinance as yf

        raw = ticker.strip()
        if not raw:
            raise HTTPException(status_code=400, detail="Ticker required")
        sym = normalize_symbol_for_yfinance(raw)
        err_msg: str | None = None
        quote_source: str = "none"
        name = sym
        name_zh: str | None = None
        price: float | None = None
        change_pct: float | None = None
        currency: str | None = None
        as_of: str | None = None
        hist: Any = None
        info: dict[str, Any] = {}
        fast: Any | None = None
        em_code = raw.split(".")[0].strip()
        if re.fullmatch(r"\d{6}", em_code) and em_code.startswith(("6", "0", "3")):
            em = _eastmoney_cn_a_snapshot(em_code)
            if em and em.get("price") is not None:
                price = em["price"]
                change_pct = em.get("change_pct")
                currency = em.get("currency") or "CNY"
                if em.get("name_zh_hint"):
                    name_zh = em["name_zh_hint"]
                quote_source = "eastmoney"
                as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            stock = yf.Ticker(sym)
            try:
                hist = stock.history(period="5d", auto_adjust=True)
                if hist is None or getattr(hist, "empty", True) or len(hist.index) == 0:
                    hist = stock.history(period="1mo", auto_adjust=True)
                if hist is None or getattr(hist, "empty", True) or len(hist.index) == 0:
                    hist = stock.history(period="3mo", auto_adjust=True)
            except Exception as e:
                logger.warning("quote history failed %s: %s", sym, e)
                err_msg = (err_msg + "; ") if err_msg else ""
                err_msg += f"history:{e}"
            try:
                fast = stock.fast_info
            except Exception as e:
                logger.warning("quote fast_info failed %s: %s", sym, e)
                err_msg = (err_msg + "; ") if err_msg else ""
                err_msg += f"fast_info:{e}"
            try:
                raw_info = stock.info
                if isinstance(raw_info, dict):
                    info = raw_info
                elif raw_info:
                    info = dict(raw_info)
                else:
                    info = {}
            except Exception as e:
                logger.warning("quote info failed %s: %s", sym, e)
                err_msg = (err_msg + "; ") if err_msg else ""
                err_msg += f"info:{e}"
            zh_from_em = name_zh
            name, name_zh = _quote_display_names(info, sym, raw)
            name_zh = name_zh or zh_from_em
            if not currency:
                currency = info.get("currency") or _mapping_get(fast, "currency")
            if price is None:
                price, change_pct, as_of = _fill_price_from_hist_info(hist, info, fast)
            if price is None:
                try:
                    df = yf.download(
                        sym,
                        period="5d",
                        interval="1d",
                        progress=False,
                        auto_adjust=True,
                        threads=False,
                    )
                    if df is not None and not getattr(df, "empty", True) and "Close" in df.columns:
                        ser = df["Close"]
                        if hasattr(ser, "squeeze"):
                            ser = ser.squeeze()
                        if hasattr(ser, "iloc") and len(ser) > 0:
                            last = ser.iloc[-1]
                            price = float(last) if last == last else None
                            if price is not None and len(ser) >= 2:
                                prev = float(ser.iloc[-2])
                                if prev != 0:
                                    change_pct = (price - prev) / prev
                            if as_of is None and len(df.index) > 0:
                                as_of = df.index[-1].strftime("%Y-%m-%d")
                except Exception as e:
                    logger.warning("quote download fallback %s: %s", sym, e)
                    err_msg = (err_msg + "; ") if err_msg else ""
                    err_msg += f"download:{e}"
            if price is not None and quote_source != "eastmoney":
                quote_source = "yahoo"
        except Exception as e:
            logger.warning("quote_snapshot failed for %s: %s", sym, e)
            err_msg = (err_msg + "; ") if err_msg else ""
            err_msg += str(e)
        if price is None and re.fullmatch(r"\d{6}", em_code):
            em = _eastmoney_cn_a_snapshot(em_code)
            if em and em.get("price") is not None:
                price = em["price"]
                if change_pct is None:
                    change_pct = em.get("change_pct")
                currency = em.get("currency") or "CNY"
                if not name_zh and em.get("name_zh_hint"):
                    name_zh = em["name_zh_hint"]
                quote_source = "eastmoney"
        def _json_float(x: float | None) -> float | None:
            if x is None:
                return None
            return float(x)

        return {
            "symbol": sym,
            "ticker": raw.upper(),
            "name": name,
            "name_zh": name_zh,
            "price": _json_float(price),
            "change_pct": _json_float(change_pct),
            "currency": currency,
            "as_of": as_of,
            "quote_source": quote_source,
            "error": err_msg,
        }

    @app.get("/api/reports/exports")
    def list_cli_exports() -> list[dict[str, Any]]:
        """Folders under ``reports/`` from CLI save (e.g. ``{ticker}_{YYYYMMDD}_{HHMMSS}``); tickers from disk, not hardcoded."""
        root = _cli_reports_root()
        if not root.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for p in root.iterdir():
            if not p.is_dir():
                continue
            if not _EXPORT_ID_RE.match(p.name):
                continue
            has_layout = (p / "complete_report.md").is_file() or (p / "1_analysts").is_dir()
            if not has_layout:
                continue
            st = p.stat()
            meta = _cli_export_row_meta(p)
            rows.append(
                {
                    "id": p.name,
                    "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                    "has_complete_report": (p / "complete_report.md").is_file(),
                    **meta,
                }
            )
        rows.sort(key=lambda r: r["modified_at"], reverse=True)
        return rows

    @app.get("/api/reports/exports/{export_id}/files")
    def list_cli_export_files(export_id: str) -> dict[str, Any]:
        bundle = _export_bundle_path(export_id)
        files: list[dict[str, str]] = []
        for f in sorted(bundle.rglob("*.md"), key=lambda x: x.relative_to(bundle).as_posix()):
            rel = f.relative_to(bundle).as_posix()
            files.append({"path": rel, "name": f.name})
        meta = _cli_export_row_meta(bundle)
        run_parameters = _read_run_parameters(bundle)
        return {
            "export_id": export_id,
            "files": files,
            "run_parameters": run_parameters,
            **meta,
        }

    @app.get("/api/reports/exports/{export_id}/content", response_class=PlainTextResponse)
    def get_cli_export_file(export_id: str, path: str) -> PlainTextResponse:
        bundle = _export_bundle_path(export_id)
        rel = _safe_export_relative(path)
        target = (bundle / rel).resolve()
        if not str(target).startswith(str(bundle.resolve())):
            raise HTTPException(status_code=400, detail="Invalid file path")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        if target.suffix.lower() not in {".md", ".txt", ".markdown"}:
            raise HTTPException(status_code=400, detail="Only markdown/text files are served")
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

    @app.get("/api/memory/entries")
    def memory_entries() -> list[dict[str, Any]]:
        mem = TradingMemoryLog(DEFAULT_CONFIG)
        return mem.load_entries()

    return app


app = create_app()
