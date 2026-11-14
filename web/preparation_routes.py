"""Authenticated draft workflow; all mutations use the dashboard's guard."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, PlainTextResponse, JSONResponse

from . import preparation as prep, sales
from .preparation_sender import VENDORS, parse_sender


def register(app, drafts, settings, guard, page, store):
    def target(target_id):
        with store() as s:
            row = s.target(target_id)
            if row is None:
                raise HTTPException(404, "기업을 찾을 수 없습니다.")
            return dict(row)

    def context(target_id):
        with store() as s:
            row = s.target(target_id)
            card = sales.company_cards(s, sales.INDUSTRIES, target_id)[0]
            # Explicit allowlist: no target notes, contact details or raw cookies.
            return {"company": row["company"], "industry": row["industry"],
                    "urls": json.loads(row["urls_json"]), "analysis_since": settings.analysis.since,
                    "captured_at": prep.now(), "last_good": card["last_good"],
                    "observation_status": card["label"], "limitation": card["reason"],
                    "observed_solutions": [{k: t[k] for k in ("name", "label", "last_seen")}
                                           for t in card["tools"]],
                    "changes": [{k: e.get(k) for k in ("name", "label", "confidence", "previous_date", "latest", "detected_count", "missing_count", "contact_allowed")}
                                for e in card["events"]]}

    def enabled():
        if not settings.preparation.enabled:
            raise HTTPException(404, "제안 준비 기능이 활성화되지 않았습니다.")

    def redirect(target_id):
        return RedirectResponse(f"/companies/{target_id}/prepare", status_code=303)

    def render(request, target_id, error="", status_code=200):
        row = target(target_id)
        saved = drafts.get(target_id)
        for item in saved["history"]:
            item["display_at"] = datetime.fromisoformat(item["saved_at"]).astimezone(ZoneInfo("Asia/Seoul")).strftime("%m.%d %H:%M")
        response = page(request, "preparation_vendor.html", c=row, saved=saved, d=saved["document"],
                        goals=prep.GOALS, channels=prep.CHANNELS, capabilities=prep.CAPABILITIES,
                        max_capabilities=prep.MAX_CAPABILITIES, steps=prep.STEPS,
                        error=error, active="companies", sender_vendors=VENDORS)
        response.status_code = status_code
        return response

    @app.get("/preparation/notifications")
    def notifications():
        enabled()
        with drafts.connect() as db:
            rows = db.execute("""SELECT j.id, j.target_id, j.step, j.state, j.finished_at,
                j.started_at, t.company FROM preparation_job j JOIN target t ON t.id=j.target_id
                ORDER BY j.id DESC LIMIT 30""").fetchall()
        items, targets = [], set()
        for row in rows:
            if row["target_id"] in targets:
                continue
            targets.add(row["target_id"])
            state = row["state"]
            action = "제안서·메일" if row["step"] in ("package", "proposal", "email") else "회사 조사·추천"
            label = {"done": "완료", "failed": "실패", "running": "진행 중"}.get(state, state)
            anchor = "documents" if row["step"] == "package" and state == "done" else "usecases" if state == "done" else "research"
            items.append({"id": row["id"], "company": row["company"], "state": state,
                "label": f"{action} {label}", "at": row["finished_at"] or row["started_at"],
                "url": f"/companies/{row['target_id']}/prepare#{anchor}"})
        return JSONResponse({"items": items}, headers={"Cache-Control": "no-store"})

    @app.get("/companies/{target_id}/prepare")
    def prepare(request: Request, target_id: str):
        enabled()
        return render(request, target_id)

    @app.get("/companies/{target_id}/prepare/status")
    def status(target_id: str):
        enabled()
        target(target_id)
        saved = drafts.get(target_id)
        return {"revision": saved["revision"], "job": saved["job"]}

    @app.get("/companies/{target_id}/prepare/history/{revision}")
    def history(target_id: str, revision: int):
        enabled()
        row = target(target_id)
        doc = drafts.history(target_id, revision)
        if doc is None:
            raise HTTPException(404, "저장 이력을 찾을 수 없습니다.")
        text = f"{row['company']} · 이전 초안 v{revision}\n현재 제안과 다른 내용일 수 있습니다.\n\n"
        text += "제안 방향\n" + ("회사 조사·KB 기반 자동 추천" if doc["inputs"].get("automatic") else prep.GOALS[doc["inputs"]["goal"]][0]) + "\n" + doc["inputs"]["description"] + "\n\n"
        for uc in doc["usecases"]:
            text += uc["title"] + "\n" + uc["text"] + "\n\n"
        text += "제안서\n" + doc["proposal"] + "\n\n메일 제목: " + doc["email"]["subject"] + "\n" + doc["email"]["text"]
        for source in doc["sources"]:
            text += f"\n\n[{source['id']}] {source['title']}\n" + "\n".join(link["url"] for link in source["links"])
        return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="draft-v{revision}.txt"'})

    @app.post("/companies/{target_id}/prepare/save")
    def save(request: Request, target_id: str):
        enabled()
        guard(request)
        target(target_id)
        # Old tabs cannot silently modify documents from the read-only workflow.
        raise HTTPException(409, "직접 편집 기능은 종료되었습니다. 새로고침 후 추천 유즈케이스를 선택해 주세요.")

    @app.post("/companies/{target_id}/prepare/generate")
    async def generate(request: Request, target_id: str):
        enabled()
        guard(request)
        target(target_id)
        form = await request.form(max_fields=12, max_part_size=16000)
        try:
            step = str(form.get("step", ""))
            revision = int(form.get("revision", "-1"))
            selected = [int(i) for i in form.getlist("selected")]
            sender = parse_sender(str(form.get("vendor")), str(form.get("sender_name", "")), str(form.get("sender_context", ""))) if step == "usecases" and "vendor" in form else None
            drafts.submit(target_id, revision, step, context(target_id) if step == "usecases" else {},
                          settings.preparation, selected_indices=selected, sender=sender)
        except (ValueError, prep.PrepError) as exc:
            message = str(exc) if isinstance(exc, prep.PrepError) else "선택한 내용을 확인해 주세요."
            return render(request, target_id, message, 409)
        return redirect(target_id)

    def current_document(target_id):
        saved = drafts.get(target_id)
        if saved["job"] and saved["job"]["state"] == "running":
            raise HTTPException(409, "생성이 끝난 뒤 최신 문서를 내려받아 주세요.")
        return saved["document"]

    @app.get("/companies/{target_id}/prepare/export/{step}")
    def export(target_id: str, step: str):
        enabled()
        row = target(target_id)
        doc = current_document(target_id)
        if step not in ("proposal", "email") or step in doc["stale"] or not doc[step] or (step == "email" and not doc["email"]["text"]):
            raise HTTPException(409, "유즈케이스를 선택하고 제안서·메일을 생성한 뒤 내보내 주세요.")
        if step == "email":
            text = f"제목: {doc['email']['subject']}\n\n{doc['email']['text']}"
        else:
            text = f"{row['company']} 제안서 초안\n고객과 논의할 가설이며 운영 현황·성과를 확정하지 않습니다.\n\n{doc['proposal']}\n\n참고 자료\n"
            for source in prep.selected_sources(doc):
                text += f"\n[{source['id']}] {source['title']}\n" + "\n".join(link["url"] for link in source["links"]) + "\n"
        return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="{step}-draft.txt"'})

    @app.get("/companies/{target_id}/prepare/print")
    def print_proposal(request: Request, target_id: str):
        enabled()
        row = target(target_id)
        doc = current_document(target_id)
        if not doc["proposal"] or "proposal" in doc["stale"]:
            raise HTTPException(409, "최신 제안서 초안을 먼저 생성해 주세요.")
        return page(request, "proposal_print.html", c=row, d=doc,
                    sources=prep.selected_sources(doc))
