"""
app.py — ResumeVerify Flask Application
========================================
Environment variables:
  SECRET_KEY       Flask secret key (required in production)
  UPLOAD_FOLDER    Where uploads are temporarily stored (default: uploads)
  REPORTS_FOLDER   Where PDF reports are saved (default: reports)
  MAX_UPLOAD_MB    Max upload size in MB (default: 16)
  FLASK_DEBUG      Set to 1 for debug mode
  PORT             Port to listen on (default: 5000)
"""

from __future__ import annotations

import logging
import os
import traceback
import uuid
from pathlib import Path

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

from cert_checker import process_resume

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("app")


# ---------------------------------------------------------------------------
# FLASK APP SETUP
# ---------------------------------------------------------------------------

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)

UPLOAD_FOLDER  = os.environ.get("UPLOAD_FOLDER",  "uploads")
REPORTS_FOLDER = os.environ.get("REPORTS_FOLDER", "reports")
MAX_UPLOAD_MB  = int(os.environ.get("MAX_UPLOAD_MB", 16))

app.config["UPLOAD_FOLDER"]      = UPLOAD_FOLDER
app.config["REPORTS_FOLDER"]     = REPORTS_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# Expose reports folder to cert_checker
os.environ.setdefault("RESUME_REPORTS_DIR", REPORTS_FOLDER)

Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)
Path(REPORTS_FOLDER).mkdir(parents=True, exist_ok=True)

ALLOWED_RESUME_EXT = {"pdf"}
ALLOWED_PROOF_EXT  = {"pdf", "png", "jpg", "jpeg"}


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _allowed(filename: str, allowed: set[str]) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed


def _save_upload(file_storage, folder: str, allowed_ext: set[str]) -> str | None:
    """
    Saves a FileStorage object to `folder` with a UUID-prefixed filename
    to prevent collisions. Returns the saved path, or None on failure.
    """
    if not file_storage or file_storage.filename == "":
        return None
    if not _allowed(file_storage.filename, allowed_ext):
        return None
    safe_name = f"{uuid.uuid4().hex}_{secure_filename(file_storage.filename)}"
    path = os.path.join(folder, safe_name)
    file_storage.save(path)
    log.info("Saved upload: %s", path)
    return path


def _cleanup(*paths: str | None) -> None:
    """Delete temporary upload files after processing."""
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
                log.debug("Cleaned up temp file: %s", p)
            except OSError as exc:
                log.warning("Could not delete temp file %s: %s", p, exc)


# ---------------------------------------------------------------------------
# FLASH MESSAGE HELPER
# ---------------------------------------------------------------------------

def _flash_messages_html() -> str:
    """Render flashed messages in templates via get_flashed_messages()."""
    return ""  # Handled in Jinja templates directly


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method != "POST":
        return render_template("upload.html")

    # ── Candidate info ──
    name  = request.form.get("name",  "").strip()
    email = request.form.get("email", "").strip()

    if not name or not email:
        flash("Name and email are required.", "error")
        return redirect(request.url)

    if "@" not in email or "." not in email.split("@")[-1]:
        flash("Please enter a valid email address.", "error")
        return redirect(request.url)

    # ── Resume file ──
    resume_file = request.files.get("resume")
    if not resume_file or resume_file.filename == "":
        flash("Please attach a resume PDF.", "error")
        return redirect(request.url)

    if not _allowed(resume_file.filename, ALLOWED_RESUME_EXT):
        flash("Resume must be a PDF file.", "error")
        return redirect(request.url)

    resume_path = _save_upload(resume_file, UPLOAD_FOLDER, ALLOWED_RESUME_EXT)
    if not resume_path:
        flash("Could not save resume. Please try again.", "error")
        return redirect(request.url)

    # ── Proof files ──
    proof_paths: list[str] = []
    for proof_file in request.files.getlist("proofs"):
        if proof_file.filename == "":
            continue
        saved = _save_upload(proof_file, UPLOAD_FOLDER, ALLOWED_PROOF_EXT)
        if saved:
            proof_paths.append(saved)
        else:
            flash(f"Skipped unsupported file: {proof_file.filename}", "warning")

    # ── Certificate / verification links ──
    cert_links = [
        link.strip()
        for link in request.form.getlist("certificate_links")
        if link.strip().startswith("http")
    ]

    # ── Run verification pipeline ──
    try:
        (
            report_path,
            report_filename,
            trust_score,
            verified,
            unknown,
            suspicious,
        ) = process_resume(name, email, resume_path, proof_paths, cert_links)

        log.info(
            "Verification complete for %s — score=%d%%, verified=%d, unknown=%d, suspicious=%d",
            name, trust_score, len(verified), len(unknown), len(suspicious),
        )

        return render_template(
            "result.html",
            name=name,
            email=email,
            trust_score=trust_score,
            verified=verified,
            unknown=unknown,
            suspicious=suspicious,
            report_filename=report_filename,
        )

    except ValueError as exc:
        # User-facing pipeline validation errors (bad PDF, no claims found, etc.)
        flash(str(exc), "error")
        log.warning("Validation error for %s: %s", name, exc)
        return redirect(url_for("upload"))

    except Exception:
        flash("An unexpected error occurred during verification. Please try again.", "error")
        log.error("Unhandled exception:\n%s", traceback.format_exc())
        return redirect(url_for("upload"))

    finally:
        # Always clean up temp files regardless of success or failure
        _cleanup(resume_path, *proof_paths)


@app.route("/download/<path:filename>")
def download(filename: str):
    # Prevent path traversal attacks
    safe = secure_filename(filename)
    if safe != filename:
        flash("Invalid filename.", "error")
        return redirect(url_for("upload"))

    report_path = os.path.join(app.config["REPORTS_FOLDER"], safe)
    if not os.path.exists(report_path):
        flash("Report not found. It may have expired.", "error")
        return redirect(url_for("upload"))

    return send_from_directory(
        app.config["REPORTS_FOLDER"],
        safe,
        as_attachment=True,
    )


# ---------------------------------------------------------------------------
# ERROR HANDLERS
# ---------------------------------------------------------------------------

@app.errorhandler(413)                        # FIX: was @app.errorhandlesr(413)
def request_too_large(e):
    flash(f"Upload too large. Maximum allowed size is {MAX_UPLOAD_MB} MB.", "error")
    return redirect(url_for("upload"))


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(e):
    log.error("500 error: %s", e)
    return render_template("500.html"), 500


# ---------------------------------------------------------------------------
# MINIMAL FALLBACK ERROR PAGES (inline — avoids missing template crashes)
# ---------------------------------------------------------------------------

_404_HTML = """
<!DOCTYPE html>
<html><head><title>404 — ResumeVerify</title>
<style>body{font-family:sans-serif;display:flex;flex-direction:column;
align-items:center;justify-content:center;min-height:100vh;gap:12px;
background:#f5f3ee;color:#0a0a0f;}
h1{font-size:3rem;margin:0;}p{color:#7a7a8a;}
a{color:#2448ff;}</style></head>
<body><h1>404</h1><p>Page not found.</p>
<a href="/">← Back to home</a></body></html>
"""

_500_HTML = """
<!DOCTYPE html>
<html><head><title>500 — ResumeVerify</title>
<style>body{font-family:sans-serif;display:flex;flex-direction:column;
align-items:center;justify-content:center;min-height:100vh;gap:12px;
background:#f5f3ee;color:#0a0a0f;}
h1{font-size:3rem;margin:0;}p{color:#7a7a8a;}
a{color:#2448ff;}</style></head>
<body><h1>500</h1><p>Something went wrong on our end.</p>
<a href="/">← Back to home</a></body></html>
"""


# Override error handlers to use inline HTML if templates are missing
@app.errorhandler(404)
def _404(e):
    try:
        return render_template("404.html"), 404
    except Exception:
        from flask import Response
        return Response(_404_HTML, status=404, mimetype="text/html")


@app.errorhandler(500)
def _500(e):
    log.error("500 error: %s", e)
    try:
        return render_template("500.html"), 500
    except Exception:
        from flask import Response
        return Response(_500_HTML, status=500, mimetype="text/html")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    port  = int(os.environ.get("PORT", 5000))
    log.info("Starting ResumeVerify on port %d (debug=%s)", port, debug)
    app.run(debug=debug, port=port, host="0.0.0.0")