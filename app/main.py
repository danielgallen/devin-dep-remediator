"""FastAPI entrypoint: webhook ingress, dashboard, and background jobs
(session poller + periodic dependency scan) wired via APScheduler."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import metrics
from app.config import settings
from app.orchestrator import github, poll_sessions
from app.scan_job import run_scan
from app.webhook import router as webhook_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

app = FastAPI(title="Devin Dependency Remediator")
app.include_router(webhook_router)
templates = Jinja2Templates(directory="app/templates")

scheduler = BackgroundScheduler()


@app.on_event("startup")
def start_background_jobs():
    # PUT .../labels requires the labels to already exist on the repo -- make
    # that true idempotently on boot instead of relying on someone having
    # created them by hand at some point. Best-effort: a GitHub outage or a
    # bad token here shouldn't take down the whole service (dashboard,
    # poller, webhook route all still have value even if label bootstrap
    # fails) -- it'll just fail loudly again next boot, or the labels can be
    # created by hand in the meantime.
    try:
        github.ensure_labels_exist(
            {
                settings.trigger_label: "1d76db",
                settings.in_progress_label: "60a5fa",
                settings.done_label: "4ade80",
                settings.blocked_label: "fbbf24",
                settings.failed_label: "f87171",
            }
        )
    except Exception:
        log.exception("Failed to ensure GitHub labels exist on startup -- continuing anyway")
    scheduler.add_job(poll_sessions, "interval", seconds=settings.poll_interval_seconds, id="poll_sessions")
    if settings.scan_interval_seconds > 0:
        scheduler.add_job(run_scan, "interval", seconds=settings.scan_interval_seconds, id="scan_dependencies")
    scheduler.start()
    log.info(
        "Background jobs started: poll every %ss, scan every %ss",
        settings.poll_interval_seconds,
        settings.scan_interval_seconds,
    )


@app.on_event("shutdown")
def stop_background_jobs():
    scheduler.shutdown(wait=False)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    m = metrics.compute()
    return templates.TemplateResponse("dashboard.html", {"request": request, "m": m, "repo": settings.github_repo})


@app.get("/api/metrics")
def api_metrics():
    return metrics.compute()


@app.get("/")
def root():
    return {"service": "devin-dep-remediator", "dashboard": "/dashboard", "webhook": "/webhooks/github"}
