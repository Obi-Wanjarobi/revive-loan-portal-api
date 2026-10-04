"""
Automatic approval-conditions sync (runs inside the portal API on Railway - no PC, no clicks).

Every APPROVAL_SYNC_MINUTES (default 30) it:
  1. Finds lender approvals (PDF/Word named *approval*/*decision*/*condition*) in Jay's OneDrive
     under Pipeline/Mortgage Processing, Mortgage Funded, Mortgage Withdrawn (Microsoft Graph, read-only).
  2. Reads the conditions (theLender, Logan Finance, UWM layouts).
  3. Matches each approval to a portal loan by house # + street + ZIP AND the borrower's last name on the approval.
  4. Pushes ONLY approvals from Mortgage Processing, for loans the portal shows as active. Funded/Withdrawn are never pushed.
  5. Anything that doesn't line up is recorded as HELP (GET /admin/approval-sync/status) and not pushed.

Turns on only when these Railway variables are set (Boss enters the secret):
  MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET   (Azure app with Microsoft Graph Files.Read.All - application)
Optional: ONEDRIVE_USER (default jturner@myrevivecapital.com), PIPELINE_PATH (default Pipeline),
          APPROVAL_SYNC_MINUTES (default 30), APPROVAL_SYNC_ENABLED=0 to pause.
"""
import datetime as dt, io, json, os, re, threading, time, traceback, urllib.error, urllib.parse, urllib.request, zipfile

import models
from database import SessionLocal

STATUS_FOLDERS = {"Mortgage Processing": "active", "Mortgage Funded": "funded", "Mortgage Withdrawn": "withdrawn"}
ACTIVE_PORTAL_STAGES = {"application", "processing", "underwriting", "conditions", "clear to close"}
NAME_HINT = re.compile(r"approval|decision|condition", re.I)
NAME_EXCLUDE = re.compile(r"pre[\s\-_]?approval|pre[\s\-_]?qual", re.I)

# Items a borrower can't act on (broker / lender / title back-office). Case-insensitive, matched on title+detail.
HIDE = [
    r"\bbroker pending\b", r"\bbroker to provide\b", r"ordered by (the)?lender", r"\bapproval terms\b",
    r"doc order form", r"highland law|go docs", r"due diligence review", r"^cpl\b", r"title to provide a cpl",
    r"invoices? for the following services", r"closing request form", r"loan to be locked",
    r"title to be vested in the name of", r"appraisal was delivered to the borrower",
    r"^tc:", r"third party processing invoice", r"copy of invoice for", r"document expiration",
    r"title company to include lender loan number",
    r"_{3,}",                      # unfilled lender template blanks: "statement for: ____"
]
DIRECTIONS = {"e": "e", "east": "e", "w": "w", "west": "w", "n": "n", "north": "n", "s": "s", "south": "s"}


# ---------------- text helpers ----------------
def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def strip_staff_notes(s):
    s = re.sub(r"\*\*[^*]{0,300}?\([a-z]{2,3}\)\s*\*\*", " ", s)             # **08/18 sent to uw for review (ct)**
    s = re.sub(r"Mortgagee Clause:.*?\(\d{3}\)\s*\d{3}-\d{4}", " ", s)
    s = re.sub(r"p 800-981-8898.*?UWM\.COM", " ", s, flags=re.I)
    s = re.sub(r"Page\s+\d+\s+of\s+\d+", " ", s)
    return clean(s)


def mask_numbers(s):
    return re.sub(r"\b\d{2,}(\d{4})\b", lambda m: "••••" + m.group(1) if len(m.group(0)) >= 6 else m.group(0), s)


def short_title(text, limit=90):
    t = re.sub(r"^\*\*\s*\d{1,2}/\d{1,2}\s+Update:.*?\*\*\s*", "", text).strip() or text   # skip "**07/22 Update: ...**" so titles stay stable
    t = t.strip("* ").strip() or t
    first = re.split(r"(?<=[.;:])\s", t, 1)[0].rstrip(".;:")
    if len(first) <= limit:
        return first
    cut = t[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def date_key(s):
    try:
        return dt.datetime.strptime(s, "%m/%d/%Y")
    except Exception:
        return dt.datetime.min


def first_date(t, *labels):
    for lab in labels:
        m = re.search(lab + r"\s*:?\s*\(?\s*(\d{2}/\d{2}/\d{4})", t, re.I)
        if m:
            return m.group(1)
    return None


def cond(title, detail, label):
    title, detail = clean(title), clean(detail)
    full = f"{title} {detail}"
    if not title or any(re.search(p, full, re.I) or re.search(p, title, re.I) for p in HIDE):
        return None
    body = f"{label}. {detail}" if detail else label
    return {"title": mask_numbers(title)[:120], "detail": mask_numbers(body.rstrip(" .") + ".")[:1500], "done": False}


# ---------------- lender parsers ----------------
def parse_thelender(t):
    labels = {"approval": "Needed for approval", "docs": "Needed before loan documents",
              "closing": "Needed before closing", "fund": "Needed before funding"}
    hdr = re.compile(r"Prior\s*to\s*(Final\s+)?(Approval|Docs|Closing|Fund(?:ing)?)", re.I)
    m1 = re.search(r"(?<![\d.])1\.\s+(?=[A-Z])", t)
    if not m1:
        return []
    region = t[m1.start():]
    pre_region = t[max(0, m1.start() - 200):m1.start()]
    toks = [m for m in re.finditer(r"(?:^|\s)(\d{1,2})\.\s+(?=[A-Z*])", region)]
    runs, expect, cur = [], 1, None
    for m in toks:
        n = int(m.group(1))
        if n == 1:
            cur = [m]; runs.append(cur); expect = 2
        elif cur is not None and n == expect:
            cur.append(m); expect += 1
    # label each run from the heading just before it; fall back to position if headings aren't inline (Word export)
    run_labels = []
    for i, run in enumerate(runs):
        window = pre_region if i == 0 else region[runs[i - 1][-1].start():run[0].start()]
        hs = hdr.findall(window)
        run_labels.append(labels[{"appr": "approval", "docs": "docs", "clos": "closing", "fund": "fund"}[hs[-1][1].lower()[:4]]] if hs else None)
    if not any(run_labels):
        fallback = {1: ["docs"], 2: ["docs", "fund"], 3: ["approval", "docs", "fund"]}.get(len(runs), ["docs"] * len(runs))
        run_labels = [labels[k] for k in fallback]
    run_labels = [l or "Outstanding condition" for l in run_labels]

    out, starts = [], [m for run in runs for m in run]
    owner = {id(m): run_labels[i] for i, run in enumerate(runs) for m in run}
    for j, m in enumerate(starts):
        end = starts[j + 1].start() if j + 1 < len(starts) else len(region)
        raw = hdr.sub(" ", region[m.end():end])
        raw = re.sub(r"\b(Cash|Co|Pre|Re|Non|Self|Near|\d{1,2})\s+-\s+(?=\w)", r"\1-", strip_staff_notes(raw))
        parts = [p.strip() for p in re.split(r"\s+-\s+", raw) if p.strip()]
        # drop processor notes: "8/13 DB", "EMAILED SSA FORM TO BROKER" (all-caps shouting = internal note)
        parts = [p for p in parts if not re.match(r"^\d{1,2}/\d{1,2}\s+[A-Z]{2,3}\b", p)
                 and not (len(p) > 8 and not re.search(r"[a-z]", p) and len(p.split()) >= 3)]
        if len(parts) >= 2 and parts[0].lower() in {"noni", "credit", "title", "property", "misc", "closing",
                                                     "income", "assets", "appraisal", "insurance"}:
            parts = parts[1:]
        if not parts:
            continue
        c = cond(parts[0], " - ".join(parts[1:]), owner[id(m)])
        if c:
            out.append(c)
    return out


def parse_logan(t):
    labels = {"PTD": "Needed for final approval", "AC": "Needed at closing", "PTF": "Needed before funding"}
    s = t.find("CONDITIONS", t.find("MORTGAGEE CLAUSES") if "MORTGAGEE CLAUSES" in t else 0)
    e = t.find("DISCLOSURE DATES", s)
    body = t[s + len("CONDITIONS"): e if e > 0 else len(t)]
    sec = re.compile(r"(PRIOR TO FINAL APPROVAL|AT CLOSING|PRIOR TO FUNDING)\s*\((PTD|AC|PTF)\)")
    cats = re.compile(r"\b(ASSETS|CREDIT|INCOME|PROPERTY|LEGAL|MISCELLANEOUS|TITLE|APPRAISAL|INSURANCE)\b(?=\s+(?:[A-Z]{1,2}\d{1,4}\.|ASSETS|CREDIT|INCOME|PROPERTY|LEGAL|MISC|TITLE|APPRAISAL|INSURANCE|$))")
    marks = sorted([(m.start(), m.end(), "sec", m.group(2)) for m in sec.finditer(body)] +
                   [(m.start(), m.end(), "item", m.group(1)) for m in re.finditer(r"(?<![A-Za-z0-9#])([A-Z]{1,2}\d{1,4})\.\s", body)])
    out, label, seen = [], labels["PTD"], set()
    for i, (a, b, kind, val) in enumerate(marks):
        if kind == "sec":
            label = labels[val]; continue
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        text = strip_staff_notes(cats.sub(" ", body[b:end]))
        if val in seen or not text:
            continue
        seen.add(val)
        c = cond(short_title(text), text, label)
        if c:
            out.append(c)
    return out


def parse_uwm(t):
    labels = {"master": "Outstanding condition", "ptd": "Needed for final approval",
              "ptf": "Needed before closing", "uw": None}
    s = t.find("CONDITIONS", t.find("LOAN INFORMATION") if "LOAN INFORMATION" in t else 0)
    e = t.find("EXPIRATION DATES", s)
    body = t[s + len("CONDITIONS"): e if e > 0 else len(t)]
    sec = re.compile(r"\b(Master|UW Prior To Final Approval \(PTD\)|Underwriter To Obtain And Clear|Closing \(PTF\))")
    item = re.compile(r"(?<![\d/,.$#\w])(\d{4})\s+((?:Document Expiration|Appraisal|Borrower|Credit|Income|Insurance|Property|Invoice|TC|Assets|Title|Closing|Legal|Misc\w*|Condo|Flood|Compliance)(?:\s*\([^)]{1,40}\))?)\s+(?=[A-Z\"'(*])")
    marks = sorted([(m.start(), m.end(), "sec", m.group(1)) for m in sec.finditer(body)] +
                   [(m.start(), m.end(), "item", (m.group(1), m.group(2))) for m in item.finditer(body)])
    out, label, seen = [], labels["master"], set()
    for i, (a, b, kind, val) in enumerate(marks):
        if kind == "sec":
            label = labels["master" if val == "Master" else "ptd" if "PTD" in val else "ptf" if "PTF" in val else "uw"]
            continue
        code, category = val
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        text = strip_staff_notes(body[b:end])
        if label is None or code in seen or not text or re.match(r"(Invoice|TC|Document Expiration)\b", category) or text.startswith("TC:"):
            continue
        seen.add(code)
        c = cond(short_title(text), text, label)
        if c:
            out.append(c)
    return out


def detect_and_parse(text):
    """-> dict(lender, approval_date, printed, address, conditions) or None if not an approval we recognize."""
    t = clean(text)
    if re.search(r"UNDERWRITING CONDITIONAL APPROVAL", t):
        m = re.search(r"Subject Property:\s*(.+?)\s+Loan Program", t)
        return dict(lender="Logan", approval_date=first_date(t, r"approval date"), printed=first_date(t, r"Date Printed"),
                    address=m.group(1) if m else None, conditions=parse_logan(t))
    if re.search(r"LOAN APPROVAL CONDITIONS", t) and "UWM" in t.upper():
        m = re.search(r"LOAN INFORMATION.*?\bProperty\s+(\d+.+?)\s+Transaction Type", t)
        return dict(lender="UWM", approval_date=first_date(t, r"Approved With Conditions"), printed=first_date(t, r"Date Printed"),
                    address=m.group(1) if m else None, conditions=parse_uwm(t))
    if re.search(r"Loan Approval\s+as of", t, re.I):
        m = re.search(r"Property\s*Address:\s*(\d+.{3,120}?)(?:\s+Occupancy|\s+Co\s*-\s*Borrower|\s+DSCR|\s+Loan\s)", t)
        return dict(lender="theLender", approval_date=first_date(t, r"Approval Date", r"Loan Approval\s+as of"),
                    printed=first_date(t, r"Printed on", r"Loan Approval\s+as of"),
                    address=m.group(1) if m else None, conditions=parse_thelender(t))
    return None


def addr_key(a):
    words = re.findall(r"[A-Za-z0-9]+", (a or "").lower())
    if not words or not words[0].isdigit():
        return None
    rest = [w for w in words[1:] if w not in DIRECTIONS]
    return (words[0], rest[0]) if rest else None


def zip5(a):
    rest = re.sub(r"^\s*\d+", "", a or "")          # drop the house number so 32336 isn't read as a ZIP
    m = re.search(r"\b(\d{5})(?:\s*-\s*\d{4})?\b(?!.*\b\d{5}\b)", rest)
    return m.group(1) if m else None


def last_name(full):
    parts = [p for p in re.findall(r"[A-Za-z'\-]+", full or "") if p.lower().strip(".") not in {"jr", "sr", "ii", "iii", "iv"}]
    return parts[-1] if parts else None


def folder_address_conflict(rel_parts, doc_key):
    """True if the file sits under a property folder (e.g. '517 M St') for a DIFFERENT address than the approval."""
    for part in rel_parts[:-1]:
        k = addr_key(part)
        if k and k != doc_key:
            return part
    return None



def _docx_text(xml):
    """Word text with automatic list numbering written out ("1. ", "2. "...), restarting at each "Prior to ..." heading."""
    out, counters = [], {}
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()
        if re.match(r"Prior\s*to\s*(Final\s+)?(Approval|Docs|Closing|Fund)", text, re.I) and len(text) < 40:
            counters.clear()
        lvl = re.search(r'<w:ilvl w:val="(\d+)"', p)
        nid = re.search(r'<w:numId w:val="(\d+)"', p)
        styled = re.search(r'<w:pStyle w:val="List ?Number', p)
        if text and (nid and nid.group(1) != "0" or styled) and (not lvl or lvl.group(1) == "0") \
                and not re.match(r"\d{1,2}\.\s", text):
            key = nid.group(1) if nid else "style"
            counters[key] = counters.get(key, 0) + 1
            text = f"{counters[key]}. {text}"
        out.append(text)
    return "\n".join(out)


def read_bytes(name, data):
    if name.lower().endswith(".docx"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf8", "ignore")
        return _docx_text(xml)
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)


# ---------------- Microsoft Graph (read-only) ----------------
def _cfg():
    return {k: os.environ.get(k, "") for k in ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET")}


def enabled():
    c = _cfg()
    return all(c.values()) and os.environ.get("APPROVAL_SYNC_ENABLED", "1") != "0"


_token = {"value": None, "exp": 0}


def _graph_token():
    if _token["value"] and time.time() < _token["exp"] - 120:
        return _token["value"]
    c = _cfg()
    body = urllib.parse.urlencode({"client_id": c["MS_CLIENT_ID"], "client_secret": c["MS_CLIENT_SECRET"],
                                   "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(f"https://login.microsoftonline.com/{c['MS_TENANT_ID']}/oauth2/v2.0/token", data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Microsoft login {e.code}: {e.read()[:300].decode('utf8', 'ignore')}")
    _token.update(value=j["access_token"], exp=time.time() + int(j.get("expires_in", 3600)))
    return _token["value"]


def _graph(url, raw=False, _retry=True):
    if url.startswith("/"):
        url = "https://graph.microsoft.com/v1.0" + url
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + _graph_token()})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 401 and _retry:            # stale token (e.g. issued before admin consent) -> get a fresh one once
            _token.update(value=None, exp=0)
            return _graph(url, raw, _retry=False)
        detail = e.read()[:400].decode("utf8", "ignore")
        raise RuntimeError(f"Graph {e.code} on {url.split('?')[0][-90:]}: {detail}")
    return data if raw else json.loads(data)


def _user():
    return urllib.parse.quote(os.environ.get("ONEDRIVE_USER", "jturner@myrevivecapital.com"))


_closed_cache = {"at": 0, "files": []}      # Funded/Withdrawn change rarely -> rescan every CLOSED_RESCAN_HOURS


ALL_ACTIVE_FILES = []                       # every file under Mortgage Processing (for "already on file" checks)


def _walk(item_path, status, rel_parts, out, depth=0, all_files=None):
    """List a OneDrive folder and its subfolders (max 5 deep), keeping approval-named PDF/Word files."""
    if depth > 5:
        return
    url = (f"/users/{_user()}/drive/root:/{urllib.parse.quote(item_path)}:/children"
           f"?$top=200&$select=id,name,eTag,file,folder,lastModifiedDateTime")
    while url:
        page = _graph(url)
        for it in page.get("value", []):
            name = it["name"]
            if "folder" in it:
                if "do not use" in name.lower():
                    continue
                _walk(f"{item_path}/{name}", status, rel_parts + [name], out, depth + 1, all_files)
                continue
            if all_files is not None and "file" in it:
                all_files.append({"rel_parts": rel_parts + [name], "modified": it.get("lastModifiedDateTime")})
            if "file" not in it or not name.lower().endswith((".pdf", ".docx")) or name.startswith("~$"):
                continue
            if not NAME_HINT.search(name) or NAME_EXCLUDE.search(name):
                continue
            out.append({"id": it["id"], "name": name, "etag": it.get("eTag"), "folder_status": status,
                        "rel_parts": rel_parts + [name], "rel": "/".join([item_path.split("/", 1)[-1]] + [name])})
        url = page.get("@odata.nextLink")


def graph_find_files():
    """-> list of {id, name, etag, folder_status, rel_parts} for approval-looking files under the 3 Pipeline folders.
    Walks the folders directly (app-only OneDrive search is blocked on this tenant)."""
    pipeline = os.environ.get("PIPELINE_PATH", "Pipeline").strip("/")
    out, every = [], []
    for folder, status in STATUS_FOLDERS.items():
        if status == "active":
            _walk(f"{pipeline}/{folder}", status, [], out, all_files=every)
    ALL_ACTIVE_FILES[:] = every
    hours = float(os.environ.get("CLOSED_RESCAN_HOURS", "6"))
    if time.time() - _closed_cache["at"] > hours * 3600:
        closed = []
        for folder, status in STATUS_FOLDERS.items():
            if status != "active":
                _walk(f"{pipeline}/{folder}", status, [], closed)
        _closed_cache.update(at=time.time(), files=closed)
    return out + _closed_cache["files"]


def graph_download(item_id):
    return _graph(f"/users/{_user()}/drive/items/{item_id}/content", raw=True)


# ---------------- "already on file" (don't ask the borrower twice) ----------------
# (what the condition asks for, file/folder names that satisfy it, file names that must NOT count)
ON_FILE_RULES = [
    (r"\bflood", r"flood", None),
    (r"hazard|homeowner|\bhoi\b|dec(laration)? page|insurance binder", r"\bhoi\b|insur|\bdec(laration)?s?\b|binder|homeowner", r"flood|title"),
    (r"\blease", r"lease", None),
    (r"payoff", r"payoff", None),
    (r"mortgage statement|housing history|mortgage rating|payment history",
     r"mortgage st|mtg st|newrez|shellpoint|rocket mort|mr\.? ?cooper|nationstar|pennymac|loancare|carrington|lakeview|dovenmuehle|freedom mort", None),
    (r"bank statement|asset|reserves|funds to close|seasoning", r"bank|estmt|e-?statement|checking|savings|brokerage|\bchase\b|wells|bofa|schwab|fidelity", r"mortgage|newrez|shellpoint"),
    (r"\bach\b", r"\bach\b", None),
    (r"identification|\bphoto id|driver'?s? licen|\bid\b", r"\bid\b|licen|passport|\bdl\b", None),
    (r"apprais|\b1004\b|\b1007\b|\b1025\b|rent schedule", r"apprais|\b1004\b|\b1007\b|\b1025\b", None),
    (r"letter of explanation|\blox\b|\bloe\b|purpose of the cash|cash-?out letter", r"\blox\b|\bloe\b|explanation", None),
    (r"\bssn\b|social security|\bssa", r"\bssa|\bssn\b|social security", None),
    (r"prelim|title commitment|title report", r"prelim|title commit|title report", None),
    (r"operating agreement|articles of|\bein\b|good standing|entity doc|llc doc", r"operating|articles|\bein\b|good standing|formation|\bllc doc", None),
    (r"tax return|\b1040|transcript", r"\b1040|tax return|transcript", None),
    (r"pay ?stub|\bw-?2\b|\bvoe\b|employment history", r"pay ?stub|\bw-?2\b|\bvoe\b", None),
]
# Conditions that are lender decisions/payments, not a document the borrower sends - never marked "on file".
NOT_A_DOCUMENT = (r"\bdue\b|\bsecond\b|\bvalue\b|cash to close|ownership|vesting|must be from|settlement statement"
                  r"|\bterms\b|restructur|\bfee\b|paid at closing|at closing")
ON_FILE_NOTE = ("📄 We already have this on file (received {when}) - it's with the lender for review. "
                "No need to send it again unless your processor asks for an updated copy. ")


def _scoped_files(approval_rel_parts, doc_key, files):
    """Files belonging to this loan: its property folder (+ subfolders) and the borrower's shared, non-property folders."""
    if not approval_rel_parts:
        return []
    borrower = approval_rel_parts[0]
    out = []
    for f in files:
        parts = f["rel_parts"]
        if not parts or parts[0] != borrower:
            continue
        inner = parts[1:-1]                                           # folders between borrower and file
        if any("do not use" in p.lower() for p in inner):
            continue
        prop = [p for p in inner if addr_key(p)]
        if prop and addr_key(prop[0]) != doc_key:
            continue                                                  # another property's folder
        if NAME_HINT.search(parts[-1]) and re.search(r"approval|decision", parts[-1], re.I):
            continue                                                  # the approvals themselves
        out.append(f)
    return out


def annotate_on_file(conditions, scoped, uploads):
    """Mark conditions whose document is already in the loan's folder or was uploaded in the portal (stays OPEN)."""
    pool = [("/".join(f["rel_parts"]).lower(), (f.get("modified") or "")[:10]) for f in scoped]
    pool += [(f"{u.filename} {u.doc_type or ''}".lower(), str(u.created_at or "")[:10]) for u in uploads]
    out = []
    for c in conditions:
        c = dict(c)
        text = c["title"].lower()                     # title only: descriptions mention many documents in passing
        hits = []
        if re.search(NOT_A_DOCUMENT, text, re.I):
            out.append(c); continue
        for cond_pat, file_pat, not_pat in ON_FILE_RULES:
            if re.search(cond_pat, text, re.I):
                hits += [d for name, d in pool if re.search(file_pat, name, re.I) and not (not_pat and re.search(not_pat, name, re.I))]
                break
        if hits:
            latest = max(hits) if any(hits) else ""
            try:
                when = dt.datetime.strptime(latest, "%Y-%m-%d").strftime("%b %d").replace(" 0", " ")
            except Exception:
                when = "earlier"
            c["detail"] = ON_FILE_NOTE.format(when=when) + (c.get("detail") or "")
            c["on_file"] = True
        out.append(c)
    return out


# ---------------- merge into the portal DB ----------------
def _cond_key(title):
    return " ".join((title or "").lower().split())


def merge_approval(db, loan, conditions, approval_date=None):
    """New on approval -> open; gone from approval -> cleared (checkmark); back again -> reopened."""
    existing = {_cond_key(c.title): c for c in db.query(models.Condition).filter(models.Condition.loan_id == loan.id).all()}
    incoming = {}
    for c in conditions:
        incoming.setdefault(_cond_key(c["title"]), c)
    added = cleared = reopened = 0
    now = dt.datetime.utcnow()
    for key, c in incoming.items():
        row = existing.get(key)
        if row is None:
            db.add(models.Condition(loan_id=loan.id, title=c["title"], detail=c.get("detail"), done=False)); added += 1
        else:
            row.detail = c.get("detail")
            if row.done:
                row.done, row.completed_at = False, None; reopened += 1
    removed = 0
    for key, row in existing.items():
        if key in incoming:
            continue
        full = f"{row.title} {row.detail or ''}"
        if any(re.search(p, full, re.I) for p in HIDE):     # template blanks / back-office items: remove, don't show as cleared
            db.delete(row); removed += 1
        elif not row.done:
            row.done, row.completed_at = True, now; cleared += 1
    if added or cleared or reopened:
        when = f" ({approval_date})" if approval_date else ""
        parts = [f"{n} {w}" for n, w in ((added, "new"), (cleared, "cleared"), (reopened, "reopened")) if n]
        db.add(models.ActivityEvent(loan_id=loan.id, text=f"Lender approval updated{when}: " + ", ".join(parts) + " condition(s)."))
    db.commit()
    return {"open": len(incoming), "added": added, "cleared": cleared, "reopened": reopened, "removed": removed}


# ---------------- one run ----------------
_cache = {}          # item id -> (etag, parsed doc)   avoids re-downloading unchanged files
STATUS = {"enabled": False, "running": False, "last_started": None, "last_finished": None,
          "last_error": None, "pushed": [], "help": [], "skipped": {}, "files_seen": 0}
_lock = threading.Lock()


def run_once(dry_run=False, files=None, download=None, all_files=None):
    """files/download are injectable for tests; production uses Microsoft Graph."""
    if not _lock.acquire(blocking=False):
        return {"status": "already running"}
    STATUS.update(running=True, last_started=dt.datetime.utcnow().isoformat() + "Z", last_error=None)
    db = SessionLocal()
    try:
        files = files if files is not None else graph_find_files()
        all_files = all_files if all_files is not None else list(ALL_ACTIVE_FILES)
        download = download or graph_download
        loans = db.query(models.Loan).all()
        by_addr = {}
        for l in loans:
            if re.match(r"LN-", l.loan_number or "", re.I):     # LN-xxxx = test data (to be purged) - never match
                continue
            k = addr_key(l.property_address)
            if k:
                by_addr.setdefault(k, []).append(l)

        def match(doc):
            k = addr_key(doc["address"])
            if not k:
                return None, "could not read the property address on the approval"
            cands = by_addr.get(k, [])
            z = zip5(doc["address"])
            if z:
                cands = [l for l in cands if zip5(l.property_address) in (z, None)]
            if not cands:
                return None, f"no portal loan for property '{doc['address']}'"
            named = [l for l in cands if last_name(l.borrower_name) and
                     re.search(r"\b" + re.escape(last_name(l.borrower_name)) + r"\b", doc["text"], re.I)]
            if not named:
                return None, (f"address matches {', '.join(l.loan_number + ' ' + (l.borrower_name or '') for l in cands)} "
                              f"but that borrower's name is not on the approval")
            if len(named) > 1:
                active = [l for l in named if (l.stage or "").lower() in ACTIVE_PORTAL_STAGES]
                if len(active) == 1 and doc.get("_folder_status") == "active":
                    return active[0], None
            if len(named) > 1:
                return None, f"more than one portal loan for '{doc['address']}': {', '.join(l.loan_number for l in named)}"
            return named[0], None

        best, help_items, closed = {}, [], {}
        skipped = {"funded_or_withdrawn": 0, "portal_closed": 0, "not_an_approval": 0}
        for f in files:
            cached = _cache.get(f["id"])
            if cached and cached[0] == f["etag"]:
                doc = cached[1]
            else:
                try:
                    text = read_bytes(f["name"], download(f["id"]))
                    doc = detect_and_parse(text)
                    if doc:
                        doc["text"] = clean(text)
                except Exception as e:
                    help_items.append({"file": f["rel"], "why": f"could not read file: {e}"}); continue
                _cache[f["id"]] = (f["etag"], doc)
            if doc is None:
                skipped["not_an_approval"] += 1; continue
            doc["_folder_status"] = f["folder_status"]
            loan, why = match(doc)
            if f["folder_status"] != "active":
                if loan:
                    closed[loan.loan_number] = f["folder_status"]
                    if (loan.stage or "").lower() in ACTIVE_PORTAL_STAGES:
                        help_items.append({"file": f["rel"], "why": f"OneDrive says {f['folder_status'].upper()} but portal shows "
                                           f"{loan.loan_number} {loan.borrower_name} as '{loan.stage}' - update the stage in the CRM. Not pushed."})
                skipped["funded_or_withdrawn"] += 1; continue
            if why:
                help_items.append({"file": f["rel"], "why": why}); continue
            conflict = folder_address_conflict(f["rel_parts"], addr_key(doc["address"]))
            if conflict:
                help_items.append({"file": f["rel"], "why": f"filed under folder '{conflict}' but the approval is for '{doc['address']}' - refile it"}); continue
            if not doc["conditions"]:
                help_items.append({"file": f["rel"], "why": f"{doc['lender']} approval found but no conditions could be read"}); continue
            if (loan.stage or "").lower() not in ACTIVE_PORTAL_STAGES:
                skipped["portal_closed"] += 1; continue
            rank = (date_key(doc["printed"] or ""), date_key(doc["approval_date"] or ""))
            if loan.loan_number not in best or rank > best[loan.loan_number][0]:
                best[loan.loan_number] = (rank, loan, doc, f["rel"], f["rel_parts"])
        for ln, st in closed.items():
            if ln in best:
                help_items.append({"file": best[ln][3], "why": f"{ln} also has an approval in Mortgage {st.title()} - treated as closed. Not pushed."})
                del best[ln]

        pushed = []
        for ln, (rank, loan, doc, rel, rel_parts) in sorted(best.items()):
            scoped = _scoped_files(rel_parts, addr_key(doc["address"]), all_files)
            uploads = db.query(models.Document).filter(models.Document.loan_id == loan.id).all()
            conds = annotate_on_file(doc["conditions"], scoped, uploads)
            res = {"loan": ln, "borrower": loan.borrower_name, "lender": doc["lender"], "file": rel,
                   "approval_date": doc["approval_date"] or doc["printed"], "conditions": len(conds),
                   "already_on_file": [c["title"] for c in conds if c.get("on_file")]}
            if dry_run:
                res["titles"] = [c["title"] for c in conds]
            else:
                res.update(merge_approval(db, loan, conds, doc["approval_date"] or doc["printed"]))
            pushed.append(res)
        STATUS.update(pushed=pushed, help=help_items, skipped=skipped, files_seen=len(files), dry_run=dry_run)
        return {"dry_run": dry_run, "pushed": pushed, "help": help_items, "skipped": skipped, "files_seen": len(files)}
    except Exception as e:
        STATUS["last_error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        return {"error": STATUS["last_error"]}
    finally:
        db.close()
        STATUS.update(running=False, last_finished=dt.datetime.utcnow().isoformat() + "Z")
        _lock.release()


def _loop():
    time.sleep(60)
    while True:
        if enabled():
            run_once()
        time.sleep(max(5, int(os.environ.get("APPROVAL_SYNC_MINUTES", "30"))) * 60)


def start_scheduler():
    STATUS["enabled"] = enabled()
    threading.Thread(target=_loop, name="approval-sync", daemon=True).start()
