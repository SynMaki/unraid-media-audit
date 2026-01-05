#!/usr/bin/env python3
"""
Media Audit Web Application
Provides a web UI for running media audits and viewing reports.
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
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any

from fastapi import FastAPI, HTTPException, Depends, Request, Response, Query
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("media_audit_webapp")

# =============================================================================
# CONFIGURATION
# =============================================================================

class Config:
    """Application configuration from environment variables."""
    
    # Web authentication
    WEB_USER: str = os.environ.get("WEB_USER", "")
    WEB_PASS: str = os.environ.get("WEB_PASS", "")
    
    # Media audit settings
    REPORT_DIR: str = os.environ.get("REPORT_DIR", "/reports")
    ROOTS: str = os.environ.get("ROOTS", "/media")
    DELETE_UNDER: str = os.environ.get("DELETE_UNDER", "/media")
    
    # qBittorrent settings
    QBIT_HOST: str = os.environ.get("QBIT_HOST", "")
    QBIT_PORT: str = os.environ.get("QBIT_PORT", "8080")
    QBIT_USER: str = os.environ.get("QBIT_USER", "")
    QBIT_PASS: str = os.environ.get("QBIT_PASS", "")
    QBIT_PATH_MAP: str = os.environ.get("QBIT_PATH_MAP", "")
    QBIT_WEBUI_URL: str = os.environ.get("QBIT_WEBUI_URL", "")
    
    # Audit settings
    FFPROBE_SCOPE: str = os.environ.get("FFPROBE_SCOPE", "dupes")
    CONTENT_TYPE: str = os.environ.get("CONTENT_TYPE", "auto")
    AVOID_MODE: str = os.environ.get("AVOID_MODE", "if-no-prefer")
    AVOID_AUDIO_LANG: str = os.environ.get("AVOID_AUDIO_LANG", "")
    
    # Safety settings - always default to safe (no deletion)
    ALLOW_DELETE: bool = os.environ.get("ALLOW_DELETE", "").lower() == "true"
    
    # Scheduling (disabled by default for safety)
    SCHEDULE_ENABLED: bool = os.environ.get("SCHEDULE_ENABLED", "").lower() == "true"
    SCHEDULE_CRON: str = os.environ.get("SCHEDULE_CRON", "0 3 * * *")  # 3 AM daily


CFG = Config()


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
        self._job_thread: Optional[threading.Thread] = None
    
    def create_job(self) -> Job:
        """Create a new job."""
        with self._lock:
            job_id = str(uuid.uuid4())[:8]
            job = Job(id=job_id, status=JobStatus.QUEUED)
            self._jobs[job_id] = job
            return job
    
    def get_job(self, job_id: str) -> Optional[Job]:
        """Get a job by ID."""
        with self._lock:
            return self._jobs.get(job_id)
    
    def list_jobs(self, limit: int = 20) -> List[Job]:
        """List recent jobs."""
        with self._lock:
            jobs = sorted(self._jobs.values(), 
                         key=lambda j: j.started_at or "", 
                         reverse=True)
            return jobs[:limit]
    
    def is_running(self) -> bool:
        """Check if a job is currently running."""
        with self._lock:
            return self._current_job is not None
    
    def start_job(self, job: Job, media_audit_path: str) -> bool:
        """Start running a job in background."""
        with self._lock:
            if self._current_job is not None:
                return False
            
            self._current_job = job.id
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now().isoformat()
        
        # Start the job in a background thread
        thread = threading.Thread(
            target=self._run_audit,
            args=(job, media_audit_path),
            daemon=True
        )
        thread.start()
        self._job_thread = thread
        return True
    
    def _run_audit(self, job: Job, media_audit_path: str):
        """Run the media audit script."""
        try:
            cmd = self._build_command(media_audit_path)
            LOG.info(f"Running audit command: {' '.join(cmd)}")
            job.logs.append(f"$ {' '.join(cmd)}")
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            
            # Stream output
            for line in iter(process.stdout.readline, ''):
                line = line.rstrip()
                if line:
                    job.logs.append(line)
                    LOG.debug(f"Audit: {line}")
                    
                    # Parse progress from log
                    if "Scanned" in line and "files" in line:
                        match = re.search(r"Scanned (\d+) files", line)
                        if match:
                            job.progress = 30
                    elif "Running ffprobe" in line:
                        job.progress = 50
                    elif "Generating reports" in line:
                        job.progress = 80
                    elif "Reports saved to" in line:
                        job.progress = 100
                        # Extract report path
                        match = re.search(r"Reports saved to: (.+)", line)
                        if match:
                            job.report_run = match.group(1).strip()
            
            process.wait()
            
            with self._lock:
                if process.returncode == 0:
                    job.status = JobStatus.COMPLETED
                    job.progress = 100
                else:
                    job.status = JobStatus.FAILED
                    job.error = f"Exit code: {process.returncode}"
                
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
        """Build the audit command with current configuration."""
        cmd = [sys.executable, media_audit_path]
        
        # Add roots
        roots = [r.strip() for r in CFG.ROOTS.split(",") if r.strip()]
        if roots:
            cmd.append("--roots")
            cmd.extend(roots)
        
        # Add report directory
        cmd.extend(["--report-dir", CFG.REPORT_DIR])
        
        # Add delete-under
        if CFG.DELETE_UNDER:
            cmd.extend(["--delete-under", CFG.DELETE_UNDER])
        
        # Add ffprobe scope
        cmd.extend(["--ffprobe-scope", CFG.FFPROBE_SCOPE])
        
        # Add content type
        cmd.extend(["--content-type", CFG.CONTENT_TYPE])
        
        # Add avoid mode
        cmd.extend(["--avoid-mode", CFG.AVOID_MODE])
        
        # Add avoid audio languages
        if CFG.AVOID_AUDIO_LANG:
            cmd.extend(["--avoid-audio-lang", CFG.AVOID_AUDIO_LANG])
        
        # Add qBittorrent settings if configured
        if CFG.QBIT_HOST:
            cmd.extend(["--qbit-host", CFG.QBIT_HOST])
            cmd.extend(["--qbit-port", CFG.QBIT_PORT])
            if CFG.QBIT_USER:
                cmd.extend(["--qbit-user", CFG.QBIT_USER])
            if CFG.QBIT_PASS:
                cmd.extend(["--qbit-pass", CFG.QBIT_PASS])
            
            # Add path mappings
            if CFG.QBIT_PATH_MAP:
                for mapping in CFG.QBIT_PATH_MAP.split(";"):
                    mapping = mapping.strip()
                    if mapping and ":" in mapping:
                        cmd.extend(["--qbit-path-map", mapping])
        else:
            cmd.append("--no-qbit")
        
        # Always enable HTML report
        cmd.append("--html-report")
        
        # Never enable deletion from web UI (safety)
        # Deletions must be done manually using delete_plan.sh
        
        return cmd


# Global job manager instance
job_manager = JobManager()


# =============================================================================
# FASTAPI APPLICATION
# =============================================================================

app = FastAPI(
    title="Media Audit",
    description="Web UI for Unraid media auditing",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security
security = HTTPBasic(auto_error=False)


def verify_credentials(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> bool:
    """Verify HTTP Basic auth credentials."""
    # If no credentials configured, require local network access
    if not CFG.WEB_USER or not CFG.WEB_PASS:
        # Allow access without auth (for local network)
        return True
    
    if credentials is None:
        return False
    
    correct_username = secrets.compare_digest(credentials.username, CFG.WEB_USER)
    correct_password = secrets.compare_digest(credentials.password, CFG.WEB_PASS)
    
    return correct_username and correct_password


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    """Dependency that requires authentication."""
    if not verify_credentials(credentials):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


# =============================================================================
# API ROUTES
# =============================================================================

class RunRequest(BaseModel):
    """Request model for starting a run."""
    pass  # No extra params needed currently


class RunResponse(BaseModel):
    """Response model for run status."""
    job_id: str
    status: str
    message: str


@app.get("/", response_class=HTMLResponse)
async def root(authenticated: bool = Depends(require_auth)):
    """Serve the main dashboard."""
    return get_dashboard_html()


@app.get("/api/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "version": "1.0.0"}


@app.post("/api/run", response_model=RunResponse)
async def start_run(authenticated: bool = Depends(require_auth)):
    """Start a new audit run."""
    if job_manager.is_running():
        raise HTTPException(status_code=409, detail="An audit is already running")
    
    job = job_manager.create_job()
    
    # Get the media_audit.py path
    script_dir = Path(__file__).parent.parent
    media_audit_path = str(script_dir / "media_audit.py")
    
    if not Path(media_audit_path).exists():
        raise HTTPException(status_code=500, detail="media_audit.py not found")
    
    success = job_manager.start_job(job, media_audit_path)
    
    if not success:
        raise HTTPException(status_code=409, detail="Failed to start job")
    
    return RunResponse(
        job_id=job.id,
        status=job.status.value,
        message="Audit started"
    )


@app.get("/api/status/{job_id}")
async def get_job_status(job_id: str, authenticated: bool = Depends(require_auth)):
    """Get the status of a specific job."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return job.to_dict()


@app.get("/api/logs/{job_id}")
async def get_job_logs(
    job_id: str, 
    offset: int = Query(0, ge=0),
    authenticated: bool = Depends(require_auth)
):
    """Get logs for a specific job."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return {
        "logs": job.logs[offset:],
        "total": len(job.logs),
        "offset": offset,
        "status": job.status.value,
    }


@app.get("/api/jobs")
async def list_jobs(authenticated: bool = Depends(require_auth)):
    """List recent jobs."""
    jobs = job_manager.list_jobs()
    return {"jobs": [j.to_dict() for j in jobs]}


@app.get("/api/runs")
async def list_runs(authenticated: bool = Depends(require_auth)):
    """List available report runs."""
    report_dir = Path(CFG.REPORT_DIR)
    runs = []
    
    if report_dir.exists():
        for run_dir in sorted(report_dir.iterdir(), reverse=True):
            if run_dir.is_dir() and run_dir.name.startswith("run-"):
                # Parse run info
                summary_file = run_dir / "summary.json"
                summary = {}
                if summary_file.exists():
                    try:
                        summary = json.loads(summary_file.read_text())
                    except:
                        pass
                
                # Check available files
                files = {
                    "report.html": (run_dir / "report.html").exists(),
                    "summary.json": summary_file.exists(),
                    "delete_plan.sh": (run_dir / "delete_plan.sh").exists(),
                    "files.csv": (run_dir / "files.csv").exists(),
                    "episode_duplicates.csv": (run_dir / "episode_duplicates.csv").exists(),
                    "delete_candidates.csv": (run_dir / "delete_candidates.csv").exists(),
                }
                
                runs.append({
                    "id": run_dir.name,
                    "path": str(run_dir),
                    "timestamp": run_dir.name.replace("run-", ""),
                    "summary": {
                        "scanned_files": summary.get("scanned_files", 0),
                        "episode_duplicate_groups": summary.get("episode_duplicate_groups", 0),
                        "delete_candidates_count": summary.get("delete_candidates_count", 0),
                        "seeding_files_protected": summary.get("seeding_files_protected", 0),
                    },
                    "files": files,
                })
    
    return {"runs": runs[:50]}  # Limit to 50 most recent


@app.get("/runs/{run_id}/report.html", response_class=HTMLResponse)
async def get_report(run_id: str, authenticated: bool = Depends(require_auth)):
    """Serve the HTML report for a specific run."""
    report_path = Path(CFG.REPORT_DIR) / run_id / "report.html"
    
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    
    return HTMLResponse(content=report_path.read_text(encoding="utf-8"))


@app.get("/runs/{run_id}/artifact/{filename}")
async def get_artifact(run_id: str, filename: str, authenticated: bool = Depends(require_auth)):
    """Serve an artifact file from a specific run."""
    # Validate filename to prevent path traversal
    allowed_files = {
        "summary.json", "delete_plan.sh", "files.csv", 
        "episode_duplicates.csv", "delete_candidates.csv",
        "season_folder_conflicts.csv", "language_flags.csv", "hardlinks.csv"
    }
    
    if filename not in allowed_files:
        raise HTTPException(status_code=400, detail="Invalid filename")
    
    file_path = Path(CFG.REPORT_DIR) / run_id / filename
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    # Determine content type
    if filename.endswith(".json"):
        media_type = "application/json"
    elif filename.endswith(".sh"):
        media_type = "text/x-shellscript"
    elif filename.endswith(".csv"):
        media_type = "text/csv"
    else:
        media_type = "application/octet-stream"
    
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type=media_type
    )


@app.get("/api/config")
async def get_config(authenticated: bool = Depends(require_auth)):
    """Get current configuration (non-sensitive values only)."""
    return {
        "report_dir": CFG.REPORT_DIR,
        "roots": CFG.ROOTS.split(","),
        "delete_under": CFG.DELETE_UNDER,
        "ffprobe_scope": CFG.FFPROBE_SCOPE,
        "content_type": CFG.CONTENT_TYPE,
        "avoid_mode": CFG.AVOID_MODE,
        "qbit_configured": bool(CFG.QBIT_HOST),
        "qbit_webui_url": CFG.QBIT_WEBUI_URL,
        "allow_delete": CFG.ALLOW_DELETE,
        "schedule_enabled": CFG.SCHEDULE_ENABLED,
    }


# =============================================================================
# DASHBOARD HTML
# =============================================================================

def get_dashboard_html() -> str:
    """Generate the dashboard HTML."""
    qbit_status = "Configured" if CFG.QBIT_HOST else "Not configured"
    qbit_link = f'<a href="{CFG.QBIT_WEBUI_URL}" target="_blank" class="qbit-link">Open qBittorrent</a>' if CFG.QBIT_WEBUI_URL else ""
    
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Media Audit Dashboard</title>
    <style>
        :root {{
            --bg-primary: #1a1a2e;
            --bg-secondary: #16213e;
            --bg-card: #0f3460;
            --text-primary: #eee;
            --text-secondary: #aaa;
            --accent-green: #00d26a;
            --accent-red: #ff6b6b;
            --accent-yellow: #ffd93d;
            --accent-blue: #6bcbff;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
            padding: 20px;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        h1 {{ 
            text-align: center; 
            margin-bottom: 10px;
            background: linear-gradient(135deg, var(--accent-blue), var(--accent-green));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            font-size: 2rem;
        }}
        .subtitle {{ text-align: center; color: var(--text-secondary); margin-bottom: 30px; }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .card {{
            background: var(--bg-card);
            border-radius: 12px;
            padding: 20px;
        }}
        .card h2 {{ 
            font-size: 1.2rem; 
            margin-bottom: 15px; 
            color: var(--accent-blue);
        }}
        .btn {{
            display: inline-block;
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 1rem;
            font-weight: bold;
            transition: all 0.2s;
            text-decoration: none;
        }}
        .btn-primary {{
            background: linear-gradient(135deg, var(--accent-blue), var(--accent-green));
            color: var(--bg-primary);
        }}
        .btn-primary:hover {{ transform: translateY(-2px); box-shadow: 0 4px 15px rgba(107, 203, 255, 0.4); }}
        .btn-primary:disabled {{ opacity: 0.5; cursor: not-allowed; transform: none; }}
        .btn-secondary {{
            background: var(--bg-secondary);
            color: var(--text-primary);
            border: 1px solid var(--bg-card);
        }}
        .status-running {{ color: var(--accent-yellow); }}
        .status-completed {{ color: var(--accent-green); }}
        .status-failed {{ color: var(--accent-red); }}
        .status-queued {{ color: var(--text-secondary); }}
        .log-box {{
            background: var(--bg-primary);
            border-radius: 8px;
            padding: 15px;
            max-height: 300px;
            overflow-y: auto;
            font-family: monospace;
            font-size: 0.85rem;
            white-space: pre-wrap;
            word-break: break-all;
        }}
        .run-item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px;
            background: var(--bg-secondary);
            border-radius: 8px;
            margin-bottom: 10px;
        }}
        .run-item:hover {{ background: var(--bg-primary); }}
        .run-info {{ flex: 1; }}
        .run-title {{ font-weight: bold; color: var(--accent-blue); }}
        .run-stats {{ font-size: 0.85rem; color: var(--text-secondary); }}
        .run-actions {{ display: flex; gap: 10px; }}
        .run-actions a {{
            padding: 6px 12px;
            background: var(--bg-card);
            color: var(--text-primary);
            text-decoration: none;
            border-radius: 6px;
            font-size: 0.8rem;
        }}
        .run-actions a:hover {{ background: var(--accent-blue); color: var(--bg-primary); }}
        .config-item {{
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid var(--bg-secondary);
        }}
        .config-label {{ color: var(--text-secondary); }}
        .config-value {{ font-family: monospace; }}
        .progress-bar {{
            width: 100%;
            height: 8px;
            background: var(--bg-secondary);
            border-radius: 4px;
            overflow: hidden;
            margin-top: 10px;
        }}
        .progress-fill {{
            height: 100%;
            background: linear-gradient(90deg, var(--accent-blue), var(--accent-green));
            border-radius: 4px;
            transition: width 0.3s;
        }}
        .qbit-link {{
            display: inline-block;
            padding: 8px 16px;
            background: linear-gradient(135deg, #2980b9, #3498db);
            color: white;
            text-decoration: none;
            border-radius: 8px;
            font-size: 0.9rem;
            margin-top: 10px;
        }}
        .qbit-link:hover {{ opacity: 0.9; }}
        .empty-state {{
            text-align: center;
            color: var(--text-secondary);
            padding: 30px;
        }}
        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}
        .spinner {{
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid var(--bg-secondary);
            border-top-color: var(--accent-blue);
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Media Audit Dashboard</h1>
        <p class="subtitle">Scan media libraries for duplicates, manage quality upgrades</p>
        
        <div class="grid">
            <!-- Run Audit Card -->
            <div class="card">
                <h2>🚀 Run Audit</h2>
                <p style="color: var(--text-secondary); margin-bottom: 15px;">
                    Start a new scan of your media libraries to find duplicates and quality issues.
                </p>
                <button id="runBtn" class="btn btn-primary" onclick="startAudit()">
                    ▶️ Start Audit
                </button>
                
                <div id="jobStatus" style="display: none; margin-top: 20px;">
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <span class="spinner"></span>
                        <span id="statusText">Running...</span>
                    </div>
                    <div class="progress-bar">
                        <div class="progress-fill" id="progressBar" style="width: 0%;"></div>
                    </div>
                </div>
            </div>
            
            <!-- Configuration Card -->
            <div class="card">
                <h2>⚙️ Configuration</h2>
                <div class="config-item">
                    <span class="config-label">Report Directory</span>
                    <span class="config-value">{CFG.REPORT_DIR}</span>
                </div>
                <div class="config-item">
                    <span class="config-label">Media Roots</span>
                    <span class="config-value">{CFG.ROOTS[:50]}...</span>
                </div>
                <div class="config-item">
                    <span class="config-label">FFprobe Scope</span>
                    <span class="config-value">{CFG.FFPROBE_SCOPE}</span>
                </div>
                <div class="config-item">
                    <span class="config-label">qBittorrent</span>
                    <span class="config-value">{qbit_status}</span>
                </div>
                {qbit_link}
            </div>
        </div>
        
        <!-- Recent Runs -->
        <div class="card" style="margin-bottom: 20px;">
            <h2>📁 Recent Reports</h2>
            <div id="runsList">
                <div class="empty-state">Loading...</div>
            </div>
        </div>
        
        <!-- Logs -->
        <div class="card">
            <h2>📜 Live Logs</h2>
            <div class="log-box" id="logBox">Waiting for audit to start...</div>
        </div>
    </div>
    
    <script>
        let currentJobId = null;
        let pollInterval = null;
        
        async function startAudit() {{
            const btn = document.getElementById('runBtn');
            btn.disabled = true;
            
            try {{
                const resp = await fetch('/api/run', {{ method: 'POST' }});
                if (!resp.ok) {{
                    const err = await resp.json();
                    throw new Error(err.detail || 'Failed to start audit');
                }}
                
                const data = await resp.json();
                currentJobId = data.job_id;
                
                document.getElementById('jobStatus').style.display = 'block';
                document.getElementById('logBox').textContent = '';
                
                // Start polling
                pollInterval = setInterval(pollStatus, 1000);
                
            }} catch (err) {{
                alert('Error: ' + err.message);
                btn.disabled = false;
            }}
        }}
        
        async function pollStatus() {{
            if (!currentJobId) return;
            
            try {{
                // Get status
                const statusResp = await fetch(`/api/status/${{currentJobId}}`);
                const status = await statusResp.json();
                
                document.getElementById('statusText').textContent = 
                    `${{status.status}} (${{status.progress}}%)`;
                document.getElementById('progressBar').style.width = status.progress + '%';
                
                // Get logs
                const logsResp = await fetch(`/api/logs/${{currentJobId}}`);
                const logs = await logsResp.json();
                document.getElementById('logBox').textContent = logs.logs.join('\\n');
                document.getElementById('logBox').scrollTop = document.getElementById('logBox').scrollHeight;
                
                // Check if done
                if (status.status === 'completed' || status.status === 'failed') {{
                    clearInterval(pollInterval);
                    pollInterval = null;
                    
                    document.getElementById('runBtn').disabled = false;
                    document.querySelector('.spinner').style.display = 'none';
                    
                    if (status.status === 'completed') {{
                        document.getElementById('statusText').innerHTML = 
                            '<span class="status-completed">✓ Completed</span>';
                        loadRuns();
                    }} else {{
                        document.getElementById('statusText').innerHTML = 
                            '<span class="status-failed">✗ Failed: ' + (status.error || 'Unknown error') + '</span>';
                    }}
                }}
                
            }} catch (err) {{
                console.error('Poll error:', err);
            }}
        }}
        
        async function loadRuns() {{
            try {{
                const resp = await fetch('/api/runs');
                const data = await resp.json();
                
                const list = document.getElementById('runsList');
                
                if (data.runs.length === 0) {{
                    list.innerHTML = '<div class="empty-state">No reports yet. Run an audit to get started!</div>';
                    return;
                }}
                
                list.innerHTML = data.runs.slice(0, 10).map(run => `
                    <div class="run-item">
                        <div class="run-info">
                            <div class="run-title">${{run.id}}</div>
                            <div class="run-stats">
                                📁 ${{run.summary.scanned_files.toLocaleString()}} files |
                                🔄 ${{run.summary.episode_duplicate_groups}} dupe groups |
                                🗑️ ${{run.summary.delete_candidates_count}} deletable |
                                🌱 ${{run.summary.seeding_files_protected}} protected
                            </div>
                        </div>
                        <div class="run-actions">
                            ${{run.files['report.html'] ? `<a href="/runs/${{run.id}}/report.html" target="_blank">📊 Report</a>` : ''}}
                            ${{run.files['delete_plan.sh'] ? `<a href="/runs/${{run.id}}/artifact/delete_plan.sh">📜 Delete Script</a>` : ''}}
                            ${{run.files['summary.json'] ? `<a href="/runs/${{run.id}}/artifact/summary.json">📋 Summary</a>` : ''}}
                        </div>
                    </div>
                `).join('');
                
            }} catch (err) {{
                console.error('Failed to load runs:', err);
                document.getElementById('runsList').innerHTML = 
                    '<div class="empty-state">Failed to load reports</div>';
            }}
        }}
        
        // Initial load
        loadRuns();
    </script>
</body>
</html>'''


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Run the web application."""
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    
    LOG.info(f"Starting Media Audit Web UI on {host}:{port}")
    LOG.info(f"Report directory: {CFG.REPORT_DIR}")
    LOG.info(f"Media roots: {CFG.ROOTS}")
    LOG.info(f"qBittorrent: {CFG.QBIT_HOST}:{CFG.QBIT_PORT}" if CFG.QBIT_HOST else "qBittorrent: Not configured")
    
    if CFG.WEB_USER and CFG.WEB_PASS:
        LOG.info("Authentication enabled")
    else:
        LOG.warning("No authentication configured - access is open")
    
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
