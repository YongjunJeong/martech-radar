"""The dashboard.

It edits the watchlist and queues scans, but it never scans: work goes on a
queue and a worker picks it up, which is what lets the browser run somewhere
other than the server. Nothing here ever writes a verdict — verdicts come
from evidence, and evidence comes from the worker.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from radar import config as cfg
from radar import registry as reg
from radar import signals as sig
from radar import evidence as ev
from radar import targets as tgt
from radar import security
from radar import brief as brf
from radar import tasks as tsk
from radar.batch import due_split, jobs_for
from radar.store import Store
from radar.strings import translator

from . import data

HERE = Path(__file__).resolve().parent

def _labels(t, prefix: str, keys) -> dict[str, str]:
    """Turn catalogue entries into the flat maps templates look things up in."""
    return {key: t(f"{prefix}.{key}") for key in keys}


def create_app(db_path: str | None = None, targets_path: str | None = None,
               fingerprints_path: str | None = None,
               language: str | None = None) -> FastAPI:
    app = FastAPI(title="MarTech Radar", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    settings = cfg.load()
    lang = language or settings.ui.language
    app.state.settings = settings
    t = translator(lang)
    templates.env.globals.update(
        t=t,
        lang=lang,
        requires_login=settings.server.requires_login,
        trusted=ev.TRUSTWORTHY_STATUSES,
        verdict_label=_labels(t, "verdict", ("DETECTED", "PROBABLE")),
        state_label=_labels(t, "state", data.STATE_ORDER),
        kind_label=_labels(t, "kind", sig.PRIORITY),
        category_label=_labels(t, "category", data.CATEGORY_ORDER),
        status_label=_labels(t, "status", ev.TRUSTWORTHY_STATUSES | {
            "BLOCKED", "TIMEOUT", "NAV_FAILED", "BROWSER_ERROR", "THIN",
            "SKIPPED_BY_ROBOTS", "PARTIAL"}),
        state_order=data.STATE_ORDER,
        task_kind_label=_labels(t, "task.kind", tsk.KINDS),
        task_state_label=_labels(t, "task.state", ("pending", "running", "done", "failed")),
    )

    registry = reg.load(fingerprints_path)
    app.state.db_path = db_path
    app.state.language = lang

    def industry_labels(s: Store) -> dict[str, str]:
        """Industry labels come from the database, like the watchlist itself."""
        return {row["code"]: (row["label_en"] if lang != "ko" and row["label_en"]
                              else row["label"])
                for row in s.industries()}

    def store() -> Store:
        return Store(app.state.db_path)

    def page(request: Request, name: str, industries: dict[str, str] | None = None,
             **context) -> HTMLResponse:
        return templates.TemplateResponse(
            request, name, {"industries": industries or {}, **context})

    @app.middleware("http")
    async def require_session(request: Request, call_next):
        """With a token configured, everything needs a session except login."""
        gated = (settings.server.requires_login
                 and request.url.path not in ("/login", "/health")
                 and not request.url.path.startswith("/static"))
        if gated and not security.session_is_valid(
                request.cookies.get(security.COOKIE_NAME), settings.server.token):
            return RedirectResponse(url="/login", status_code=303)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with store() as s:
            labels = industry_labels(s)
            companies = data.company_rows(s, labels, app.state.language)
            return page(request, "index.html", industries=labels,
                        overview=data.overview(s, companies),
                        industries_rows=data.industry_rows(companies),
                        vendors=[v for v in data.vendor_rows(s, companies)
                                 if v["category"] == sig.ENGAGEMENT and v["count"]],
                        active="index")

    @app.get("/signals", response_class=HTMLResponse)
    def signals(request: Request, kind: str | None = None, industry: str | None = None,
                dismissed: int = 0):
        from radar.notify import signal_key
        with store() as s:
            labels = industry_labels(s)
            everything = sig.all_signals(s, language=app.state.language)
            acks = s.signal_acks()
            found = []
            for x in everything:
                row = x.as_dict()
                row["key"] = signal_key(x)
                ack = acks.get(row["key"])
                row["ack"] = ack["state"] if ack else ""
                found.append(row)
            if kind:
                found = [x for x in found if x["kind"] == kind]
            if industry:
                found = [x for x in found if x["industry"] == industry]
            hidden = sum(1 for x in found if x["ack"] == "DISMISSED")
            if not dismissed:
                found = [x for x in found if x["ack"] != "DISMISSED"]
            return page(request, "signals.html", industries=labels,
                        signals=found, dismissed_count=hidden,
                        show_dismissed=bool(dismissed),
                        counts={k: sum(1 for x in everything if x.kind == k)
                                for k in sig.PRIORITY},
                        quiet=sig.quiet_greenfield(s, app.state.language),
                        unjudged=[c for c in data.company_rows(s, labels, app.state.language)
                                  if not c["judged"]],
                        kind=kind, industry=industry, active="signals")

    @app.post("/signals/ack")
    def signal_ack(request: Request, key: str = Form(...), state: str = Form("")):
        guard(request)
        if state and state not in Store.ACK_STATES:
            raise HTTPException(status_code=400, detail=f"unknown state {state!r}")
        with store() as s:
            s.set_signal_ack(key, state or None)
        return back(request, "/signals", "")

    @app.get("/companies", response_class=HTMLResponse)
    def companies(request: Request, industry: str | None = None,
                  state: str | None = Query(None, pattern="^(engagement|greenfield|unjudged|moved)$")):
        with store() as s:
            labels = industry_labels(s)
            rows = data.company_rows(s, labels, app.state.language)
        if industry:
            rows = [r for r in rows if r["industry"] == industry]
        if state == "engagement":
            rows = [r for r in rows if r["judged"] and r["engagement"] and not r["off_domain"]]
        elif state == "greenfield":
            rows = [r for r in rows if r["judged"] and not r["off_domain"]
                    and not r["engagement"] and not r["engagement_pending"]]
        elif state == "unjudged":
            rows = [r for r in rows if not r["judged"]]
        elif state == "moved":
            rows = [r for r in rows if r["moved"]]
        return page(request, "companies.html", industries=labels, companies=rows,
                    industry=industry, state=state, active="companies")

    @app.get("/companies/{target_id}", response_class=HTMLResponse)
    def company(request: Request, target_id: str):
        with store() as s:
            labels = industry_labels(s)
            detail = data.company_detail(s, labels, target_id, app.state.language)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"unknown company {target_id!r}")
        return page(request, "company.html", industries=labels, c=detail, active="companies")

    @app.get("/companies/{target_id}/brief", response_class=HTMLResponse)
    def company_brief(request: Request, target_id: str):
        with store() as s:
            if s.target(target_id) is None:
                raise HTTPException(status_code=404, detail=f"unknown company {target_id!r}")
            labels = industry_labels(s)
            text, usable = brf.company_brief(s, target_id, language=app.state.language)
            company = data.company_name(s.target(target_id), app.state.language)
        return page(request, "brief.html", industries=labels, target_id=target_id,
                    company=company, text=text, usable=usable, active="companies")

    @app.get("/vendors", response_class=HTMLResponse)
    def vendors(request: Request, category: str | None = None, unseen: int = 0):
        with store() as s:
            labels = industry_labels(s)
            companies_rows = data.company_rows(s, labels, app.state.language)
            rows = data.vendor_rows(s, companies_rows, registry if unseen else None)
            judged = sum(1 for c in companies_rows if c["judged"])
        if category:
            rows = [r for r in rows if r["category"] == category]
        return page(request, "vendors.html", industries=labels,
                    vendor_groups=data.group_by_category(rows), judged=judged,
                    category=category, unseen=bool(unseen),
                    categories=data.CATEGORY_ORDER, active="vendors")

    @app.get("/vendors/{fingerprint_id}", response_class=HTMLResponse)
    def vendor(request: Request, fingerprint_id: str):
        fingerprint = registry.get(fingerprint_id)
        if fingerprint is None:
            raise HTTPException(status_code=404, detail=f"unknown vendor {fingerprint_id!r}")
        with store() as s:
            labels = industry_labels(s)
            rows = data.vendor_rows(
                s, data.company_rows(s, labels, app.state.language))
        row = next((r for r in rows if r["id"] == fingerprint_id),
                   {"id": fingerprint.id, "name": fingerprint.name,
                    "category": fingerprint.category, "count": 0,
                    "share": 0.0, "companies": []})
        return page(request, "vendor.html", industries=labels, v=row,
                    fingerprint=fingerprint, active="vendors")

    @app.get("/fingerprints", response_class=HTMLResponse)
    def fingerprints(request: Request, q: str = "", field: str = "hosts",
                     pattern: str = ""):
        """Browse the fingerprints, and try a pattern against stored evidence.

        Editing stays in the YAML files on purpose — that is where the
        review happens, and a form would quietly invite the two mistakes
        that have actually caused false positives here: an unanchored
        filename, and a global short enough to belong to somebody else.
        """
        rows = [
            {"id": fp.id, "name": fp.name, "category": fp.category,
             "note": fp.note, "vendor_url": fp.vendor_url,
             "signals": [{"field": sg.field, "pattern": sg.pattern,
                          "strength": sg.strength} for sg in fp.signals]}
            for fp in registry
            if not q or q.lower() in fp.name.lower() or q.lower() in fp.id
        ]
        trial = None
        if pattern.strip():
            trial = data.try_pattern(field, pattern.strip(), app.state.db_path)
        with store() as s:
            labels = industry_labels(s)
        return page(request, "fingerprints.html", industries=labels,
                    fingerprints=data.group_by_category(rows), q=q,
                    field=field, pattern=pattern, trial=trial,
                    fields=list(reg.FIELDS), active="fingerprints")

    # -- writes ------------------------------------------------------------

    def guard(request: Request) -> None:
        """Refuse a write that arrives without a session or from elsewhere."""
        if settings.server.requires_login and not security.session_is_valid(
                request.cookies.get(security.COOKIE_NAME), settings.server.token):
            raise HTTPException(status_code=401, detail="sign in first")
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            # Same-origin only. The cookie is SameSite=strict as well; this
            # is the belt to that pair of braces.
            raise HTTPException(status_code=403, detail="cross-origin write refused")

    def back(request: Request, to: str, message: str = "") -> RedirectResponse:
        # Quote it: a '#' in the message would otherwise become a fragment
        # and the rest of the sentence would never reach the page.
        suffix = f"?msg={quote(message)}" if message else ""
        return RedirectResponse(url=f"{to}{suffix}", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, bad: int = 0):
        if not settings.server.requires_login:
            return back(request, "/")
        return page(request, "login.html", bad=bool(bad), active="")

    @app.post("/login")
    def login(request: Request, token: str = Form(...)):
        if not security.token_matches(token, settings.server.token):
            return RedirectResponse(url="/login?bad=1", status_code=303)
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            security.COOKIE_NAME, security.session_value(settings.server.token),
            httponly=True, samesite="strict", max_age=60 * 60 * 24 * 30)
        return response

    @app.post("/logout")
    def logout():
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(security.COOKIE_NAME)
        return response

    @app.get("/manage", response_class=HTMLResponse)
    def manage(request: Request, msg: str = "", edit: str | None = None):
        with store() as s:
            labels = industry_labels(s)
            rows = [dict(r) for r in s.targets()]
            editing = dict(s.target(edit)) if edit and s.target(edit) else None
            queued = [s.run_progress(r["run_id"]) for r in s.active_runs()]
            last = s.last_status_per_target()
        for row in rows:
            row["urls"] = json.loads(row["urls_json"])
            # Surfacing this is the point: an operator cannot decide about an
            # exception without knowing the site is turning us away.
            row["last_status"] = last.get(row["id"])
        if editing:
            editing["urls"] = json.loads(editing["urls_json"])
        return page(request, "manage.html", industries=labels, targets=rows,
                    editing=editing, queued=queued, msg=msg, active="manage")

    @app.post("/manage/save")
    def manage_save(
        request: Request,
        id: str = Form(...), company: str = Form(...), industry: str = Form(...),
        urls: str = Form(...), company_en: str = Form(""), tier: str = Form(""),
        note: str = Form(""), enabled: str = Form(""),
        robots_policy: str = Form("respect"), robots_note: str = Form(""),
    ):
        guard(request)
        target_id = id.strip().lower()
        if not tgt.ID_PATTERN.match(target_id):
            return back(request, "/manage", "id must be lowercase letters, digits and _")
        url_list = [u.strip() for u in urls.replace(",", "\n").splitlines() if u.strip()]
        if not url_list:
            return back(request, "/manage", "at least one URL is needed")
        try:
            for url in url_list:
                security.check_scan_target(
                    url, allow_private=settings.server.allow_private_targets)
        except security.UnsafeTarget as exc:
            return back(request, "/manage", str(exc))

        with store() as s:
            if industry not in {r["code"] for r in s.industries()}:
                return back(request, "/manage", f"unknown industry {industry}")
            try:
                s.upsert_target(
                    id=target_id, company=company.strip(),
                    company_en=company_en.strip() or None,
                    industry=industry, tier=tier.strip() or None,
                    note=note.strip() or None, urls=url_list,
                    enabled=bool(enabled),
                    robots_policy=robots_policy,
                    robots_note=robots_note.strip() or None)
            except ValueError as exc:
                return back(request, "/manage", str(exc))
        return back(request, "/manage", f"saved {target_id}")

    @app.post("/manage/industry")
    def manage_industry(request: Request, code: str = Form(...), label: str = Form(...),
                        label_en: str = Form("")):
        guard(request)
        code = code.strip().lower()
        if not tgt.ID_PATTERN.match(code):
            return back(request, "/manage", "industry code must be lowercase letters, digits and _")
        if not label.strip():
            return back(request, "/manage", "an industry needs a label")
        with store() as s:
            s.upsert_industry(code, label.strip(), label_en.strip())
        return back(request, "/manage", f"industry {code} saved")

    @app.post("/manage/toggle")
    def manage_toggle(request: Request, id: str = Form(...), enabled: str = Form("")):
        guard(request)
        with store() as s:
            found = s.set_target_enabled(id, bool(enabled))
        return back(request, "/manage", "saved" if found else f"unknown target {id}")

    @app.post("/manage/scan")
    def manage_scan(request: Request, id: str = Form(""), scope: str = Form("all")):
        guard(request)
        with store() as s:
            jobs = jobs_for(s, target_ids=[id]) if id else jobs_for_scope(s, "all")
            if not jobs:
                return back(request, "/manage", "nothing enabled to scan")
            run_id = s.queue_run(jobs, note=f"queued from the dashboard ({scope})")
        return back(request, f"/runs/{run_id}", "")

    # -- operations: what used to need a terminal ---------------------------

    def worker_status(s: Store) -> dict:
        raw = s.get_meta("worker_seen")
        if not raw:
            return {"state": "none"}
        seen = json.loads(raw)
        age = int((datetime.now(UTC) - datetime.fromisoformat(seen["at"])).total_seconds())
        return {"state": "alive" if age < 30 else "stale", "name": seen.get("name"),
                "ago": age, "at": seen["at"][:16].replace("T", " ")}

    def scan_scopes(s: Store) -> list[dict]:
        """The scan buttons: what each would queue, with its count."""
        every = jobs_for(s)
        due, _ = due_split(s, every)
        scopes = [{"scope": "due", "label": t("ops.scan.due", n=len({j.target_id for j in due}))}]
        for tier in sorted({j.tier for j in every if j.tier}):
            n = len({j.target_id for j in every if j.tier == tier})
            scopes.append({"scope": f"tier:{tier}", "label": t("ops.scan.tier", tier=tier, n=n)})
        scopes.append({"scope": "all", "label": t("ops.scan.all", n=len({j.target_id for j in every}))})
        return scopes

    def jobs_for_scope(s: Store, scope: str) -> list:
        if scope == "all":
            return jobs_for(s)
        if scope == "due":
            return due_split(s, jobs_for(s))[0]
        if scope.startswith("tier:"):
            return jobs_for(s, tiers=[scope[5:]])
        raise HTTPException(status_code=400, detail=f"unknown scope {scope!r}")

    @app.get("/ops", response_class=HTMLResponse)
    def ops(request: Request, msg: str = ""):
        with store() as s:
            labels = industry_labels(s)
            return page(request, "ops.html", industries=labels,
                        worker=worker_status(s), scopes=scan_scopes(s),
                        runs=[s.run_progress(r["run_id"]) for r in s.active_runs()],
                        tasks=[dict(r) for r in s.tasks(limit=30)],
                        kinds=tsk.KINDS, msg=msg, active="ops")

    @app.post("/ops/scan")
    def ops_scan(request: Request, scope: str = Form("due")):
        guard(request)
        with store() as s:
            jobs = jobs_for_scope(s, scope)
            if not jobs:
                return back(request, "/ops", t("ops.scan.nothing"))
            run_id = s.queue_run(jobs, note=f"queued from the dashboard ({scope})")
        return back(request, f"/runs/{run_id}", "")

    @app.post("/ops/task")
    def ops_task(request: Request, kind: str = Form(...), dry_run: str = Form("")):
        guard(request)
        if kind not in tsk.KINDS:
            raise HTTPException(status_code=400, detail=f"unknown task {kind!r}")
        params = {"dry_run": True} if dry_run else {}
        with store() as s:
            task_id = s.queue_task(kind, params, requested_by="dashboard")
        return back(request, "/ops", t("ops.queued", id=task_id, kind=t(f"task.kind.{kind}")))

    @app.get("/ops/tasks/{task_id}", response_class=HTMLResponse)
    def task_detail(request: Request, task_id: int):
        with store() as s:
            row = s.task(task_id)
            if row is None:
                raise HTTPException(status_code=404, detail="no such task")
            labels = industry_labels(s)
        task = dict(row)
        task["params"] = json.loads(task["params_json"] or "{}")
        return page(request, "task.html", industries=labels, task=task, active="ops")

    @app.get("/api/tasks/{task_id}")
    def task_status(task_id: int):
        with store() as s:
            row = s.task(task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such task")
        return {"id": row["id"], "state": row["state"], "finished_at": row["finished_at"]}

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: int):
        with store() as s:
            progress = s.run_progress(run_id)
            if progress["total"] == 0 and not s.conn.execute(
                    "SELECT 1 FROM run WHERE id = ?", (run_id,)).fetchone():
                raise HTTPException(status_code=404, detail="no such run")
            jobs = [dict(r) for r in s.jobs_of(run_id)]
            labels = industry_labels(s)
        return page(request, "run.html", industries=labels, progress=progress,
                    jobs=jobs, active="manage")

    @app.get("/api/runs/{run_id}")
    def run_status(run_id: int):
        with store() as s:
            return s.run_progress(run_id)

    @app.get("/health")
    def health():
        with store() as s:
            return {"ok": True, "runs": len(s.runs(limit=1)), "db": str(s.path)}

    return app
