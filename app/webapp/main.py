#!/usr/bin/env python3
"""
Media Audit Web Application
Provides a web UI for running media audits, viewing reports, and configuring settings.

Version 3.0.0 - Now with WebUI-based settings management!
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

# Import settings manager
sys.path.insert(0, str(Path(__file__).parent.parent))
from settings_manager import get_settings_manager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("media_audit_webapp")

# Settings initialization
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
settings = get_settings_manager(CONFIG_DIR)


# =============================================================================
# JOB MANAGEMENT
# =============================================================================

class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
            "id": self.id,
            "status": self.status.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "report_run": self.report_run,
            "error": self.error,
            "progress": self.progress,
            "log_count": len(self.logs),
        }


class JobManager:
    """Simple thread-based job manager."""
    
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
        with self._lock:
            return self._jobs.get(job_id)
    
    def list_jobs(self, limit: int = 20) -> List[Job]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.started_at or "", reverse=True)
            return jobs[:limit]
    
    def is_running(self) -> bool:
        with self._lock:
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
                    if "Scanning" in line:
                        job.progress = 10
                    elif "Found" in line and "files" in line:
                        job.progress = 30
                    elif "Grouping" in line:
                        job.progress = 50
                    elif "Scoring" in line:
                        job.progress = 70
                    elif "Generating" in line:
                        job.progress = 80
                    elif "Reports saved to" in line:
                        job.progress = 100
                        match = re.search(r"Reports saved to: (.+)", line)
                        if match:
                            job.report_run = match.group(1).strip()
            
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
        
        # qBittorrent
        if qbit.get("enabled") and qbit.get("host"):
            cmd.extend(["--qbit-host", qbit["host"]])
            cmd.extend(["--qbit-port", str(qbit.get("port", 8080))])
            if qbit.get("username"):
                cmd.extend(["--qbit-user", qbit["username"]])
            if qbit.get("password"):
                cmd.extend(["--qbit-pass", qbit["password"]])
            for mapping in qbit.get("path_mappings", []):
                qp = mapping.get("qbit_path", "")
                lp = mapping.get("local_path", "")
                if qp and lp:
                    cmd.extend(["--qbit-path-map", f"{qp}:{lp}"])
        else:
            cmd.append("--no-qbit")
        
        # Sonarr
        for inst in sonarr_instances:
            if inst.get("enabled") and inst.get("url") and inst.get("api_key"):
                parts = [f"name={inst.get('name', 'sonarr')}", f"url={inst['url']}", f"apikey={inst['api_key']}"]
                for pm in inst.get("path_mappings", []):
                    sp = pm.get("servarr_path", "")
                    lp = pm.get("local_path", "")
                    if sp and lp:
                        parts.append(f"path_map={sp}:{lp}")
                cmd.extend(["--sonarr", ",".join(parts)])
        
        # Radarr
        for inst in radarr_instances:
            if inst.get("enabled") and inst.get("url") and inst.get("api_key"):
                parts = [f"name={inst.get('name', 'radarr')}", f"url={inst['url']}", f"apikey={inst['api_key']}"]
                for pm in inst.get("path_mappings", []):
                    sp = pm.get("servarr_path", "")
                    lp = pm.get("local_path", "")
                    if sp and lp:
                        parts.append(f"path_map={sp}:{lp}")
                cmd.extend(["--radarr", ",".join(parts)])
        
        if not sonarr_instances and not radarr_instances:
            cmd.append("--no-servarr")
        
        cmd.append("--html-report")
        return cmd


job_manager = JobManager()


# =============================================================================
# FASTAPI APPLICATION
# =============================================================================

app = FastAPI(title="Media Audit", description="Web UI for Unraid media auditing", version="3.0.0")
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
    return {"status": "healthy", "version": "3.0.0"}


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
                    try:
                        summary = json.loads(summary_file.read_text())
                    except:
                        pass
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
            inst_copy["api_key"] = key[:4] + "****" + key[-4:] if len(key) > 8 else "********"
        result.append(inst_copy)
    return {"instances": result}


@app.post("/api/settings/instances/{app_type}")
async def add_instance(app_type: str, instance: Dict[str, Any] = Body(...), authenticated: bool = Depends(require_auth)):
    if app_type not in ["sonarr", "radarr"]:
        raise HTTPException(status_code=400, detail="Invalid app type")
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
# HTML TEMPLATES
# =============================================================================

CSS = """
:root { --bg-primary: #1a1a2e; --bg-secondary: #16213e; --bg-card: #0f3460; --text-primary: #eee; --text-secondary: #aaa; --accent-green: #00d26a; --accent-red: #ff6b6b; --accent-yellow: #ffd93d; --accent-blue: #6bcbff; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg-primary); color: var(--text-primary); line-height: 1.6; padding: 20px; }
.container { max-width: 1200px; margin: 0 auto; }
h1 { text-align: center; margin-bottom: 10px; background: linear-gradient(135deg, var(--accent-blue), var(--accent-green)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-size: 2rem; }
.subtitle { text-align: center; color: var(--text-secondary); margin-bottom: 20px; }
.nav { display: flex; justify-content: center; gap: 20px; margin-bottom: 30px; }
.nav a { color: var(--text-secondary); text-decoration: none; padding: 10px 20px; border-radius: 8px; transition: all 0.2s; }
.nav a:hover { background: var(--bg-card); color: var(--accent-blue); }
.nav a.active { background: var(--bg-card); color: var(--accent-blue); font-weight: bold; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin-bottom: 30px; }
.card { background: var(--bg-card); border-radius: 12px; padding: 20px; }
.card h2 { font-size: 1.2rem; margin-bottom: 15px; color: var(--accent-blue); }
.btn { display: inline-block; padding: 12px 24px; border: none; border-radius: 8px; cursor: pointer; font-size: 1rem; font-weight: bold; transition: all 0.2s; text-decoration: none; }
.btn-primary { background: linear-gradient(135deg, var(--accent-blue), var(--accent-green)); color: var(--bg-primary); }
.btn-primary:hover { transform: translateY(-2px); box-shadow: 0 4px 15px rgba(107, 203, 255, 0.4); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
.btn-secondary { background: var(--bg-secondary); color: var(--text-primary); border: 1px solid var(--bg-card); }
.btn-danger { background: var(--accent-red); color: white; }
.btn-small { padding: 6px 12px; font-size: 0.85rem; }
.log-box { background: var(--bg-primary); border-radius: 8px; padding: 15px; max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 0.85rem; white-space: pre-wrap; }
.run-item { display: flex; justify-content: space-between; align-items: center; padding: 12px; background: var(--bg-secondary); border-radius: 8px; margin-bottom: 10px; }
.run-title { font-weight: bold; color: var(--accent-blue); }
.run-stats { font-size: 0.85rem; color: var(--text-secondary); }
.run-actions a { color: var(--accent-blue); text-decoration: none; margin-left: 15px; }
.empty-state { text-align: center; color: var(--text-secondary); padding: 40px; }
.progress-bar { background: var(--bg-primary); border-radius: 10px; height: 8px; margin-top: 10px; overflow: hidden; }
.progress-fill { background: linear-gradient(90deg, var(--accent-blue), var(--accent-green)); height: 100%; transition: width 0.3s; }
.spinner { width: 20px; height: 20px; border: 3px solid var(--bg-secondary); border-top-color: var(--accent-blue); border-radius: 50%; animation: spin 1s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }
.status-completed { color: var(--accent-green); }
.status-failed { color: var(--accent-red); }
.integration-status { display: flex; align-items: center; gap: 8px; padding: 8px 12px; background: var(--bg-secondary); border-radius: 6px; margin-bottom: 8px; }
.integration-status.connected { border-left: 3px solid var(--accent-green); }
.integration-status.disconnected { border-left: 3px solid var(--text-secondary); }
.tabs { display: flex; gap: 5px; margin-bottom: 20px; border-bottom: 2px solid var(--bg-card); padding-bottom: 10px; flex-wrap: wrap; }
.tab { padding: 10px 20px; cursor: pointer; border-radius: 8px 8px 0 0; color: var(--text-secondary); transition: all 0.2s; }
.tab:hover { background: var(--bg-secondary); }
.tab.active { background: var(--bg-card); color: var(--accent-blue); font-weight: bold; }
.tab-content { display: none; }
.tab-content.active { display: block; }
.form-group { margin-bottom: 20px; }
.form-group label { display: block; margin-bottom: 8px; color: var(--text-secondary); font-size: 0.9rem; }
.form-group input, .form-group select { width: 100%; padding: 12px; border: 1px solid var(--bg-card); border-radius: 8px; background: var(--bg-secondary); color: var(--text-primary); font-size: 1rem; }
.form-group input:focus, .form-group select:focus { outline: none; border-color: var(--accent-blue); }
.form-group small { display: block; margin-top: 5px; color: var(--text-secondary); font-size: 0.8rem; }
.form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
@media (max-width: 600px) { .form-row { grid-template-columns: 1fr; } }
.instance-card { background: var(--bg-secondary); border-radius: 8px; padding: 15px; margin-bottom: 15px; border-left: 3px solid var(--accent-blue); }
.instance-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; flex-wrap: wrap; gap: 10px; }
.instance-title { font-weight: bold; color: var(--accent-blue); }
.toggle-switch { position: relative; display: inline-block; width: 50px; height: 26px; }
.toggle-switch input { opacity: 0; width: 0; height: 0; }
.toggle-slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: var(--bg-primary); transition: .4s; border-radius: 26px; }
.toggle-slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 4px; bottom: 4px; background-color: white; transition: .4s; border-radius: 50%; }
input:checked + .toggle-slider { background-color: var(--accent-green); }
input:checked + .toggle-slider:before { transform: translateX(24px); }
.path-mapping { display: grid; grid-template-columns: 1fr 1fr auto; gap: 10px; align-items: center; margin-bottom: 10px; }
.path-mapping input { padding: 8px; }
.alert { padding: 15px; border-radius: 8px; margin-bottom: 20px; }
.alert-success { background: rgba(0, 210, 106, 0.2); border: 1px solid var(--accent-green); color: var(--accent-green); }
.alert-error { background: rgba(255, 107, 107, 0.2); border: 1px solid var(--accent-red); color: var(--accent-red); }
.test-result { margin-top: 10px; padding: 10px; border-radius: 6px; font-size: 0.9rem; }
.test-result.success { background: rgba(0, 210, 106, 0.2); color: var(--accent-green); }
.test-result.error { background: rgba(255, 107, 107, 0.2); color: var(--accent-red); }
"""


def get_dashboard_html() -> str:
    cfg = settings.get_all()
    qbit = cfg.get("qbittorrent", {})
    sonarr_count = len(cfg.get("sonarr_instances", []))
    radarr_count = len(cfg.get("radarr_instances", []))
    
    qbit_status = "connected" if qbit.get("enabled") and qbit.get("host") else "disconnected"
    sonarr_status = "connected" if sonarr_count > 0 else "disconnected"
    radarr_status = "connected" if radarr_count > 0 else "disconnected"
    
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Media Audit Dashboard</title>
    <style>{CSS}</style>
</head>
<body>
    <div class="container">
        <h1>📊 Media Audit</h1>
        <p class="subtitle">Scan for duplicates, analyze quality, protect seeding files</p>
        <nav class="nav">
            <a href="/" class="active">🏠 Dashboard</a>
            <a href="/settings">⚙️ Settings</a>
        </nav>
        <div class="grid">
            <div class="card">
                <h2>🚀 Run Audit</h2>
                <p style="color: var(--text-secondary); margin-bottom: 15px;">Start a new scan of your media libraries.</p>
                <button id="runBtn" class="btn btn-primary" onclick="startAudit()">▶️ Start Audit</button>
                <div id="jobStatus" style="display: none; margin-top: 20px;">
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <span class="spinner"></span>
                        <span id="statusText">Running...</span>
                    </div>
                    <div class="progress-bar"><div class="progress-fill" id="progressBar" style="width: 0%;"></div></div>
                </div>
            </div>
            <div class="card">
                <h2>🔗 Integrations</h2>
                <div class="integration-status {qbit_status}">📥 qBittorrent: {"✓ Connected" if qbit_status == "connected" else "Not configured"}</div>
                <div class="integration-status {sonarr_status}">📺 Sonarr: {"✓ " + str(sonarr_count) + " instance(s)" if sonarr_count else "Not configured"}</div>
                <div class="integration-status {radarr_status}">🎬 Radarr: {"✓ " + str(radarr_count) + " instance(s)" if radarr_count else "Not configured"}</div>
                <a href="/settings" class="btn btn-secondary" style="margin-top: 15px;">⚙️ Configure</a>
            </div>
        </div>
        <div class="card" style="margin-bottom: 20px;">
            <h2>📁 Recent Reports</h2>
            <div id="runsList"><div class="empty-state">Loading...</div></div>
        </div>
        <div class="card">
            <h2>📜 Live Logs</h2>
            <div class="log-box" id="logBox">Waiting for audit to start...</div>
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
                        ? '<span class="status-completed">✓ Completed</span>' 
                        : '<span class="status-failed">✗ Failed: ' + (status.error || '') + '</span>';
                    loadRuns();
                }}
            }} catch (err) {{ console.error(err); }}
        }}
        async function loadRuns() {{
            try {{
                const data = await (await fetch('/api/runs')).json();
                const list = document.getElementById('runsList');
                if (!data.runs.length) {{ list.innerHTML = '<div class="empty-state">No reports yet.</div>'; return; }}
                list.innerHTML = data.runs.slice(0, 10).map(r => `
                    <div class="run-item">
                        <div><div class="run-title">${{r.id}}</div>
                        <div class="run-stats">📁 ${{r.summary.scanned_files}} files | 🔄 ${{r.summary.episode_duplicate_groups}} dupes | 🗑️ ${{r.summary.delete_candidates_count}} deletable | 🌱 ${{r.summary.seeding_files_protected}} seeding | 📺 ${{r.summary.arr_protected || 0}} arr</div></div>
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
        <p class="subtitle">Configure Media Audit integrations and preferences</p>
        <nav class="nav">
            <a href="/">🏠 Dashboard</a>
            <a href="/settings" class="active">⚙️ Settings</a>
        </nav>
        <div id="alertContainer"></div>
        <div class="tabs">
            <div class="tab active" data-tab="general">📋 General</div>
            <div class="tab" data-tab="qbittorrent">📥 qBittorrent</div>
            <div class="tab" data-tab="sonarr">📺 Sonarr</div>
            <div class="tab" data-tab="radarr">🎬 Radarr</div>
        </div>
        
        <!-- General Tab -->
        <div id="tab-general" class="tab-content active">
            <div class="card">
                <h2>📋 General Settings</h2>
                <form id="generalForm">
                    <div class="form-row">
                        <div class="form-group"><label>Report Directory</label><input type="text" name="report_dir" id="report_dir" value="/reports"><small>Where to save reports</small></div>
                        <div class="form-group"><label>Delete Under</label><input type="text" name="delete_under" id="delete_under" value="/media"><small>Only allow deletions here</small></div>
                    </div>
                    <div class="form-group"><label>Media Roots (comma-separated)</label><input type="text" name="roots" id="roots" value="/media"><small>Directories to scan</small></div>
                    <div class="form-row">
                        <div class="form-group"><label>FFprobe Scope</label><select name="ffprobe_scope" id="ffprobe_scope"><option value="none">None</option><option value="dupes">Duplicates only</option><option value="all">All files</option></select></div>
                        <div class="form-group"><label>Content Type</label><select name="content_type" id="content_type"><option value="auto">Auto-detect</option><option value="anime">Anime</option><option value="series">Series</option><option value="movie">Movie</option></select></div>
                    </div>
                    <button type="submit" class="btn btn-primary">💾 Save</button>
                </form>
            </div>
        </div>
        
        <!-- qBittorrent Tab -->
        <div id="tab-qbittorrent" class="tab-content">
            <div class="card">
                <h2>📥 qBittorrent</h2>
                <form id="qbitForm">
                    <div class="form-group"><label><label class="toggle-switch"><input type="checkbox" name="enabled" id="qbit_enabled"><span class="toggle-slider"></span></label> Enable qBittorrent</label></div>
                    <div class="form-row">
                        <div class="form-group"><label>Host</label><input type="text" id="qbit_host" placeholder="192.168.1.39"></div>
                        <div class="form-group"><label>Port</label><input type="number" id="qbit_port" value="8080"></div>
                    </div>
                    <div class="form-row">
                        <div class="form-group"><label>Username</label><input type="text" id="qbit_username"></div>
                        <div class="form-group"><label>Password</label><input type="password" id="qbit_password"></div>
                    </div>
                    <div class="form-group"><label>Path Mappings</label><div id="qbit_path_mappings"></div><button type="button" class="btn btn-secondary btn-small" onclick="addQbitMapping()">+ Add</button></div>
                    <div style="display: flex; gap: 10px;"><button type="submit" class="btn btn-primary">💾 Save</button><button type="button" class="btn btn-secondary" onclick="testQbit()">🔌 Test</button></div>
                    <div id="qbitTestResult"></div>
                </form>
            </div>
        </div>
        
        <!-- Sonarr Tab -->
        <div id="tab-sonarr" class="tab-content">
            <div class="card">
                <h2>📺 Sonarr Instances</h2>
                <div id="sonarrInstances"><p style="color: var(--text-secondary);">Loading...</p></div>
                <button class="btn btn-primary" onclick="addInstance('sonarr')">+ Add Sonarr</button>
            </div>
        </div>
        
        <!-- Radarr Tab -->
        <div id="tab-radarr" class="tab-content">
            <div class="card">
                <h2>🎬 Radarr Instances</h2>
                <div id="radarrInstances"><p style="color: var(--text-secondary);">Loading...</p></div>
                <button class="btn btn-primary" onclick="addInstance('radarr')">+ Add Radarr</button>
            </div>
        </div>
    </div>
    <script>
        let currentSettings = {{}};
        
        document.querySelectorAll('.tab').forEach(tab => {{
            tab.addEventListener('click', () => {{
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                tab.classList.add('active');
                document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
            }});
        }});
        
        async function loadSettings() {{
            currentSettings = await (await fetch('/api/settings')).json();
            const g = currentSettings.general || {{}};
            const q = currentSettings.qbittorrent || {{}};
            document.getElementById('report_dir').value = g.report_dir || '/reports';
            document.getElementById('delete_under').value = g.delete_under || '/media';
            document.getElementById('roots').value = (g.roots || []).join(', ');
            document.getElementById('ffprobe_scope').value = g.ffprobe_scope || 'dupes';
            document.getElementById('content_type').value = g.content_type || 'auto';
            document.getElementById('qbit_enabled').checked = q.enabled || false;
            document.getElementById('qbit_host').value = q.host || '';
            document.getElementById('qbit_port').value = q.port || 8080;
            document.getElementById('qbit_username').value = q.username || '';
            document.getElementById('qbit_password').value = q.password || '';
            document.getElementById('qbit_path_mappings').innerHTML = '';
            (q.path_mappings || []).forEach(m => addQbitMappingRow(m.qbit_path, m.local_path));
            loadInstances('sonarr');
            loadInstances('radarr');
        }}
        
        function addQbitMapping() {{ addQbitMappingRow('', ''); }}
        function addQbitMappingRow(qp, lp) {{
            const div = document.getElementById('qbit_path_mappings');
            const row = document.createElement('div');
            row.className = 'path-mapping';
            row.innerHTML = '<input type="text" placeholder="qBit path" value="' + qp + '"><input type="text" placeholder="Local path" value="' + lp + '"><button type="button" class="btn btn-danger btn-small" onclick="this.parentElement.remove()">✕</button>';
            div.appendChild(row);
        }}
        
        document.getElementById('generalForm').addEventListener('submit', async (e) => {{
            e.preventDefault();
            await fetch('/api/settings/general', {{
                method: 'PUT',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{
                    report_dir: document.getElementById('report_dir').value,
                    delete_under: document.getElementById('delete_under').value,
                    roots: document.getElementById('roots').value.split(',').map(s => s.trim()).filter(s => s),
                    ffprobe_scope: document.getElementById('ffprobe_scope').value,
                    content_type: document.getElementById('content_type').value,
                }})
            }});
            showAlert('Settings saved!', 'success');
        }});
        
        document.getElementById('qbitForm').addEventListener('submit', async (e) => {{
            e.preventDefault();
            const mappings = [];
            document.querySelectorAll('#qbit_path_mappings .path-mapping').forEach(row => {{
                const inputs = row.querySelectorAll('input');
                if (inputs[0].value && inputs[1].value) mappings.push({{ qbit_path: inputs[0].value, local_path: inputs[1].value }});
            }});
            await fetch('/api/settings/qbittorrent', {{
                method: 'PUT',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{
                    enabled: document.getElementById('qbit_enabled').checked,
                    host: document.getElementById('qbit_host').value,
                    port: parseInt(document.getElementById('qbit_port').value) || 8080,
                    username: document.getElementById('qbit_username').value,
                    password: document.getElementById('qbit_password').value,
                    path_mappings: mappings
                }})
            }});
            showAlert('qBittorrent settings saved!', 'success');
        }});
        
        async function testQbit() {{
            const r = document.getElementById('qbitTestResult');
            r.innerHTML = '<div class="test-result">Testing...</div>';
            const result = await (await fetch('/api/settings/test/qbittorrent', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ host: document.getElementById('qbit_host').value, port: parseInt(document.getElementById('qbit_port').value) }})
            }})).json();
            r.innerHTML = result.success 
                ? '<div class="test-result success">✓ ' + result.message + ' (v' + result.details.version + ')</div>'
                : '<div class="test-result error">✗ ' + result.message + '</div>';
        }}
        
        async function loadInstances(appType) {{
            const data = await (await fetch('/api/settings/instances/' + appType)).json();
            renderInstances(appType, data.instances);
        }}
        
        function renderInstances(appType, instances) {{
            const c = document.getElementById(appType + 'Instances');
            if (!instances.length) {{ c.innerHTML = '<p style="color: var(--text-secondary);">No instances configured.</p>'; return; }}
            c.innerHTML = instances.map((inst, i) => `
                <div class="instance-card">
                    <div class="instance-header">
                        <span class="instance-title">${{inst.name || appType}}</span>
                        <div><button class="btn btn-secondary btn-small" onclick="testInstance('${{appType}}', ${{i}}, this)">🔌 Test</button>
                        <button class="btn btn-danger btn-small" onclick="deleteInstance('${{appType}}', ${{i}})">🗑️</button></div>
                    </div>
                    <form onsubmit="saveInstance(event, '${{appType}}', ${{i}})">
                        <div class="form-group"><label><label class="toggle-switch"><input type="checkbox" name="enabled" ${{inst.enabled ? 'checked' : ''}}><span class="toggle-slider"></span></label> Enabled</label></div>
                        <div class="form-row">
                            <div class="form-group"><label>Name</label><input type="text" name="name" value="${{inst.name || ''}}" required></div>
                            <div class="form-group"><label>URL</label><input type="text" name="url" value="${{inst.url || ''}}" required></div>
                        </div>
                        <div class="form-group"><label>API Key</label><input type="text" name="api_key" value="${{inst.api_key || ''}}" required></div>
                        <div class="form-group"><label>Path Mappings</label>
                            <div class="inst-mappings" data-app="${{appType}}" data-idx="${{i}}">
                                ${{(inst.path_mappings || []).map(pm => '<div class="path-mapping"><input type="text" value="' + (pm.servarr_path || '') + '"><input type="text" value="' + (pm.local_path || '') + '"><button type="button" class="btn btn-danger btn-small" onclick="this.parentElement.remove()">✕</button></div>').join('')}}
                            </div>
                            <button type="button" class="btn btn-secondary btn-small" onclick="addInstMapping('${{appType}}', ${{i}})">+ Add</button>
                        </div>
                        <button type="submit" class="btn btn-primary btn-small">💾 Save</button>
                        <span class="test-result-${{appType}}-${{i}}"></span>
                    </form>
                </div>`).join('');
        }}
        
        function addInstMapping(app, idx) {{
            const c = document.querySelector('.inst-mappings[data-app="' + app + '"][data-idx="' + idx + '"]');
            const row = document.createElement('div');
            row.className = 'path-mapping';
            row.innerHTML = '<input type="text" placeholder="Servarr path"><input type="text" placeholder="Local path"><button type="button" class="btn btn-danger btn-small" onclick="this.parentElement.remove()">✕</button>';
            c.appendChild(row);
        }}
        
        async function saveInstance(e, appType, idx) {{
            e.preventDefault();
            const form = e.target;
            const mappings = [];
            form.querySelectorAll('.path-mapping').forEach(row => {{
                const inputs = row.querySelectorAll('input');
                if (inputs[0].value && inputs[1].value) mappings.push({{ servarr_path: inputs[0].value, local_path: inputs[1].value }});
            }});
            await fetch('/api/settings/instances/' + appType + '/' + idx, {{
                method: 'PUT',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{
                    enabled: form.querySelector('[name="enabled"]').checked,
                    name: form.querySelector('[name="name"]').value,
                    url: form.querySelector('[name="url"]').value,
                    api_key: form.querySelector('[name="api_key"]').value,
                    path_mappings: mappings
                }})
            }});
            showAlert(appType + ' saved!', 'success');
            loadInstances(appType);
        }}
        
        async function addInstance(appType) {{
            const name = prompt('Instance name:', appType);
            if (!name) return;
            const url = prompt('URL:', 'http://localhost:' + (appType === 'sonarr' ? '8989' : '7878'));
            if (!url) return;
            const apiKey = prompt('API Key:');
            if (!apiKey) return;
            await fetch('/api/settings/instances/' + appType, {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ enabled: true, name, url, api_key: apiKey, path_mappings: [] }})
            }});
            showAlert(appType + ' added!', 'success');
            loadInstances(appType);
        }}
        
        async function deleteInstance(appType, idx) {{
            if (!confirm('Delete this instance?')) return;
            await fetch('/api/settings/instances/' + appType + '/' + idx, {{ method: 'DELETE' }});
            showAlert(appType + ' deleted!', 'success');
            loadInstances(appType);
        }}
        
        async function testInstance(appType, idx, btn) {{
            const card = btn.closest('.instance-card');
            const r = card.querySelector('.test-result-' + appType + '-' + idx) || card.querySelector('span[class^="test-result"]');
            if (r) r.innerHTML = ' Testing...';
            const form = card.querySelector('form');
            const result = await (await fetch('/api/settings/test/' + appType, {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ url: form.querySelector('[name="url"]').value, api_key: form.querySelector('[name="api_key"]').value }})
            }})).json();
            if (r) r.innerHTML = result.success ? ' <span class="status-completed">✓ v' + result.details.version + '</span>' : ' <span class="status-failed">✗ ' + result.message + '</span>';
        }}
        
        function showAlert(msg, type) {{
            const c = document.getElementById('alertContainer');
            c.innerHTML = '<div class="alert alert-' + type + '">' + msg + '</div>';
            setTimeout(() => c.innerHTML = '', 5000);
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
    LOG.info(f"Starting Media Audit Web UI v3.0.0 on {host}:{port}")
    LOG.info(f"Config directory: {CONFIG_DIR}")
    cfg = settings.get_all_raw()
    if cfg.get("web", {}).get("auth_enabled"):
        LOG.info("Authentication enabled")
    else:
        LOG.warning("No authentication configured - access is open")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
