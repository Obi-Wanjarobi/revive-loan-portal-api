"""
Client email campaign: branded HTML email sent through Microsoft 365 (from Jay's mailbox) via Microsoft Graph.

- One email per client (deduped by email AND by borrower name; most current loan wins).
- LN-xxxx test loans, invalid / internal addresses, CAMPAIGN_EXCLUDE loans and unsubscribed emails are skipped.
- Every email has a one-click unsubscribe link -> permanent suppression list (EmailSuppression table).
- Each (campaign, email) is sent at most once (CampaignSend table).
- Uses the "Revive Portal Sync" Entra app; needs Microsoft Graph Mail.Send (Application) with admin consent.

Railway variables (optional):
  CAMPAIGN_FROM            sender mailbox (default jturner@myrevivecapital.com)
  CAMPAIGN_EXCLUDE         comma-separated loan numbers to never email (e.g. wrong email on file, staff)
  CAMPAIGN_LICENSE_LINE    state license disclosure printed in the footer (left out if blank)
  PUBLIC_API_BASE          this API's public URL (for unsubscribe links)
"""
import datetime as dt, hashlib, hmac, html, json, os, re, threading, time, urllib.parse, urllib.request

import models
from database import SessionLocal
import approval_sync

CAMPAIGN_ID = "heloc-portal-2026-10"
SUBJECT = "Need cash? Plus your new Revive Capital client portal"
PORTAL = "https://revivecapclientportal.com"
STAGE_RANK = {"clear to close": 7, "conditions": 6, "underwriting": 5, "processing": 4, "application": 3, "funded": 2, "withdrawn": 1}
_EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
STATUS = {"running": False, "sent": [], "failed": [], "skipped": [], "started": None, "finished": None, "dry_run": None}
_lock = threading.Lock()


def _secret():
    return (os.environ.get("JWT_SECRET") or "dev").encode()


def unsub_token(email):
    return hmac.new(_secret(), email.strip().lower().encode(), hashlib.sha256).hexdigest()[:32]


def unsub_url(email):
    base = os.environ.get("PUBLIC_API_BASE", "https://revive-loan-portal-api-production.up.railway.app").rstrip("/")
    return f"{base}/u?e={urllib.parse.quote(email.strip().lower())}&t={unsub_token(email)}"


# ---------------- recipients ----------------
def recipients(db):
    excluded = {x.strip().upper() for x in os.environ.get("CAMPAIGN_EXCLUDE", "").split(",") if x.strip()}
    suppressed = {s.email for s in db.query(models.EmailSuppression).all()}
    sent = {s.email for s in db.query(models.CampaignSend).filter(models.CampaignSend.campaign == CAMPAIGN_ID).all()}
    registered = {b.email.lower() for b in db.query(models.Borrower).all()}
    pool, skipped = [], []
    for l in db.query(models.Loan).all():
        e = (l.borrower_email or "").strip()
        why = None
        if re.match(r"LN-", l.loan_number or "", re.I):
            continue
        if (l.loan_number or "").upper() in excluded:
            why = "excluded (CAMPAIGN_EXCLUDE)"
        elif not _EMAIL_OK.match(e) or re.search(r"revive|example\.com|\btest\b", e, re.I):
            why = "no usable email"
        if why:
            skipped.append({"loan": l.loan_number, "name": l.borrower_name, "why": why})
            continue
        pool.append(l)
    best = {}
    for l in pool:                                  # dedupe by email, then by borrower name
        for key in ("e:" + l.borrower_email.strip().lower(), "n:" + " ".join((l.borrower_name or "").lower().split())):
            cur = best.get(key)
            if cur is None or (STAGE_RANK.get((l.stage or "").lower(), 0), l.loan_number) > \
                    (STAGE_RANK.get((cur.stage or "").lower(), 0), cur.loan_number):
                best[key] = l
    chosen = {}
    for k, l in best.items():
        if k.startswith("e:") and best.get("n:" + " ".join((l.borrower_name or "").lower().split())) is l:
            chosen[l.loan_number] = l
    out = []
    for l in sorted(chosen.values(), key=lambda x: (x.borrower_name or "")):
        e = l.borrower_email.strip().lower()
        if e in suppressed:
            skipped.append({"loan": l.loan_number, "name": l.borrower_name, "why": "unsubscribed"}); continue
        if e in sent:
            skipped.append({"loan": l.loan_number, "name": l.borrower_name, "why": "already sent"}); continue
        active = (l.stage or "").lower() not in ("funded", "withdrawn")
        out.append({"loan": l.loan_number, "name": (l.borrower_name or "").strip(), "email": l.borrower_email.strip(),
                    "active": active, "registered": e in registered})
    return out, skipped


# ---------------- the email ----------------
def first_name(name):
    w = re.sub(r"\(.*?\)", "", name or "").split()
    return w[0].title() if w else "there"


def render(r):
    """Table-based, inline-styled HTML that renders in Outlook, Gmail and phones. No images (nothing to block)."""
    e = html.escape
    fn = e(first_name(r["name"]))
    link = f"{PORTAL}/?invite={urllib.parse.quote(r['loan'])}"
    if r.get("registered"):
        portal_text = "Your loan is in our secure client portal &mdash; log in any time to see where things stand."
        btn_text, btn_link, how = "Log in to my portal", PORTAL, ""
    elif r.get("active"):
        portal_text = ("You can now follow your loan online: your current stage, what&rsquo;s still needed from you, "
                       "and what we already have on file.")
        btn_text, btn_link = "Set up my portal", link
        how = f'Choose &ldquo;First time here,&rdquo; confirm loan # <b>{e(r["loan"])}</b> and this email address, then create a password.'
    else:
        portal_text = ("Your loan file with us now lives in our secure client portal &mdash; "
                       "and it&rsquo;s the fastest way to start your next one.")
        btn_text, btn_link = "Set up my portal", link
        how = f'Choose &ldquo;First time here,&rdquo; confirm loan # <b>{e(r["loan"])}</b> and this email address, then create a password.'
    lic = os.environ.get("CAMPAIGN_LICENSE_LINE", "").strip()
    lic_html = f"{e(lic)} " if lic else ""
    heloc_mail = ("mailto:jturner@myrevivecapital.com?subject=" +
                  urllib.parse.quote("HELOC - I'd like to learn more"))
    font = "font-family:Arial,Helvetica,sans-serif;"
    def button(href, label, bg):
        return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
                f'<td bgcolor="{bg}" style="border-radius:8px;"><a href="{href}" target="_blank" '
                f'style="display:inline-block;padding:13px 26px;{font}font-size:15px;font-weight:bold;'
                f'color:#ffffff;text-decoration:none;border-radius:8px;">{label}</a></td></tr></table>')
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F2F4F7;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#F2F4F7"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;background:#ffffff;border:1px solid #E1E6ED;border-radius:14px;">
 <tr><td bgcolor="#0F1E35" style="padding:20px 28px;border-radius:14px 14px 0 0;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
   <td bgcolor="#1A5FA8" width="34" height="34" align="center" style="border-radius:8px;{font}font-size:17px;font-weight:bold;color:#ffffff;">R</td>
   <td style="padding-left:10px;{font}"><div style="font-size:17px;font-weight:bold;color:#ffffff;">Revive Capital</div>
   <div style="font-size:9px;letter-spacing:2px;color:#9AA5B3;">CLIENT PORTAL</div></td></tr></table>
 </td></tr>
 <tr><td style="padding:30px 28px 8px;{font}color:#1B2A4A;">
  <p style="margin:0 0 14px;font-size:15px;">Hi {fn},</p>
  <h1 style="margin:0 0 10px;font-size:30px;line-height:1.15;color:#1B2A4A;">Need cash?</h1>
  <p style="margin:0 0 16px;font-size:17px;line-height:1.45;color:#1B2A4A;">If increasing payments and higher costs are causing you stress, <b>the equity in your home might be the solution.</b></p>
  <p style="margin:0 0 20px;font-size:14px;line-height:1.6;color:#4A5568;">A Home Equity Line of Credit (HELOC) lets you borrow against your home&rsquo;s equity as you need it &mdash; to pay down higher-interest debt, cover big expenses, or keep cash on hand &mdash; while keeping your current first mortgage in place. I&rsquo;ll walk you through your options. No cost, no obligation.</p>
  {button(heloc_mail, "Ask Jay about a HELOC", "#1A7A4A")}
  <p style="margin:12px 0 0;font-size:13px;color:#6B7686;">Or call me directly at <a href="tel:9497350829" style="color:#1A5FA8;text-decoration:none;">949-735-0829</a>.</p>
 </td></tr>
 <tr><td style="padding:22px 28px 6px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#EEF4FC" style="border-radius:12px;"><tr><td style="padding:20px 22px;{font}color:#1B2A4A;">
  <div style="font-size:10px;font-weight:bold;letter-spacing:1.5px;color:#1A5FA8;margin-bottom:6px;">ALSO NEW &middot; YOUR CLIENT PORTAL</div>
  <p style="margin:0 0 14px;font-size:14px;line-height:1.55;">{portal_text}</p>
  {button(btn_link, btn_text, "#1A5FA8")}
  <p style="margin:12px 0 0;font-size:12px;line-height:1.5;color:#6B7686;">{how}</p>
 </td></tr></table></td></tr>
 <tr><td style="padding:22px 28px 26px;{font}font-size:13px;line-height:1.55;color:#1B2A4A;">
  <b>Jay Turner</b><br>Loan Officer &middot; NMLS #1369080<br>Revive Capital LLC &middot; NMLS #2372330<br>
  <a href="tel:9497350829" style="color:#1A5FA8;text-decoration:none;">949-735-0829</a> &middot; (888) 901-5105<br>
  <a href="https://www.myrevivecapital.com" style="color:#1A5FA8;text-decoration:none;">myrevivecapital.com</a> &middot;
  <a href="https://www.myreviveholdings.com" style="color:#1A5FA8;text-decoration:none;">myreviveholdings.com</a>
 </td></tr>
</table>
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;"><tr><td style="padding:16px 20px;{font}font-size:11px;line-height:1.55;color:#8A94A6;">
  Revive Capital LLC &middot; 400 Spectrum Center Dr, Suite 1900, Irvine, CA 92618 &middot; Equal Housing Opportunity.
  This is not a commitment to lend. All loans are subject to credit approval, property eligibility and program guidelines;
  not all products are available in every state. A HELOC is secured by your home. {lic_html}Verify licensing at
  <a href="https://www.nmlsconsumeraccess.org" style="color:#8A94A6;">nmlsconsumeraccess.org</a>.<br><br>
  You&rsquo;re receiving this because you&rsquo;ve worked with Revive Capital.
  <a href="{unsub_url(r['email'])}" style="color:#8A94A6;">Unsubscribe</a> from marketing emails.
</td></tr></table>
</td></tr></table></body></html>"""


# ---------------- sending (Microsoft Graph, app-only) ----------------
def send_one(to_email, subject, body_html):
    sender = urllib.parse.quote(os.environ.get("CAMPAIGN_FROM", "jturner@myrevivecapital.com"))
    payload = {"message": {"subject": subject, "body": {"contentType": "HTML", "content": body_html},
                           "toRecipients": [{"emailAddress": {"address": to_email}}]},
               "saveToSentItems": True}
    req = urllib.request.Request(f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail",
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + approval_sync._graph_token(),
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Graph {e.code}: {e.read()[:300].decode('utf8', 'ignore')}")


def approved():
    return os.environ.get("CAMPAIGN_APPROVED", "").strip().lower() == "yes"


def run(dry_run=True, limit=None, only_loans=None, pace_seconds=3):
    if not dry_run and not approved():
        return {"status": "blocked", "detail": "Set CAMPAIGN_APPROVED=yes in Railway after consent + license line are cleared."}
    if not _lock.acquire(blocking=False):
        return {"status": "already running"}
    STATUS.update(running=True, sent=[], failed=[], skipped=[], dry_run=dry_run,
                  started=dt.datetime.utcnow().isoformat() + "Z", finished=None)
    db = SessionLocal()
    try:
        recips, skipped = recipients(db)
        if only_loans:
            want = {x.strip().upper() for x in only_loans}
            recips = [r for r in recips if r["loan"].upper() in want]
        if limit:
            recips = recips[:int(limit)]
        STATUS["skipped"] = skipped
        for r in recips:
            if dry_run:
                STATUS["sent"].append({**r, "dry_run": True}); continue
            try:
                send_one(r["email"], SUBJECT, render(r))
                db.add(models.CampaignSend(campaign=CAMPAIGN_ID, email=r["email"].lower(), loan_number=r["loan"]))
                db.commit()
                STATUS["sent"].append(r)
            except Exception as ex:
                STATUS["failed"].append({**r, "error": str(ex)[:300]})
                if "429" in str(ex):
                    time.sleep(60)
            time.sleep(pace_seconds)
        return {k: STATUS[k] for k in ("sent", "failed", "skipped")}
    finally:
        db.close()
        STATUS.update(running=False, finished=dt.datetime.utcnow().isoformat() + "Z")
        _lock.release()


def send_test(to_email, loan_number=None):
    """Send the real rendered email for one client to an internal address (e.g. Jay) - never to the client."""
    db = SessionLocal()
    try:
        recips, _ = recipients(db)
        r = next((x for x in recips if not loan_number or x["loan"] == loan_number), None)
        if r is None:
            raise ValueError("no recipient found for that loan")
        preview = {**r, "email": to_email}                  # unsubscribe link points at the TEST address, never the client's
        send_one(to_email, "[TEST] " + SUBJECT, render(preview))
        return {"sent_to": to_email, "rendered_for": r["loan"]}
    finally:
        db.close()
