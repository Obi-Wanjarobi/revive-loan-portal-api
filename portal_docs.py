"""
Portal uploads -> OneDrive (+ email alert to Jay).

Every document a borrower uploads in the portal is copied into that borrower's OneDrive folder:
    Pipeline/<Mortgage Processing | Mortgage Funded | Mortgage Withdrawn>/<Borrower folder>/Portal Uploads/
(the existing borrower folder is found by name; if none exists, one is created under Mortgage Processing).
Jay gets a short email per upload (who, what, where) - the document itself is NOT emailed.

Runs right after each upload and as a sweep every PORTAL_DOCS_MINUTES (default 10) to catch anything missed.
Needs Microsoft Graph Files.ReadWrite.All (Application) on the "Revive Portal Sync" app.
Turn off with Railway variable PORTAL_DOCS_EXPORT=0.
"""
import datetime as dt, html, json, os, re, threading, time, traceback, urllib.error, urllib.parse, urllib.request

import models
from database import SessionLocal
import approval_sync

NOTIFY_TO = os.environ.get("PORTAL_DOCS_NOTIFY", "jturner@myrevivecapital.com")
SUBFOLDER = "Portal Uploads"
STATUS = {"last_run": None, "exported": [], "errors": []}
_lock = threading.Lock()
_folder_cache = {"at": 0, "folders": []}       # (status_folder, name) of borrower folders


def enabled():
    return approval_sync.enabled() and os.environ.get("PORTAL_DOCS_EXPORT", "1") != "0"


def _pipeline():
    return os.environ.get("PIPELINE_PATH", "Pipeline").strip("/")


def _safe(name):
    name = re.sub(r'[\\/:*?"<>|#%]+', "-", name or "file").strip(" .")
    return name[:180] or "file"


def _norm(s):
    return " ".join(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _borrower_folders():
    if time.time() - _folder_cache["at"] < 3600 and _folder_cache["folders"]:
        return _folder_cache["folders"]
    out = []
    for folder in approval_sync.STATUS_FOLDERS:
        url = (f"/users/{approval_sync._user()}/drive/root:/{urllib.parse.quote(_pipeline() + '/' + folder)}:/children"
               f"?$top=200&$select=name,folder")
        while url:
            page = approval_sync._graph(url)
            out += [(folder, it["name"]) for it in page.get("value", []) if "folder" in it]
            url = page.get("@odata.nextLink")
    _folder_cache.update(at=time.time(), folders=out)
    return out


def borrower_folder(borrower_name):
    """Best existing folder for this borrower: exact name match first, then first+last name contained in folder name.
    Prefers Mortgage Processing, then Funded, then Withdrawn. Falls back to a new folder under Mortgage Processing."""
    want = _norm(borrower_name)
    words = want.split()
    first, last = (words[0], words[-1]) if words else ("", "")
    order = list(approval_sync.STATUS_FOLDERS)
    folders = sorted(_borrower_folders(), key=lambda f: order.index(f[0]))
    for status_folder, name in folders:
        if _norm(name) == want:
            return f"{status_folder}/{name}"
    for status_folder, name in folders:
        n = _norm(name)
        if first and last and re.search(rf"\b{re.escape(first)}\b", n) and re.search(rf"\b{re.escape(last)}\b", n):
            return f"{status_folder}/{name}"
    return f"Mortgage Processing/{_safe(borrower_name or 'Unknown Borrower')}"


def _put(path, data, content_type):
    """Upload bytes to OneDrive at path (folders auto-created). Small files direct; large via upload session."""
    tok = approval_sync._graph_token()
    base = f"https://graph.microsoft.com/v1.0/users/{approval_sync._user()}/drive/root:/{urllib.parse.quote(path)}"
    if len(data) <= 4 * 1024 * 1024:
        req = urllib.request.Request(base + ":/content?@microsoft.graph.conflictBehavior=rename", data=data, method="PUT",
                                     headers={"Authorization": "Bearer " + tok, "Content-Type": content_type or "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    req = urllib.request.Request(base + ":/createUploadSession", method="POST",
                                 data=json.dumps({"item": {"@microsoft.graph.conflictBehavior": "rename"}}).encode(),
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        upload_url = json.loads(r.read())["uploadUrl"]
    chunk, size, result = 5 * 320 * 1024, len(data), None
    for start in range(0, size, chunk):
        part = data[start:start + chunk]
        req = urllib.request.Request(upload_url, data=part, method="PUT",
                                     headers={"Content-Length": str(len(part)),
                                              "Content-Range": f"bytes {start}-{start + len(part) - 1}/{size}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            if body:
                result = json.loads(body)
    return result or {}


def _notify(loan, doc, item, folder_path):
    link = item.get("webUrl") or ""
    e = html.escape
    body = (f"<p><b>{e(loan.borrower_name or 'A borrower')}</b> ({e(loan.loan_number)}) uploaded a document in the client portal.</p>"
            f"<p>File: <b>{e(doc.filename or '')}</b><br>Type: {e(doc.doc_type or 'not specified')}<br>"
            f"Uploaded: {e(str(doc.created_at or '')[:16])} UTC</p>"
            f"<p>Saved to OneDrive: {e(folder_path)}/{SUBFOLDER}<br>"
            + (f'<a href="{e(link)}">Open the file in OneDrive</a>' if link else "") + "</p>")
    try:
        import campaign
        campaign.send_one(NOTIFY_TO, f"Portal upload: {loan.borrower_name} - {doc.filename}", body)
    except Exception as ex:                                   # alert failure never blocks the export
        STATUS["errors"].append({"doc": doc.id, "error": f"notify: {str(ex)[:200]}"})


def export_doc(db, doc, notify=True):
    if db.query(models.DocumentExport).filter(models.DocumentExport.document_id == doc.id).first():
        return None
    loan = db.query(models.Loan).filter(models.Loan.id == doc.loan_id).first()
    if not loan:
        return None
    folder_path = borrower_folder(loan.borrower_name)
    stamp = (doc.created_at or dt.datetime.utcnow()).strftime("%Y-%m-%d")
    path = f"{_pipeline()}/{folder_path}/{SUBFOLDER}/{stamp} - {_safe(doc.filename)}"
    item = _put(path, doc.data or b"", doc.content_type)
    db.add(models.DocumentExport(document_id=doc.id, onedrive_path=path, web_url=item.get("webUrl")))
    db.commit()
    recent = doc.created_at and (dt.datetime.now(dt.timezone.utc) - (doc.created_at if doc.created_at.tzinfo
                                  else doc.created_at.replace(tzinfo=dt.timezone.utc))).total_seconds() < 72 * 3600
    if notify and recent and doc.uploaded_by_role == "borrower":
        _notify(loan, doc, item, folder_path)
    STATUS["exported"] = (STATUS["exported"] + [{"loan": loan.loan_number, "file": doc.filename, "path": path}])[-50:]
    return path


def sweep(notify=True):
    """Export every portal document not yet copied to OneDrive."""
    if not enabled() or not _lock.acquire(blocking=False):
        return {"status": "skipped"}
    db = SessionLocal()
    done = []
    try:
        exported = {x.document_id for x in db.query(models.DocumentExport).all()}
        for doc in db.query(models.Document).order_by(models.Document.created_at).all():
            if doc.id in exported:
                continue
            try:
                p = export_doc(db, doc, notify=notify)
                if p:
                    done.append(p)
            except Exception as ex:
                db.rollback()
                msg = ex.read()[:200].decode("utf8", "ignore") if isinstance(ex, urllib.error.HTTPError) else str(ex)[:200]
                STATUS["errors"] = (STATUS["errors"] + [{"doc": doc.id, "file": doc.filename, "error": msg}])[-50:]
        return {"exported": done}
    finally:
        db.close()
        STATUS["last_run"] = dt.datetime.utcnow().isoformat() + "Z"
        _lock.release()


def export_now(doc_id):
    """Called right after an upload (in a background thread)."""
    def go():
        time.sleep(2)
        sweep()
    threading.Thread(target=go, daemon=True).start()


def _loop():
    time.sleep(90)
    while True:
        try:
            sweep()
        except Exception:
            traceback.print_exc()
        time.sleep(max(2, int(os.environ.get("PORTAL_DOCS_MINUTES", "10"))) * 60)


def start_scheduler():
    threading.Thread(target=_loop, name="portal-docs", daemon=True).start()
