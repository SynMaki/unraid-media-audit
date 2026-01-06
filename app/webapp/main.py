#!/usr/bin/env python3
"""
Media Audit Web Application v3.1.0
Provides a web UI for running media audits, viewing reports, and configuring settings.

Features:
- WebUI-based settings management (no popups, inline forms)
- Sonarr/Radarr/qBittorrent integration with test connections
- Score breakdown and protection evidence in reports
- Search/filter capabilities
"""

import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any

from fastapi import FastAPI, HTTPException, Depends, Query, Body
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

sys.path.insert(0, str(Path(__file__).parent.parent))
from settings_manager import get_settings_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOG = logging.getLogger("media_audit_webapp")

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
settings = get_settings_manager(CONFIG_DIR)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Job:
    id: str
    status: JobStatus
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    report_run: Optional[str] = None
    error: Optional[str] = None
    logs: List[str] = field(default_factory=list)
    progress: int = 0
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id, "status": self.status.value,
            "started_at": self.started_at, "completed_at": self.completed_at,
            "report_run": self.report_run, "error": self.error,
            "progress": self.progress, "log_count": len(self.logs),
        }


class JobManager:
    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._current_job: Optional[str] = None
    
    def create_job(self) -> Job:
        with self._lock:
            job_id = str(uuid.uuid4())[:8]
            job = Job(id=job_id, status=JobStatus.QUEUED)
            self._jobs[job_id] = job
            return job
    
    def get_job(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)
    
    def list_jobs(self, limit: int = 20) -> List[Job]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.started_at or "", reverse=True)
            return jobs[:limit]
    
    def is_running(self) -> bool:
        return self._current_job is not None
    
    def start_job(self, job: Job, media_audit_path: str) -> bool:
        with self._lock:
            if self._current_job is not None:
                return False
            self._current_job = job.id
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now().isoformat()
        
        thread = threading.Thread(target=self._run_audit, args=(job, media_audit_path), daemon=True)
        thread.start()
        return True
    
    def _run_audit(self, job: Job, media_audit_path: str):
        try:
            cmd = self._build_command(media_audit_path)
            LOG.info(f"Running: {' '.join(cmd)}")
            
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            
            for line in iter(process.stdout.readline, ""):
                line = line.rstrip()
                if line:
                    with self._lock:
                        job.logs.append(line)
                    if "Scanning" in line: job.progress = 10
                    elif "Found" in line and "files" in line: job.progress = 30
                    elif "Grouping" in line: job.progress = 50
                    elif "Scoring" in line: job.progress = 70
                    elif "Generating" in line: job.progress = 80
                    elif "Reports saved to" in line:
                        job.progress = 100
                        match = re.search(r"Reports saved to: (.+)", line)
                        if match: job.report_run = match.group(1).strip()
            
            process.wait()
            
            with self._lock:
                job.status = JobStatus.COMPLETED if process.returncode == 0 else JobStatus.FAILED
                if process.returncode != 0:
                    job.error = f"Exit code: {process.returncode}"
                job.progress = 100
                job.completed_at = datetime.now().isoformat()
                self._current_job = None
                
        except Exception as e:
            LOG.error(f"Audit failed: {e}")
            with self._lock:
                job.status = JobStatus.FAILED
                job.error = str(e)
                job.completed_at = datetime.now().isoformat()
                self._current_job = None
    
    def _build_command(self, media_audit_path: str) -> List[str]:
        cfg = settings.get_all_raw()
        general = cfg.get("general", {})
        qbit = cfg.get("qbittorrent", {})
        sonarr_instances = cfg.get("sonarr_instances", [])
        radarr_instances = cfg.get("radarr_instances", [])
        
        cmd = [sys.executable, media_audit_path]
        
        roots = general.get("roots", ["/media"])
        if roots:
            cmd.append("--roots")
            cmd.extend(roots)
        
        cmd.extend(["--report-dir", general.get("report_dir", "/reports")])
        if general.get("delete_under"):
            cmd.extend(["--delete-under", general["delete_under"]])
        cmd.extend(["--ffprobe-scope", general.get("ffprobe_scope", "dupes")])
        cmd.extend(["--content-type", general.get("content_type", "auto")])
        cmd.extend(["--avoid-mode", general.get("avoid_mode", "if-no-prefer")])
        
        if general.get("avoid_audio_lang"):
            cmd.extend(["--avoid-audio-lang", ",".join(general["avoid_audio_lang"])])
        
        if qbit.get("enabled") and qbit.get("host"):
            cmd.extend(["--qbit-host", qbit["host"]])
            cmd.extend(["--qbit-port", str(qbit.get("port", 8080))])
            if qbit.get("username"): cmd.extend(["--qbit-user", qbit["username"]])
            if qbit.get("password"): cmd.extend(["--qbit-pass", qbit["password"]])
            for mapping in qbit.get("path_mappings", []):
                qp, lp = mapping.get("qbit_path", ""), mapping.get("local_path", "")
                if qp and lp: cmd.extend(["--qbit-path-map", f"{qp}:{lp}"])
        else:
            cmd.append("--no-qbit")
        
        for inst in sonarr_instances:
            if inst.get("enabled") and inst.get("url") and inst.get("api_key"):
                parts = [f"name={inst.get('name', 'sonarr')}", f"url={inst['url']}", f"apikey={inst['api_key']}"]
                for pm in inst.get("path_mappings", []):
                    sp, lp = pm.get("servarr_path", ""), pm.get("local_path", "")
                    if sp and lp: parts.append(f"path_map={sp}:{lp}")
                cmd.extend(["--sonarr", ",".join(parts)])
        
        for inst in radarr_instances:
            if inst.get("enabled") and inst.get("url") and inst.get("api_key"):
                parts = [f"name={inst.get('name', 'radarr')}", f"url={inst['url']}", f"apikey={inst['api_key']}"]
                for pm in inst.get("path_mappings", []):
                    sp, lp = pm.get("servarr_path", ""), pm.get("local_path", "")
                    if sp and lp: parts.append(f"path_map={sp}:{lp}")
                cmd.extend(["--radarr", ",".join(parts)])
        
        if not sonarr_instances and not radarr_instances:
            cmd.append("--no-servarr")
        
        cmd.append("--html-report")
        return cmd


job_manager = JobManager()


# =============================================================================
# FASTAPI APPLICATION
# =============================================================================

app = FastAPI(title="Media Audit", version="3.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
security = HTTPBasic(auto_error=False)


def verify_credentials(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> bool:
    web_cfg = settings.get("web")
    if not web_cfg.get("auth_enabled"):
        return True
    if credentials is None:
        return False
    return (secrets.compare_digest(credentials.username, web_cfg.get("username", "")) and 
            secrets.compare_digest(credentials.password, web_cfg.get("password", "")))


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    if not verify_credentials(credentials):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return True


class RunResponse(BaseModel):
    job_id: str
    status: str
    message: str


@app.get("/", response_class=HTMLResponse)
async def root(authenticated: bool = Depends(require_auth)):
    return get_dashboard_html()


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(authenticated: bool = Depends(require_auth)):
    return get_settings_html()


@app.get("/api/health")
async def health():
    return {"status": "healthy", "version": "3.1.0"}


@app.post("/api/run", response_model=RunResponse)
async def start_run(authenticated: bool = Depends(require_auth)):
    if job_manager.is_running():
        raise HTTPException(status_code=409, detail="An audit is already running")
    job = job_manager.create_job()
    script_dir = Path(__file__).parent.parent
    media_audit_path = str(script_dir / "media_audit.py")
    if not Path(media_audit_path).exists():
        raise HTTPException(status_code=500, detail="media_audit.py not found")
    if not job_manager.start_job(job, media_audit_path):
        raise HTTPException(status_code=409, detail="Failed to start job")
    return RunResponse(job_id=job.id, status=job.status.value, message="Audit started")


@app.get("/api/status/{job_id}")
async def get_job_status(job_id: str, authenticated: bool = Depends(require_auth)):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.get("/api/logs/{job_id}")
async def get_job_logs(job_id: str, offset: int = Query(0, ge=0), authenticated: bool = Depends(require_auth)):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"logs": job.logs[offset:], "total": len(job.logs), "offset": offset, "status": job.status.value}


@app.get("/api/jobs")
async def list_jobs(authenticated: bool = Depends(require_auth)):
    return {"jobs": [j.to_dict() for j in job_manager.list_jobs()]}


@app.get("/api/runs")
async def list_runs(authenticated: bool = Depends(require_auth)):
    report_dir = Path(settings.get("general", "report_dir") or "/reports")
    runs = []
    if report_dir.exists():
        for run_dir in sorted(report_dir.iterdir(), reverse=True):
            if run_dir.is_dir() and run_dir.name.startswith("run-"):
                summary_file = run_dir / "summary.json"
                summary = {}
                if summary_file.exists():
                    try: summary = json.loads(summary_file.read_text())
                    except: pass
                files = {
                    "report.html": (run_dir / "report.html").exists(),
                    "summary.json": summary_file.exists(),
                    "delete_plan.sh": (run_dir / "delete_plan.sh").exists(),
                }
                runs.append({
                    "id": run_dir.name,
                    "timestamp": run_dir.name.replace("run-", ""),
                    "summary": {
                        "scanned_files": summary.get("scanned_files", 0),
                        "episode_duplicate_groups": summary.get("episode_duplicate_groups", 0),
                        "delete_candidates_count": summary.get("delete_candidates_count", 0),
                        "seeding_files_protected": summary.get("seeding_files_protected", 0),
                        "arr_protected": summary.get("arr_protected", 0),
                    },
                    "files": files,
                })
    return {"runs": runs[:50]}


@app.get("/runs/{run_id}/report.html", response_class=HTMLResponse)
async def get_report(run_id: str, authenticated: bool = Depends(require_auth)):
    report_dir = settings.get("general", "report_dir") or "/reports"
    report_path = Path(report_dir) / run_id / "report.html"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    return HTMLResponse(content=report_path.read_text(encoding="utf-8"))


@app.get("/runs/{run_id}/artifact/{filename}")
async def get_artifact(run_id: str, filename: str, authenticated: bool = Depends(require_auth)):
    allowed = {"summary.json", "delete_plan.sh", "files.csv", "episode_duplicates.csv", "delete_candidates.csv"}
    if filename not in allowed:
        raise HTTPException(status_code=400, detail="Invalid filename")
    report_dir = settings.get("general", "report_dir") or "/reports"
    file_path = Path(report_dir) / run_id / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    media_type = "application/json" if filename.endswith(".json") else "text/plain"
    return FileResponse(path=file_path, filename=filename, media_type=media_type)


# =============================================================================
# SETTINGS API
# =============================================================================

@app.get("/api/settings")
async def get_settings(authenticated: bool = Depends(require_auth)):
    return settings.get_all()


@app.put("/api/settings/{section}")
async def update_settings(section: str, data: Dict[str, Any] = Body(...), authenticated: bool = Depends(require_auth)):
    if section not in ["general", "qbittorrent", "web"]:
        raise HTTPException(status_code=400, detail=f"Invalid section: {section}")
    if settings.update(section, data):
        return {"success": True, "message": f"{section} settings updated"}
    raise HTTPException(status_code=500, detail="Failed to update settings")


@app.get("/api/settings/instances/{app_type}")
async def get_instances(app_type: str, authenticated: bool = Depends(require_auth)):
    if app_type not in ["sonarr", "radarr"]:
        raise HTTPException(status_code=400, detail="Invalid app type")
    instances = settings.get(f"{app_type}_instances") or []
    result = []
    for inst in instances:
        inst_copy = dict(inst)
        if inst_copy.get("api_key"):
            key = inst_copy["api_key"]
            inst_copy["api_key_masked"] = key[:4] + "****" + key[-4:] if len(key) > 8 else "********"
        result.append(inst_copy)
    return {"instances": result}


@app.post("/api/settings/instances/{app_type}")
async def add_instance(app_type: str, instance: Dict[str, Any] = Body(...), authenticated: bool = Depends(require_auth)):
    if app_type not in ["sonarr", "radarr"]:
        raise HTTPException(status_code=400, detail="Invalid app type")
    if not instance.get("url") or not instance.get("api_key"):
        raise HTTPException(status_code=400, detail="URL and API key are required")
    if settings.add_instance(app_type, instance):
        return {"success": True, "message": f"{app_type} instance added"}
    raise HTTPException(status_code=400, detail="Failed to add instance")


@app.put("/api/settings/instances/{app_type}/{index}")
async def update_instance(app_type: str, index: int, instance: Dict[str, Any] = Body(...), authenticated: bool = Depends(require_auth)):
    if app_type not in ["sonarr", "radarr"]:
        raise HTTPException(status_code=400, detail="Invalid app type")
    if settings.update_instance(app_type, index, instance):
        return {"success": True, "message": f"{app_type} instance updated"}
    raise HTTPException(status_code=404, detail="Instance not found")


@app.delete("/api/settings/instances/{app_type}/{index}")
async def delete_instance(app_type: str, index: int, authenticated: bool = Depends(require_auth)):
    if app_type not in ["sonarr", "radarr"]:
        raise HTTPException(status_code=400, detail="Invalid app type")
    if settings.remove_instance(app_type, index):
        return {"success": True, "message": f"{app_type} instance removed"}
    raise HTTPException(status_code=404, detail="Instance not found")


@app.post("/api/settings/test/{app_type}")
async def test_connection(app_type: str, config: Dict[str, Any] = Body(...), authenticated: bool = Depends(require_auth)):
    if app_type not in ["sonarr", "radarr", "qbittorrent"]:
        raise HTTPException(status_code=400, detail="Invalid app type")
    return settings.test_connection(app_type, config)


# =============================================================================
# CSS STYLES
# =============================================================================

CSS = """
:root { --bg-primary: #0f0f1a; --bg-secondary: #1a1a2e; --bg-card: #252540; --bg-input: #1e1e35; --text-primary: #f0f0f0; --text-secondary: #a0a0b0; --accent: #6366f1; --accent-hover: #818cf8; --success: #22c55e; --error: #ef4444; --warning: #f59e0b; --border: #3f3f5a; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg-primary); color: var(--text-primary); line-height: 1.6; }
.container { max-width: 1200px; margin: 0 auto; padding: 20px; }
h1 { text-align: center; margin-bottom: 8px; font-size: 1.8rem; background: linear-gradient(135deg, var(--accent), var(--success)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.subtitle { text-align: center; color: var(--text-secondary); margin-bottom: 24px; font-size: 0.9rem; }
.nav { display: flex; justify-content: center; gap: 8px; margin-bottom: 24px; }
.nav a { color: var(--text-secondary); text-decoration: none; padding: 10px 20px; border-radius: 8px; transition: all 0.2s; font-size: 0.9rem; }
.nav a:hover { background: var(--bg-card); color: var(--text-primary); }
.nav a.active { background: var(--accent); color: white; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; margin-bottom: 24px; }
.card { background: var(--bg-card); border-radius: 12px; padding: 20px; border: 1px solid var(--border); }
.card h2 { font-size: 1rem; margin-bottom: 16px; color: var(--text-primary); display: flex; align-items: center; gap: 8px; }
.btn { display: inline-flex; align-items: center; gap: 6px; padding: 10px 18px; border: none; border-radius: 8px; cursor: pointer; font-size: 0.9rem; font-weight: 500; transition: all 0.2s; text-decoration: none; }
.btn-primary { background: var(--accent); color: white; }
.btn-primary:hover { background: var(--accent-hover); transform: translateY(-1px); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
.btn-secondary { background: var(--bg-input); color: var(--text-primary); border: 1px solid var(--border); }
.btn-secondary:hover { border-color: var(--accent); }
.btn-danger { background: var(--error); color: white; }
.btn-danger:hover { background: #dc2626; }
.btn-sm { padding: 6px 12px; font-size: 0.8rem; }
.btn-icon { padding: 6px 10px; }
.log-box { background: var(--bg-primary); border-radius: 8px; padding: 12px; max-height: 280px; overflow-y: auto; font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; white-space: pre-wrap; border: 1px solid var(--border); }
.run-item { display: flex; justify-content: space-between; align-items: center; padding: 12px; background: var(--bg-secondary); border-radius: 8px; margin-bottom: 8px; }
.run-title { font-weight: 600; font-size: 0.9rem; }
.run-stats { font-size: 0.75rem; color: var(--text-secondary); margin-top: 4px; }
.run-actions { display: flex; gap: 8px; }
.run-actions a { color: var(--accent); text-decoration: none; font-size: 0.8rem; }
.empty-state { text-align: center; color: var(--text-secondary); padding: 40px; }
.progress-bar { background: var(--bg-primary); border-radius: 10px; height: 6px; margin-top: 12px; overflow: hidden; }
.progress-fill { background: linear-gradient(90deg, var(--accent), var(--success)); height: 100%; transition: width 0.3s; }
.spinner { width: 16px; height: 16px; border: 2px solid var(--bg-secondary); border-top-color: var(--accent); border-radius: 50%; animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.status-ok { color: var(--success); }
.status-err { color: var(--error); }
.integration-item { display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; background: var(--bg-secondary); border-radius: 8px; margin-bottom: 8px; font-size: 0.85rem; }
.integration-item.ok { border-left: 3px solid var(--success); }
.integration-item.off { border-left: 3px solid var(--text-secondary); }
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin-bottom: 20px; }
.tab { padding: 12px 20px; cursor: pointer; color: var(--text-secondary); font-size: 0.9rem; border-bottom: 2px solid transparent; transition: all 0.2s; }
.tab:hover { color: var(--text-primary); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab-content { display: none; }
.tab-content.active { display: block; }
.form-group { margin-bottom: 16px; }
.form-group label { display: block; margin-bottom: 6px; color: var(--text-secondary); font-size: 0.85rem; }
.form-group input, .form-group select { width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-input); color: var(--text-primary); font-size: 0.9rem; }
.form-group input:focus, .form-group select:focus { outline: none; border-color: var(--accent); }
.form-group small { display: block; margin-top: 4px; color: var(--text-secondary); font-size: 0.75rem; }
.form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 600px) { .form-row { grid-template-columns: 1fr; } }
.instance-card { background: var(--bg-secondary); border-radius: 10px; padding: 16px; margin-bottom: 16px; border: 1px solid var(--border); }
.instance-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid var(--border); }
.instance-title { font-weight: 600; display: flex; align-items: center; gap: 8px; }
.instance-actions { display: flex; gap: 8px; }
.toggle { position: relative; display: inline-block; width: 44px; height: 24px; }
.toggle input { opacity: 0; width: 0; height: 0; }
.toggle-slider { position: absolute; cursor: pointer; inset: 0; background: var(--bg-input); border-radius: 24px; transition: 0.3s; }
.toggle-slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background: white; border-radius: 50%; transition: 0.3s; }
input:checked + .toggle-slider { background: var(--success); }
input:checked + .toggle-slider:before { transform: translateX(20px); }
.path-row { display: grid; grid-template-columns: 1fr 1fr auto; gap: 8px; align-items: center; margin-bottom: 8px; }
.path-row input { padding: 8px 10px; font-size: 0.85rem; }
.alert { padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-size: 0.85rem; display: flex; align-items: center; gap: 8px; }
.alert-success { background: rgba(34, 197, 94, 0.15); border: 1px solid var(--success); color: var(--success); }
.alert-error { background: rgba(239, 68, 68, 0.15); border: 1px solid var(--error); color: var(--error); }
.test-result { margin-top: 8px; padding: 8px 12px; border-radius: 6px; font-size: 0.8rem; }
.test-result.ok { background: rgba(34, 197, 94, 0.15); color: var(--success); }
.test-result.err { background: rgba(239, 68, 68, 0.15); color: var(--error); }
.new-instance-form { background: var(--bg-input); border: 2px dashed var(--border); border-radius: 10px; padding: 16px; margin-bottom: 16px; display: none; }
.new-instance-form.show { display: block; }
.section-actions { display: flex; gap: 8px; margin-top: 16px; }
"""


def get_dashboard_html() -> str:
    cfg = settings.get_all()
    qbit = cfg.get("qbittorrent", {})
    sonarr_count = len([i for i in cfg.get("sonarr_instances", []) if i.get("enabled")])
    radarr_count = len([i for i in cfg.get("radarr_instances", []) if i.get("enabled")])
    
    qbit_ok = qbit.get("enabled") and qbit.get("host")
    
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Media Audit</title>
    <style>{CSS}</style>
</head>
<body>
    <div class="container">
        <h1>📊 Media Audit</h1>
        <p class="subtitle">Find duplicates, compare quality, protect seeding files</p>
        <nav class="nav">
            <a href="/" class="active">Dashboard</a>
            <a href="/settings">Settings</a>
        </nav>
        <div class="grid">
            <div class="card">
                <h2>🚀 Run Audit</h2>
                <p style="color: var(--text-secondary); margin-bottom: 16px; font-size: 0.85rem;">Scan media libraries for duplicates and quality analysis.</p>
                <button id="runBtn" class="btn btn-primary" onclick="startAudit()">▶ Start Audit</button>
                <div id="jobStatus" style="display: none; margin-top: 16px;">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="spinner"></span>
                        <span id="statusText" style="font-size: 0.85rem;">Running...</span>
                    </div>
                    <div class="progress-bar"><div class="progress-fill" id="progressBar" style="width: 0%;"></div></div>
                </div>
            </div>
            <div class="card">
                <h2>🔗 Integrations</h2>
                <div class="integration-item {'ok' if qbit_ok else 'off'}">
                    <span>📥 qBittorrent</span>
                    <span>{'✓ Connected' if qbit_ok else 'Not configured'}</span>
                </div>
                <div class="integration-item {'ok' if sonarr_count else 'off'}">
                    <span>📺 Sonarr</span>
                    <span>{'✓ ' + str(sonarr_count) + ' instance(s)' if sonarr_count else 'Not configured'}</span>
                </div>
                <div class="integration-item {'ok' if radarr_count else 'off'}">
                    <span>🎬 Radarr</span>
                    <span>{'✓ ' + str(radarr_count) + ' instance(s)' if radarr_count else 'Not configured'}</span>
                </div>
                <a href="/settings" class="btn btn-secondary btn-sm" style="margin-top: 12px;">Configure</a>
            </div>
        </div>
        <div class="card" style="margin-bottom: 20px;">
            <h2>📁 Recent Reports</h2>
            <div id="runsList"><div class="empty-state">Loading...</div></div>
        </div>
        <div class="card">
            <h2>📜 Live Logs</h2>
            <div class="log-box" id="logBox">Waiting for audit...</div>
        </div>
    </div>
    <script>
        let currentJobId = null, pollInterval = null;
        async function startAudit() {{
            document.getElementById('runBtn').disabled = true;
            try {{
                const resp = await fetch('/api/run', {{ method: 'POST' }});
                if (!resp.ok) throw new Error((await resp.json()).detail || 'Failed');
                currentJobId = (await resp.json()).job_id;
                document.getElementById('jobStatus').style.display = 'block';
                document.getElementById('logBox').textContent = '';
                pollInterval = setInterval(pollStatus, 1000);
            }} catch (err) {{
                alert('Error: ' + err.message);
                document.getElementById('runBtn').disabled = false;
            }}
        }}
        async function pollStatus() {{
            if (!currentJobId) return;
            try {{
                const status = await (await fetch('/api/status/' + currentJobId)).json();
                document.getElementById('statusText').textContent = status.status + ' (' + status.progress + '%)';
                document.getElementById('progressBar').style.width = status.progress + '%';
                const logs = await (await fetch('/api/logs/' + currentJobId)).json();
                document.getElementById('logBox').textContent = logs.logs.join('\\n');
                document.getElementById('logBox').scrollTop = document.getElementById('logBox').scrollHeight;
                if (status.status === 'completed' || status.status === 'failed') {{
                    clearInterval(pollInterval);
                    document.getElementById('runBtn').disabled = false;
                    document.querySelector('.spinner').style.display = 'none';
                    document.getElementById('statusText').innerHTML = status.status === 'completed' 
                        ? '<span class="status-ok">✓ Completed</span>' 
                        : '<span class="status-err">✗ Failed: ' + (status.error || '') + '</span>';
                    loadRuns();
                }}
            }} catch (err) {{ console.error(err); }}
        }}
        async function loadRuns() {{
            try {{
                const data = await (await fetch('/api/runs')).json();
                const list = document.getElementById('runsList');
                if (!data.runs.length) {{ list.innerHTML = '<div class="empty-state">No reports yet. Run an audit to get started.</div>'; return; }}
                list.innerHTML = data.runs.slice(0, 8).map(r => `
                    <div class="run-item">
                        <div>
                            <div class="run-title">${{r.id}}</div>
                            <div class="run-stats">📁 ${{r.summary.scanned_files}} files · 🔄 ${{r.summary.episode_duplicate_groups}} dupes · 🗑️ ${{r.summary.delete_candidates_count}} deletable · 🌱 ${{r.summary.seeding_files_protected}} seeding</div>
                        </div>
                        <div class="run-actions">
                            ${{r.files['report.html'] ? '<a href="/runs/' + r.id + '/report.html" target="_blank">📊 Report</a>' : ''}}
                            ${{r.files['delete_plan.sh'] ? '<a href="/runs/' + r.id + '/artifact/delete_plan.sh">📜 Script</a>' : ''}}
                        </div>
                    </div>`).join('');
            }} catch (err) {{ document.getElementById('runsList').innerHTML = '<div class="empty-state">Failed to load</div>'; }}
        }}
        loadRuns();
    </script>
</body>
</html>'''


def get_settings_html() -> str:
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Settings - Media Audit</title>
    <style>{CSS}</style>
</head>
<body>
    <div class="container">
        <h1>⚙️ Settings</h1>
        <p class="subtitle">Configure integrations and preferences</p>
        <nav class="nav">
            <a href="/">Dashboard</a>
            <a href="/settings" class="active">Settings</a>
        </nav>
        <div id="alertBox"></div>
        <div class="tabs">
            <div class="tab active" data-tab="general">General</div>
            <div class="tab" data-tab="qbittorrent">qBittorrent</div>
            <div class="tab" data-tab="sonarr">Sonarr</div>
            <div class="tab" data-tab="radarr">Radarr</div>
        </div>
        
        <!-- General Tab -->
        <div id="tab-general" class="tab-content active">
            <div class="card">
                <h2>📋 General Settings</h2>
                <form id="generalForm" onsubmit="saveGeneral(event)">
                    <div class="form-row">
                        <div class="form-group">
                            <label>Report Directory</label>
                            <input type="text" id="g_report_dir" value="/reports">
                            <small>Where reports are saved</small>
                        </div>
                        <div class="form-group">
                            <label>Delete Under</label>
                            <input type="text" id="g_delete_under" value="/media">
                            <small>Only suggest deletions under this path</small>
                        </div>
                    </div>
                    <div class="form-group">
                        <label>Media Roots (comma-separated)</label>
                        <input type="text" id="g_roots" value="/media">
                        <small>Directories to scan for media files</small>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label>FFprobe Scope</label>
                            <select id="g_ffprobe_scope">
                                <option value="none">None</option>
                                <option value="dupes" selected>Duplicates only</option>
                                <option value="all">All files</option>
                            </select>
                        </div>
                        <div class="form-group">
                            <label>Content Type</label>
                            <select id="g_content_type">
                                <option value="auto" selected>Auto-detect</option>
                                <option value="anime">Anime</option>
                                <option value="series">Series</option>
                                <option value="movie">Movie</option>
                            </select>
                        </div>
                    </div>
                    <button type="submit" class="btn btn-primary">💾 Save</button>
                </form>
            </div>
        </div>
        
        <!-- qBittorrent Tab -->
        <div id="tab-qbittorrent" class="tab-content">
            <div class="card">
                <h2>📥 qBittorrent</h2>
                <form id="qbitForm" onsubmit="saveQbit(event)">
                    <div class="form-group">
                        <label class="toggle">
                            <input type="checkbox" id="qb_enabled">
                            <span class="toggle-slider"></span>
                        </label>
                        <span style="margin-left: 8px;">Enable qBittorrent integration</span>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label>Host</label>
                            <input type="text" id="qb_host" placeholder="192.168.1.100">
                        </div>
                        <div class="form-group">
                            <label>Port</label>
                            <input type="number" id="qb_port" value="8080">
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label>Username</label>
                            <input type="text" id="qb_user" placeholder="admin">
                        </div>
                        <div class="form-group">
                            <label>Password</label>
                            <input type="password" id="qb_pass">
                        </div>
                    </div>
                    <div class="form-group">
                        <label>Path Mappings</label>
                        <div id="qb_mappings"></div>
                        <button type="button" class="btn btn-secondary btn-sm" onclick="addQbitMapping()">+ Add Mapping</button>
                    </div>
                    <div class="section-actions">
                        <button type="submit" class="btn btn-primary">💾 Save</button>
                        <button type="button" class="btn btn-secondary" onclick="testQbit()">🔌 Test Connection</button>
                    </div>
                    <div id="qb_test_result"></div>
                </form>
            </div>
        </div>
        
        <!-- Sonarr Tab -->
        <div id="tab-sonarr" class="tab-content">
            <div class="card">
                <h2>📺 Sonarr Instances</h2>
                <div id="sonarr_instances"></div>
                <div id="sonarr_new_form" class="new-instance-form">
                    <h3 style="margin-bottom: 12px; font-size: 0.95rem;">Add New Sonarr Instance</h3>
                    <div class="form-row">
                        <div class="form-group">
                            <label>Name</label>
                            <input type="text" id="sonarr_new_name" placeholder="sonarr-main">
                        </div>
                        <div class="form-group">
                            <label>URL</label>
                            <input type="text" id="sonarr_new_url" placeholder="http://localhost:8989">
                        </div>
                    </div>
                    <div class="form-group">
                        <label>API Key</label>
                        <input type="text" id="sonarr_new_apikey" placeholder="Your Sonarr API key">
                    </div>
                    <div class="section-actions">
                        <button type="button" class="btn btn-primary btn-sm" onclick="saveNewInstance('sonarr')">Add Instance</button>
                        <button type="button" class="btn btn-secondary btn-sm" onclick="cancelNewInstance('sonarr')">Cancel</button>
                    </div>
                </div>
                <button class="btn btn-secondary" onclick="showNewInstance('sonarr')">+ Add Sonarr Instance</button>
            </div>
        </div>
        
        <!-- Radarr Tab -->
        <div id="tab-radarr" class="tab-content">
            <div class="card">
                <h2>🎬 Radarr Instances</h2>
                <div id="radarr_instances"></div>
                <div id="radarr_new_form" class="new-instance-form">
                    <h3 style="margin-bottom: 12px; font-size: 0.95rem;">Add New Radarr Instance</h3>
                    <div class="form-row">
                        <div class="form-group">
                            <label>Name</label>
                            <input type="text" id="radarr_new_name" placeholder="radarr-main">
                        </div>
                        <div class="form-group">
                            <label>URL</label>
                            <input type="text" id="radarr_new_url" placeholder="http://localhost:7878">
                        </div>
                    </div>
                    <div class="form-group">
                        <label>API Key</label>
                        <input type="text" id="radarr_new_apikey" placeholder="Your Radarr API key">
                    </div>
                    <div class="section-actions">
                        <button type="button" class="btn btn-primary btn-sm" onclick="saveNewInstance('radarr')">Add Instance</button>
                        <button type="button" class="btn btn-secondary btn-sm" onclick="cancelNewInstance('radarr')">Cancel</button>
                    </div>
                </div>
                <button class="btn btn-secondary" onclick="showNewInstance('radarr')">+ Add Radarr Instance</button>
            </div>
        </div>
    </div>
    
    <script>
        let settings = {{}};
        
        // Tab switching
        document.querySelectorAll('.tab').forEach(tab => {{
            tab.addEventListener('click', () => {{
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                tab.classList.add('active');
                document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
            }});
        }});
        
        function showAlert(msg, type) {{
            const box = document.getElementById('alertBox');
            box.innerHTML = '<div class="alert alert-' + type + '">' + msg + '</div>';
            setTimeout(() => box.innerHTML = '', 4000);
        }}
        
        async function loadSettings() {{
            settings = await (await fetch('/api/settings')).json();
            const g = settings.general || {{}};
            const q = settings.qbittorrent || {{}};
            
            document.getElementById('g_report_dir').value = g.report_dir || '/reports';
            document.getElementById('g_delete_under').value = g.delete_under || '/media';
            document.getElementById('g_roots').value = (g.roots || []).join(', ');
            document.getElementById('g_ffprobe_scope').value = g.ffprobe_scope || 'dupes';
            document.getElementById('g_content_type').value = g.content_type || 'auto';
            
            document.getElementById('qb_enabled').checked = q.enabled || false;
            document.getElementById('qb_host').value = q.host || '';
            document.getElementById('qb_port').value = q.port || 8080;
            document.getElementById('qb_user').value = q.username || '';
            document.getElementById('qb_pass').value = q.password || '';
            
            const qbm = document.getElementById('qb_mappings');
            qbm.innerHTML = '';
            (q.path_mappings || []).forEach(m => addQbitMappingRow(m.qbit_path, m.local_path));
            
            loadInstances('sonarr');
            loadInstances('radarr');
        }}
        
        function addQbitMapping() {{ addQbitMappingRow('', ''); }}
        function addQbitMappingRow(qp, lp) {{
            const div = document.getElementById('qb_mappings');
            const row = document.createElement('div');
            row.className = 'path-row';
            row.innerHTML = '<input type="text" placeholder="qBit path (e.g. /downloads)" value="' + (qp||'') + '">' +
                '<input type="text" placeholder="Local path (e.g. /media/downloads)" value="' + (lp||'') + '">' +
                '<button type="button" class="btn btn-danger btn-icon btn-sm" onclick="this.parentElement.remove()">✕</button>';
            div.appendChild(row);
        }}
        
        async function saveGeneral(e) {{
            e.preventDefault();
            await fetch('/api/settings/general', {{
                method: 'PUT',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{
                    report_dir: document.getElementById('g_report_dir').value,
                    delete_under: document.getElementById('g_delete_under').value,
                    roots: document.getElementById('g_roots').value.split(',').map(s => s.trim()).filter(s => s),
                    ffprobe_scope: document.getElementById('g_ffprobe_scope').value,
                    content_type: document.getElementById('g_content_type').value,
                }})
            }});
            showAlert('General settings saved!', 'success');
        }}
        
        async function saveQbit(e) {{
            e.preventDefault();
            const mappings = [];
            document.querySelectorAll('#qb_mappings .path-row').forEach(row => {{
                const inputs = row.querySelectorAll('input');
                if (inputs[0].value && inputs[1].value) 
                    mappings.push({{ qbit_path: inputs[0].value, local_path: inputs[1].value }});
            }});
            await fetch('/api/settings/qbittorrent', {{
                method: 'PUT',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{
                    enabled: document.getElementById('qb_enabled').checked,
                    host: document.getElementById('qb_host').value,
                    port: parseInt(document.getElementById('qb_port').value) || 8080,
                    username: document.getElementById('qb_user').value,
                    password: document.getElementById('qb_pass').value,
                    path_mappings: mappings
                }})
            }});
            showAlert('qBittorrent settings saved!', 'success');
        }}
        
        async function testQbit() {{
            const r = document.getElementById('qb_test_result');
            r.innerHTML = '<div class="test-result">Testing...</div>';
            const result = await (await fetch('/api/settings/test/qbittorrent', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ 
                    host: document.getElementById('qb_host').value, 
                    port: parseInt(document.getElementById('qb_port').value),
                    username: document.getElementById('qb_user').value,
                    password: document.getElementById('qb_pass').value
                }})
            }})).json();
            r.innerHTML = result.success 
                ? '<div class="test-result ok">✓ ' + result.message + '</div>'
                : '<div class="test-result err">✗ ' + result.message + '</div>';
        }}
        
        async function loadInstances(appType) {{
            const data = await (await fetch('/api/settings/instances/' + appType)).json();
            renderInstances(appType, data.instances);
        }}
        
        function renderInstances(appType, instances) {{
            const container = document.getElementById(appType + '_instances');
            if (!instances.length) {{
                container.innerHTML = '<p style="color: var(--text-secondary); margin-bottom: 16px;">No instances configured yet.</p>';
                return;
            }}
            container.innerHTML = instances.map((inst, i) => `
                <div class="instance-card" data-index="${{i}}">
                    <div class="instance-header">
                        <div class="instance-title">
                            <label class="toggle">
                                <input type="checkbox" ${{inst.enabled ? 'checked' : ''}} onchange="toggleInstance('${{appType}}', ${{i}}, this.checked)">
                                <span class="toggle-slider"></span>
                            </label>
                            <span>${{inst.name || appType}}</span>
                        </div>
                        <div class="instance-actions">
                            <button class="btn btn-secondary btn-sm" onclick="testInstance('${{appType}}', ${{i}})">🔌 Test</button>
                            <button class="btn btn-danger btn-sm" onclick="deleteInstance('${{appType}}', ${{i}})">🗑️</button>
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label>Name</label>
                            <input type="text" value="${{inst.name || ''}}" onchange="updateInstanceField('${{appType}}', ${{i}}, 'name', this.value)">
                        </div>
                        <div class="form-group">
                            <label>URL</label>
                            <input type="text" value="${{inst.url || ''}}" onchange="updateInstanceField('${{appType}}', ${{i}}, 'url', this.value)">
                        </div>
                    </div>
                    <div class="form-group">
                        <label>API Key</label>
                        <input type="password" value="${{inst.api_key || ''}}" placeholder="${{inst.api_key_masked || 'Enter API key'}}" onchange="updateInstanceField('${{appType}}', ${{i}}, 'api_key', this.value)">
                    </div>
                    <div class="form-group">
                        <label>Path Mappings</label>
                        <div class="inst-mappings" id="${{appType}}_mappings_${{i}}">
                            ${{(inst.path_mappings || []).map((pm, mi) => `
                                <div class="path-row">
                                    <input type="text" value="${{pm.servarr_path || ''}}" placeholder="Servarr path" onchange="updateMapping('${{appType}}', ${{i}}, ${{mi}}, 'servarr_path', this.value)">
                                    <input type="text" value="${{pm.local_path || ''}}" placeholder="Local path" onchange="updateMapping('${{appType}}', ${{i}}, ${{mi}}, 'local_path', this.value)">
                                    <button type="button" class="btn btn-danger btn-icon btn-sm" onclick="removeMapping('${{appType}}', ${{i}}, ${{mi}})">✕</button>
                                </div>
                            `).join('')}}
                        </div>
                        <button type="button" class="btn btn-secondary btn-sm" onclick="addMapping('${{appType}}', ${{i}})">+ Add Mapping</button>
                    </div>
                    <div id="${{appType}}_test_${{i}}"></div>
                </div>
            `).join('');
        }}
        
        async function toggleInstance(appType, idx, enabled) {{
            const instances = (await (await fetch('/api/settings/instances/' + appType)).json()).instances;
            instances[idx].enabled = enabled;
            await fetch('/api/settings/instances/' + appType + '/' + idx, {{
                method: 'PUT',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(instances[idx])
            }});
        }}
        
        async function updateInstanceField(appType, idx, field, value) {{
            const instances = (await (await fetch('/api/settings/instances/' + appType)).json()).instances;
            instances[idx][field] = value;
            await fetch('/api/settings/instances/' + appType + '/' + idx, {{
                method: 'PUT',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(instances[idx])
            }});
        }}
        
        async function testInstance(appType, idx) {{
            const r = document.getElementById(appType + '_test_' + idx);
            r.innerHTML = '<div class="test-result">Testing connection...</div>';
            const instances = (await (await fetch('/api/settings/instances/' + appType)).json()).instances;
            const inst = instances[idx];
            const result = await (await fetch('/api/settings/test/' + appType, {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ url: inst.url, api_key: inst.api_key }})
            }})).json();
            r.innerHTML = result.success 
                ? '<div class="test-result ok">✓ ' + result.message + ' (v' + (result.details?.version || '?') + ')</div>'
                : '<div class="test-result err">✗ ' + result.message + '</div>';
        }}
        
        async function deleteInstance(appType, idx) {{
            if (!confirm('Delete this instance?')) return;
            await fetch('/api/settings/instances/' + appType + '/' + idx, {{ method: 'DELETE' }});
            showAlert(appType + ' instance deleted', 'success');
            loadInstances(appType);
        }}
        
        function showNewInstance(appType) {{
            document.getElementById(appType + '_new_form').classList.add('show');
        }}
        
        function cancelNewInstance(appType) {{
            document.getElementById(appType + '_new_form').classList.remove('show');
            document.getElementById(appType + '_new_name').value = '';
            document.getElementById(appType + '_new_url').value = '';
            document.getElementById(appType + '_new_apikey').value = '';
        }}
        
        async function saveNewInstance(appType) {{
            const name = document.getElementById(appType + '_new_name').value;
            const url = document.getElementById(appType + '_new_url').value;
            const apikey = document.getElementById(appType + '_new_apikey').value;
            
            if (!url || !apikey) {{
                showAlert('URL and API key are required', 'error');
                return;
            }}
            
            const resp = await fetch('/api/settings/instances/' + appType, {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ 
                    enabled: true, 
                    name: name || appType, 
                    url: url, 
                    api_key: apikey,
                    path_mappings: []
                }})
            }});
            
            if (resp.ok) {{
                showAlert(appType + ' instance added!', 'success');
                cancelNewInstance(appType);
                loadInstances(appType);
            }} else {{
                const err = await resp.json();
                showAlert(err.detail || 'Failed to add instance', 'error');
            }}
        }}
        
        async function addMapping(appType, idx) {{
            const instances = (await (await fetch('/api/settings/instances/' + appType)).json()).instances;
            if (!instances[idx].path_mappings) instances[idx].path_mappings = [];
            instances[idx].path_mappings.push({{ servarr_path: '', local_path: '' }});
            await fetch('/api/settings/instances/' + appType + '/' + idx, {{
                method: 'PUT',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(instances[idx])
            }});
            loadInstances(appType);
        }}
        
        async function updateMapping(appType, idx, mapIdx, field, value) {{
            const instances = (await (await fetch('/api/settings/instances/' + appType)).json()).instances;
            if (instances[idx].path_mappings && instances[idx].path_mappings[mapIdx]) {{
                instances[idx].path_mappings[mapIdx][field] = value;
                await fetch('/api/settings/instances/' + appType + '/' + idx, {{
                    method: 'PUT',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(instances[idx])
                }});
            }}
        }}
        
        async function removeMapping(appType, idx, mapIdx) {{
            const instances = (await (await fetch('/api/settings/instances/' + appType)).json()).instances;
            if (instances[idx].path_mappings) {{
                instances[idx].path_mappings.splice(mapIdx, 1);
                await fetch('/api/settings/instances/' + appType + '/' + idx, {{
                    method: 'PUT',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(instances[idx])
                }});
                loadInstances(appType);
            }}
        }}
        
        loadSettings();
    </script>
</body>
</html>'''


# =============================================================================
# MAIN
# =============================================================================

def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    LOG.info(f"Starting Media Audit Web UI v3.1.0 on {host}:{port}")
    LOG.info(f"Config directory: {CONFIG_DIR}")
    cfg = settings.get_all_raw()
    if cfg.get("web", {}).get("auth_enabled"):
        LOG.info("Authentication enabled")
    else:
        LOG.warning("No authentication configured")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
