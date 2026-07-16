#!/usr/bin/env python3
"""Small HTTP resolver for platform session metadata."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from typing import Any, Dict

from fastapi import BackgroundTasks, FastAPI, HTTPException

app = FastAPI()

SESSION_KV_DB_PATH = os.getenv("SESSION_KV_DB_PATH", "/var/lib/kube-agents/session/session_kv.db")


def init_db() -> None:
    db_dir = os.path.dirname(SESSION_KV_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_metadata (
                session_id TEXT PRIMARY KEY,
                metadata TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/sessions", status_code=201)
def create_session() -> Dict[str, str]:
    """Create a new session ID for the incoming incident."""
    session_id = f"k8s-evt-{uuid.uuid4().hex[:8]}"
    
    # Save the session to the local metadata DB
    with sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0) as conn:
        conn.execute(
            "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
            (session_id, json.dumps({"platform": "k8s-watcher", "created_at": datetime.utcnow().isoformat()}))
        )
    return {"sessionID": session_id}

def get_severity_emoji(event_type: str, reason: str) -> str:
    if event_type.lower() == "warning":
        critical_reasons = {"oomkilled", "crashloopbackoff", "errimagepull", "imagepullbackoff", "failedmount"}
        if reason.lower() in critical_reasons:
            return "🔴" # Critical
        return "🟡" # Warning
    return "🔵" # Normal/Info



def get_active_platform() -> str:
    try:
        import yaml
        with open("/opt/data/config.yaml", "r") as f:
            cfg = yaml.safe_load(f) or {}
        platforms = cfg.get("platforms", {})
        if platforms.get("slack", {}).get("enabled"):
            return "slack"
        if platforms.get("google_chat", {}).get("enabled"):
            return "google_chat"
    except Exception as exc:
        print(f"Failed to parse config.yaml for active platform: {exc}")
    if os.environ.get("SLACK_BOT_TOKEN"):
        return "slack"
    return "google_chat"


def trigger_agent_troubleshooter(session_id: str, alert_msg: str, payload: Dict[str, Any]) -> None:
    """Post the warning alert to the active chat platform, then call local gateway API to execute agent loop."""
    active_platform = get_active_platform()
    # 1. Trigger the red alert warning to Chat with --json to parse message_id
    thread_id = None
    try:
        res = subprocess.run(
            ["hermes", "send", "--json", "--to", active_platform, alert_msg],
            check=True,
            capture_output=True,
            text=True
        )
        resp = json.loads(res.stdout)
        msg_id = resp.get("message_id", "")
        if msg_id:
            if active_platform == "google_chat" and "/messages/" in msg_id:
                space_part, msg_part = msg_id.split("/messages/", 1)
                thread_key = msg_part.split(".")[0]
                thread_id = f"{space_part}/threads/{thread_key}"
            else:
                thread_id = msg_id
    except Exception as exc:
        print(f"Failed to post warning alert or parse response: {exc}")

    # Update metadata DB with the parsed thread ID
    if thread_id:
        try:
            with sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0) as conn:
                row = conn.execute(
                    "SELECT metadata FROM session_metadata WHERE session_id = ?",
                    (session_id,)
                ).fetchone()
                if row:
                    meta = json.loads(row[0])
                    meta["thread_id"] = thread_id
                    if active_platform == "slack":
                        meta["chat_id"] = os.environ.get("SLACK_HOME_CHANNEL", "")
                    else:
                        meta["chat_id"] = thread_id.split("/threads/")[0]
                    conn.execute(
                        "UPDATE session_metadata SET metadata = ? WHERE session_id = ?",
                        (json.dumps(meta), session_id)
                    )
        except Exception as exc:
            print(f"Failed to update session metadata with thread_id: {exc}")

    # 2. Call local gateway API to run troubleshooter
    api_url = "http://localhost:8642"
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("API_SERVER_KEY", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Create session inside gateway if it doesn't exist
    try:
        req = urllib.request.Request(
            f"{api_url}/api/sessions",
            data=json.dumps({"session_id": session_id, "title": f"Triage {session_id}"}).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            pass
    except urllib.error.HTTPError as exc:
        if exc.code != 409:  # 409 Conflict means it already exists, which is fine
            print(f"Failed to create gateway API session (code {exc.code}): {exc.read().decode()}")
            return
    except Exception as exc:
        print(f"Failed to connect to gateway API server: {exc}")
        return

    event_reason = payload.get("reason", "Unknown")
    namespace = payload.get("namespace", "default")
    object_kind = payload.get("kind_of_object") or payload.get("kindOfObject") or "Pod"
    object_name = payload.get("name", "")
    message = payload.get("message", "")

    # Trigger agent execution turn in the session
    agent_query = (
        f"Analyze the following Kubernetes event warning on GKE cluster '{os.environ.get('GKE_CLUSTER_NAME', 'platform-agent-host')}' "
        f"for the active session '{session_id}'.\n\n"
        f"**Event Details:**\n"
        f"• *Resource:* {namespace}/{object_kind}/{object_name}\n"
        f"• *Event Reason:* {event_reason}\n"
        f"• *Warning Message:* {message}\n\n"
        f"When calling your send_notification tool to report findings, you MUST pass this exact session ID: '{session_id}' as the session_id argument so it routes as a threaded reply to the warning alert.\n\n"
        f"When done, post your final diagnostic report to the chat platform (using your notification tool) formatted exactly like this:\n\n"
        f"📋 *Incident Triage*\n\n"
        f"• *Issue:* <Short 1-sentence description of the problem>\n"
        f"• *Root Cause:* <Key constraint mismatch or log finding in 1-2 sentences>\n\n"
        f"🛠️ *Remediation:*\n"
        f"*Option A (Scale Up):* <Remediation details>\n"
        f"`<Exact scale command>`\n\n"
        f"*Option B (Patch PDB):* <Remediation details>\n"
        f"`<Exact patch command>`\n\n"
        f"🔗 [GKE Workloads](https://console.cloud.google.com/kubernetes/workload/overview?project={os.environ.get('GCP_PROJECT', 'jayantid-gkedemos')}) | "
        f"[Cloud Logs](https://console.cloud.google.com/logs/query;query=resource.type%3D%22k8s_container%22?project={os.environ.get('GCP_PROJECT', 'jayantid-gkedemos')})\n\n"
        f"👉 *Reply to this thread with 'apply Option A' or 'apply Option B' to automatically execute the remediation.*\n\n"
        f"---"
        f"\n\n**Remediation Instructions (For subsequent turns if the user replies):**\n"
        f"If the user replies to the thread with 'apply Option A' or 'apply Option B':\n"
        f"1. You are explicitly authorized to execute the selected mutation (kubectl scale or kubectl patch) directly on the cluster in this namespace to resolve the blocker.\n"
        f"2. Run the command, verify if it succeeded, and post a threaded response using your send_notification tool confirming the remediation status.\n"
        f"3. Do not ask for further confirmation or explain your write boundaries; simply execute the command and confirm."
    )
    try:
        req = urllib.request.Request(
            f"{api_url}/api/sessions/{session_id}/chat",
            data=json.dumps({"message": agent_query}).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            if resp.status != 200:
                print(f"Gateway API chat execution failed (status {resp.status})")
    except Exception as exc:
        print(f"Failed to call gateway API chat execution: {exc}")


@app.post("/sessions/{session_id}/inject")
def inject_message(session_id: str, request_data: Dict[str, Any], background_tasks: BackgroundTasks) -> Dict[str, str]:
    """Receive the event payload and notify the Platform Agent via Google Chat."""
    raw_message = request_data.get("message", "")
    if not raw_message:
        raise HTTPException(status_code=400, detail="message field is required")
        
    try:
        payload = json.loads(raw_message)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse inner payload JSON: {exc}")
        
    event_reason = payload.get("reason", "Unknown")
    namespace = payload.get("namespace", "default")
    object_kind = payload.get("kind_of_object") or payload.get("kindOfObject") or "Pod"
    object_name = payload.get("name", "")
    message = payload.get("message", "")
    count = payload.get("count", 1)
    event_type = payload.get("type", "Warning")

    severity_emoji = get_severity_emoji(event_type, event_reason)

    # Construct a pretty notification alert
    alert_msg = (
        f"{severity_emoji} *{event_reason}* · `{namespace}/{object_kind}/{object_name}` ({count}x)\n"
        f"> {message}\n\n"
        f"🤖 _Autonomous diagnostic run started..._"
    )
    
    # Delegate the heavy REST API call to FastAPI BackgroundTasks to keep response times sub-millisecond
    background_tasks.add_task(trigger_agent_troubleshooter, session_id, alert_msg, payload)
    
    return {"status": "injected"}


@app.get("/v1/sessions/{session_id}/metadata")
def get_metadata(session_id: str) -> Dict[str, Any]:
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    with sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0) as conn:
        row = conn.execute(
            "SELECT metadata FROM session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Session metadata not found")

    try:
        return json.loads(row[0])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Data decoding failure: {exc}")


@app.get("/v1/sessions")
def list_sessions(limit: int = 100) -> Dict[str, Any]:
    limit = max(1, min(limit, 1000))
    with sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0) as conn:
        rows = conn.execute(
            """
            SELECT session_id, metadata, updated_at
            FROM session_metadata
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    sessions = []
    for session_id, metadata, updated_at in rows:
        try:
            parsed = json.loads(metadata)
        except Exception:
            parsed = {}
        sessions.append(
            {
                "session_id": session_id,
                "metadata": parsed,
                "updated_at": updated_at,
            }
        )
    return {"sessions": sessions}


init_db()
