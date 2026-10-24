"""
Owner / manager dashboard for the client portal admin.

GET  /admin/overview            everything the dashboard shows, computed server-side in one call
GET  /admin/settings/comp       compensation + stage-probability settings
PUT  /admin/settings/comp       update them
DELETE /admin/loans/{ln}?confirm=<ln>   archive + delete a bad loan (refuses if the borrower has a login or uploads)
GET  /admin/deleted             archived deletions;  POST /admin/deleted/{ln}/restore  puts one back

Income figures are ESTIMATES from the editable settings - not what was actually charged.
"""
import datetime as dt, json, re

import models
import approval_sync

# ---------------- settings ----------------
DEFAULT_COMP = {
    "programs": {
        # borrower-paid % range + lender-paid YSP % range (YSP only where allowed)
        "DSCR":         {"borrower": [1.0, 3.0], "ysp": [1.0, 2.0], "typical_borrower": 2.0, "typical_ysp": 1.5},
        "HELOC":        {"borrower": [1.0, 3.0], "ysp": [1.0, 2.0], "typical_borrower": 2.0, "typical_ysp": 1.5},
        "Conventional": {"borrower": [1.0, 5.0], "ysp": [0, 0], "typical_borrower": 2.0, "typical_ysp": 0},
        "FHA":          {"borrower": [1.0, 5.0], "ysp": [0, 0], "typical_borrower": 2.0, "typical_ysp": 0},
        "VA":           {"borrower": [1.0, 5.0], "ysp": [0, 0], "typical_borrower": 2.0, "typical_ysp": 0},
        "Construction": {"borrower": [1.0, 5.0], "ysp": [0, 0], "typical_borrower": 2.0, "typical_ysp": 0},
        "Other":        {"borrower": [1.0, 5.0], "ysp": [0, 0], "typical_borrower": 2.0, "typical_ysp": 0},
    },
    "max_total_pct": 5.0,
    # chance a loan in this stage ends up funding (for "expected" income)
    "stage_probability": {"Application": 0.20, "Processing": 0.40, "Underwriting": 0.55, "Conditions": 0.70,
                          "Clear to Close": 0.90, "On Hold": 0.15},
    "stale_days": 7,
    "lock_warn_days": 10,
}
ACTIVE = ["Application", "Processing", "Underwriting", "Conditions", "Clear to Close"]
ORDER = ACTIVE + ["Funded", "On Hold", "Withdrawn"]


def get_settings(db):
    row = db.query(models.AppSetting).filter(models.AppSetting.key == "comp").first()
    s = json.loads(json.dumps(DEFAULT_COMP))
    if row and isinstance(row.value, dict):
        for k, v in row.value.items():
            if isinstance(v, dict) and isinstance(s.get(k), dict):
                s[k].update(v)
            else:
                s[k] = v
    return s


def save_settings(db, value):
    row = db.query(models.AppSetting).filter(models.AppSetting.key == "comp").first()
    if row:
        row.value = value
    else:
        db.add(models.AppSetting(key="comp", value=value))
    db.commit()
    return get_settings(db)


# ---------------- helpers ----------------
def program(loan_type):
    t = (loan_type or "").lower()
    if re.search(r"dscr|non[- ]?qm|investor|business purpose|bank statement|p&l", t): return "DSCR"
    if re.search(r"heloc|home equity|line of credit|2nd|second lien|closed[- ]end second", t): return "HELOC"
    if re.search(r"\bfha\b", t): return "FHA"
    if re.search(r"\bva\b|veteran", t): return "VA"
    if re.search(r"construct|renovat|fix", t): return "Construction"
    if re.search(r"conv|conforming|fannie|freddie|fnma|fhlmc|jumbo", t): return "Conventional"
    return "Other"


def income(loan, s):
    amt = float(loan.loan_amount or 0)
    p = s["programs"].get(program(loan.loan_type), s["programs"]["Other"])
    cap = float(s.get("max_total_pct", 5.0))
    lo = min(p["borrower"][0] + p["ysp"][0], cap)
    hi = min(p["borrower"][1] + p["ysp"][1], cap)
    ty = min(p["typical_borrower"] + p["typical_ysp"], cap)
    return {"low": round(amt * lo / 100), "typical": round(amt * ty / 100), "high": round(amt * hi / 100),
            "typical_pct": ty}


def _aware(d):
    if d is None:
        return None
    if isinstance(d, str):
        try:
            d = dt.datetime.fromisoformat(d.replace("Z", "+00:00"))
        except Exception:
            return None
    if isinstance(d, dt.date) and not isinstance(d, dt.datetime):
        d = dt.datetime(d.year, d.month, d.day)
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _parse_close(txt, now):
    """est_closing_date is free text ('Jun 5', '2026-11-03', '11/03/2026'). Best effort."""
    if not txt or str(txt).strip().upper() in ("TBD", "-", "—"):
        return None
    t = str(txt).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y"):
        try:
            return dt.datetime.strptime(t[:20], fmt).replace(tzinfo=dt.timezone.utc)
        except Exception:
            pass
    for fmt in ("%b %d", "%B %d"):
        try:
            d = dt.datetime.strptime(t, fmt).replace(year=now.year, tzinfo=dt.timezone.utc)
            return d
        except Exception:
            pass
    return None


def is_test(l):
    return bool(re.match(r"LN-", l.loan_number or "", re.I) or
                re.search(r"\btest\b|sample", f"{l.borrower_name or ''} {l.borrower_email or ''}", re.I))


def placeholder_email(e):
    e = (e or "").strip().lower()
    return (not e) or e in ("tbd", "-", "—") or "todo." in e or "needs-update" in e or "@" not in e


# ---------------- the overview ----------------
def overview(db):
    now = dt.datetime.now(dt.timezone.utc)
    s = get_settings(db)
    loans = db.query(models.Loan).all()
    real = [l for l in loans if not is_test(l)]
    by_id = {l.id: l for l in loans}

    conds = {}
    for c in db.query(models.Condition).all():
        d = conds.setdefault(c.loan_id, {"needed": 0, "on_file": 0, "cleared": 0})
        if c.done:
            d["cleared"] += 1
        elif (c.detail or "").startswith("\U0001F4C4"):
            d["on_file"] += 1
        else:
            d["needed"] += 1
    last_act = {}
    for a in db.query(models.ActivityEvent).all():
        t = _aware(a.created_at)
        if t and (a.loan_id not in last_act or t > last_act[a.loan_id]):
            last_act[a.loan_id] = t
    accounts = {b.loan_id for b in db.query(models.Borrower).all()}
    docs = {}
    for d in db.query(models.Document).all():
        x = docs.setdefault(d.loan_id, {"count": 0, "last": None})
        x["count"] += 1
        t = _aware(d.created_at)
        if t and (x["last"] is None or t > x["last"]):
            x["last"] = t
    exports = {e.document_id: e for e in db.query(models.DocumentExport).all()}

    rows = []
    for l in real:
        stage = l.stage or "Application"
        sd = _aware(l.stage_date) or _aware(l.created_at)
        la = last_act.get(l.id) or sd            # NOT updated_at: every CRM sync touches it
        inc = income(l, s)
        close = _parse_close(l.est_closing_date, now)
        lock = _aware(l.rate_lock_expires)
        c = conds.get(l.id, {"needed": 0, "on_file": 0, "cleared": 0})
        rows.append({
            "loan_number": l.loan_number, "borrower": l.borrower_name, "email": l.borrower_email,
            "stage": stage, "program": program(l.loan_type), "loan_type": l.loan_type,
            "amount": float(l.loan_amount or 0), "rate": float(l.rate) if l.rate is not None else None,
            "ltv": l.ltv, "address": l.property_address,
            "days_in_stage": (now - sd).days if sd else None,
            "days_since_activity": (now - la).days if la else None,
            "last_activity": la.isoformat() if la else None,
            "stage_date": sd.isoformat() if sd else None,
            "est_close": l.est_closing_date, "est_close_days": (close - now).days if close else None,
            "lock_days": (lock - now).days if lock else None,
            "income_low": inc["low"], "income_typical": inc["typical"], "income_high": inc["high"],
            "income_pct": inc["typical_pct"],
            "probability": s["stage_probability"].get(stage, 1.0 if stage == "Funded" else 0),
            "conditions": c, "portal_active": l.id in accounts,
            "docs": docs.get(l.id, {}).get("count", 0),
            "last_upload": docs.get(l.id, {}).get("last").isoformat() if docs.get(l.id, {}).get("last") else None,
            "placeholder_email": placeholder_email(l.borrower_email),
        })

    active = [r for r in rows if r["stage"] in ACTIVE]
    funded = [r for r in rows if r["stage"] == "Funded"]

    def in_range(r, days=None, month=False, year=False):
        t = _aware(r["stage_date"])
        if not t:
            return False
        if month:
            return t.year == now.year and t.month == now.month
        if year:
            return t.year == now.year
        return (now - t).days <= days

    # ---- summary & funnel ----
    stages = []
    for st in ORDER:
        g = [r for r in rows if r["stage"] == st]
        stages.append({"stage": st, "count": len(g), "volume": sum(r["amount"] for r in g)})
    programs = {}
    for r in active:
        p = programs.setdefault(r["program"], {"count": 0, "volume": 0, "income_typical": 0})
        p["count"] += 1; p["volume"] += r["amount"]; p["income_typical"] += r["income_typical"]
    closed_12m = [r for r in rows if r["stage"] in ("Funded", "Withdrawn") and in_range(r, days=365)]
    pull_through = (sum(1 for r in closed_12m if r["stage"] == "Funded") / len(closed_12m)) if closed_12m else None
    aging = {}
    for st in ACTIVE:
        d = [r["days_in_stage"] for r in rows if r["stage"] == st and r["days_in_stage"] is not None]
        aging[st] = round(sum(d) / len(d), 1) if d else None

    summary = {
        "active_count": len(active), "active_volume": sum(r["amount"] for r in active),
        "funded_mtd_count": sum(1 for r in funded if in_range(r, month=True)),
        "funded_mtd_volume": sum(r["amount"] for r in funded if in_range(r, month=True)),
        "funded_ytd_count": sum(1 for r in funded if in_range(r, year=True)),
        "funded_ytd_volume": sum(r["amount"] for r in funded if in_range(r, year=True)),
        "withdrawn_90d": sum(1 for r in rows if r["stage"] == "Withdrawn" and in_range(r, days=90)),
        "pull_through_12m": round(pull_through * 100) if pull_through is not None else None,
        "portal_activation_active": round(100 * sum(1 for r in active if r["portal_active"]) / len(active)) if active else None,
        "avg_days_in_stage": aging,
    }

    # ---- income ----
    inc = {
        "pipeline_low": sum(r["income_low"] for r in active),
        "pipeline_typical": sum(r["income_typical"] for r in active),
        "pipeline_high": sum(r["income_high"] for r in active),
        "expected": round(sum(r["income_typical"] * r["probability"] for r in active)),
        "near_term": sum(r["income_typical"] for r in active if r["stage"] in ("Conditions", "Clear to Close")),
        "earned_mtd": sum(r["income_typical"] for r in funded if in_range(r, month=True)),
        "earned_ytd": sum(r["income_typical"] for r in funded if in_range(r, year=True)),
        "by_stage": {st: sum(r["income_typical"] for r in active if r["stage"] == st) for st in ACTIVE},
    }

    # ---- needs attention ----
    att = []
    def flag(sev, r, kind, msg):
        att.append({"severity": sev, "loan_number": r["loan_number"], "borrower": r["borrower"], "stage": r["stage"],
                    "kind": kind, "message": msg, "income": r["income_typical"]})
    for r in active:
        if r["lock_days"] is not None and r["lock_days"] < 0:
            flag("high", r, "Rate lock", f"Rate lock EXPIRED {-r['lock_days']} day(s) ago")
        elif r["lock_days"] is not None and r["lock_days"] <= s["lock_warn_days"]:
            flag("high", r, "Rate lock", f"Rate lock expires in {r['lock_days']} day(s)")
        if r["est_close_days"] is not None and r["est_close_days"] < 0:
            flag("high", r, "Past closing date", f"Est. closing ({r['est_close']}) has passed")
        if r["days_since_activity"] is not None and r["days_since_activity"] >= s["stale_days"]:
            flag("medium" if r["days_since_activity"] < 21 else "high", r, "Stale file",
                 f"No activity in {r['days_since_activity']} days ({r['stage']})")
        if r["conditions"]["needed"] and r["stage"] in ("Conditions", "Underwriting", "Clear to Close"):
            flag("medium", r, "Borrower items", f"{r['conditions']['needed']} condition(s) still needed from borrower")
        if not r["portal_active"] and not r["placeholder_email"]:
            flag("low", r, "Portal not activated", "Borrower hasn't set up their portal login")
        if r["placeholder_email"]:
            flag("medium", r, "Missing email", "No real borrower email - can't invite or notify")
    for r in rows:
        if r["stage"] == "On Hold":
            flag("medium", r, "On hold", f"On hold for {r['days_in_stage']} day(s)")
    sev_rank = {"high": 0, "medium": 1, "low": 2}
    att.sort(key=lambda a: (sev_rank[a["severity"]], -a["income"]))

    # ---- data quality ----
    dq = []
    groups = {}
    for l in real:
        k = approval_sync.addr_key(l.property_address)
        if k and (l.stage or "") in ACTIVE:
            groups.setdefault(k, []).append(l)
    for k, g in groups.items():
        if len(g) > 1:
            dq.append({"kind": "Duplicate", "loans": [x.loan_number for x in g],
                       "message": f"{len(g)} active loans at {g[0].property_address}: " +
                                  ", ".join(f"{x.loan_number} ({x.borrower_email})" for x in g)})
    snap = db.query(models.PulseSnapshot).filter(models.PulseSnapshot.key == "pipeline").first()
    crm_nums = set()
    if snap and isinstance(snap.data, dict):
        for v in (snap.data.get("loans") or {}).values():
            if isinstance(v, dict) and v.get("num"):
                crm_nums.add(str(v["num"]).strip())
    if crm_nums:
        for l in real:
            if l.loan_number not in crm_nums and (l.stage or "") in ACTIVE:
                dq.append({"kind": "Not in CRM", "loans": [l.loan_number],
                           "message": f"{l.loan_number} {l.borrower_name} is in the portal but not in Pulse CRM (deleted there?)"})
    for r in rows:
        if r["placeholder_email"]:
            dq.append({"kind": "Placeholder email", "loans": [r["loan_number"]], "message": f"{r['loan_number']} {r['borrower']}: '{r['email']}'"})
        if not r["amount"] and r["stage"] in ACTIVE:
            dq.append({"kind": "Missing amount", "loans": [r["loan_number"]], "message": f"{r['loan_number']} {r['borrower']} has no loan amount"})
    tests = [l.loan_number for l in loans if is_test(l)]
    if tests:
        dq.append({"kind": "Test data", "loans": tests, "message": f"{len(tests)} test loans (LN-/test) still in the portal"})

    # ---- suggestions (ranked by money / urgency) ----
    sug = []
    def add(prio, title, detail, loans=None, value=0):
        sug.append({"priority": prio, "title": title, "detail": detail, "loans": loans or [], "value": value})
    locks = [a for a in att if a["kind"] == "Rate lock"]
    if locks:
        add(1, f"Protect {len(locks)} rate lock(s)", "Call lender / extend before they lapse: " +
            ", ".join(f"{a['loan_number']} {a['borrower']}" for a in locks[:6]), [a["loan_number"] for a in locks],
            sum(a["income"] for a in locks))
    cond_heavy = sorted([r for r in active if r["conditions"]["needed"] >= 1 and r["stage"] in ("Conditions", "Underwriting")],
                        key=lambda r: -r["income_typical"])
    if cond_heavy:
        add(1, f"Chase borrower items on {len(cond_heavy)} file(s) close to approval",
            "Highest value first: " + ", ".join(f"{r['loan_number']} {r['borrower']} ({r['conditions']['needed']} needed)" for r in cond_heavy[:6]),
            [r["loan_number"] for r in cond_heavy], sum(r["income_typical"] for r in cond_heavy))
    stale = sorted([r for r in active if (r["days_since_activity"] or 0) >= s["stale_days"]], key=lambda r: -r["income_typical"])
    if stale:
        add(2, f"Revive {len(stale)} stalled file(s)", "No movement in 7+ days: " +
            ", ".join(f"{r['loan_number']} {r['borrower']} ({r['days_since_activity']}d)" for r in stale[:6]),
            [r["loan_number"] for r in stale], sum(r["income_typical"] for r in stale))
    inactive = [r for r in active if not r["portal_active"] and not r["placeholder_email"]]
    if inactive:
        add(2, f"Get {len(inactive)} active borrower(s) onto the portal",
            "Clients on the portal upload faster and call less. Resend invites to: " +
            ", ".join(f"{r['borrower']}" for r in inactive[:8]), [r["loan_number"] for r in inactive])
    heloc = [r for r in funded if r["program"] != "HELOC" and _ltv(r["ltv"]) is not None and _ltv(r["ltv"]) <= 65]
    if heloc:
        add(3, f"{len(heloc)} funded client(s) with equity - HELOC candidates",
            "Funded at 65% LTV or lower: " + ", ".join(f"{r['borrower']} ({r['ltv']})" for r in heloc[:8]),
            [r["loan_number"] for r in heloc])
    wd = [r for r in rows if r["stage"] == "Withdrawn" and in_range(r, days=180)]
    if wd:
        add(3, f"Re-engage {len(wd)} withdrawn file(s) from the last 6 months",
            "Rates/programs may fit now: " + ", ".join(r["borrower"] for r in wd[:8]), [r["loan_number"] for r in wd])
    if dq:
        add(2, f"Clean up {len(dq)} data-quality issue(s)", "Duplicates, placeholder emails, test loans and portal-only records skew the numbers - see Data Quality.")
    if pull_through is not None and pull_through < 0.5:
        add(2, f"Pull-through is {round(pull_through*100)}% (last 12 months)", "More than half of closed files withdrew - review why at application (pricing, docs, timing).")
    sug.sort(key=lambda x: (x["priority"], -x["value"]))

    # ---- automation health ----
    ast = approval_sync.STATUS
    health = {
        "approval_sync": {"enabled": approval_sync.enabled(), "last_finished": ast.get("last_finished"),
                          "error": ast.get("last_error"), "files_seen": ast.get("files_seen"),
                          "help": len(ast.get("help") or []), "stages_fixed": len(ast.get("stages_fixed") or [])},
        "campaign_sent": db.query(models.CampaignSend).count(),
        "unsubscribed": db.query(models.EmailSuppression).count(),
        "portal_uploads_exported": sum(1 for e in exports.values() if (e.onedrive_path or "").startswith(("Pipeline", "/"))),
    }

    rows.sort(key=lambda r: (ORDER.index(r["stage"]) if r["stage"] in ORDER else 99, -(r["amount"] or 0)))
    return {"generated": now.isoformat(), "settings": s, "summary": summary, "stages": stages, "programs": programs,
            "income": inc, "attention": att, "suggestions": sug, "data_quality": dq, "health": health, "loans": rows}


def _ltv(v):
    try:
        return float(re.sub(r"[^0-9.]", "", str(v)))
    except Exception:
        return None


# ---------------- safe delete / restore ----------------
def delete_loan(db, loan_number, confirm, who="admin"):
    l = db.query(models.Loan).filter(models.Loan.loan_number == loan_number).first()
    if not l:
        return 404, "Unknown loan number."
    if confirm != loan_number:
        return 400, "Type the loan number exactly to confirm."
    if db.query(models.Borrower).filter(models.Borrower.loan_id == l.id).first():
        return 409, "The borrower has a portal login on this loan - set it to Withdrawn instead of deleting."
    if db.query(models.Document).filter(models.Document.loan_id == l.id).first():
        return 409, "This loan has uploaded documents - set it to Withdrawn instead of deleting."
    snap = {c.name: (str(getattr(l, c.name)) if getattr(l, c.name) is not None else None) for c in l.__table__.columns}
    snap["_conditions"] = [{"title": c.title, "detail": c.detail, "done": c.done} for c in l.conditions]
    snap["_activity"] = [{"text": a.text, "created_at": str(a.created_at)} for a in l.activity]
    db.add(models.DeletedLoan(loan_number=loan_number, data=snap, deleted_by=who))
    db.delete(l)
    db.commit()
    return 200, "Deleted (archived - can be restored)."


def restore_loan(db, loan_number):
    row = db.query(models.DeletedLoan).filter(models.DeletedLoan.loan_number == loan_number).order_by(
        models.DeletedLoan.deleted_at.desc()).first()
    if not row:
        return 404, "Not in the deleted archive."
    if db.query(models.Loan).filter(models.Loan.loan_number == loan_number).first():
        return 409, "A loan with that number already exists."
    d = dict(row.data)
    cols = {c.name: c for c in models.Loan.__table__.columns}
    kw = {}
    for k, v in d.items():
        if k in cols and k not in ("created_at", "updated_at") and v is not None:
            if k in ("stage_date", "date_submitted", "rate_lock_expires"):
                v = _aware(v)
            kw[k] = v
    l = models.Loan(**kw)
    db.add(l); db.flush()
    for c in d.get("_conditions", []):
        db.add(models.Condition(loan_id=l.id, title=c["title"], detail=c.get("detail"), done=bool(c.get("done"))))
    for a in d.get("_activity", []):
        db.add(models.ActivityEvent(loan_id=l.id, text=a["text"]))
    db.delete(row)
    db.commit()
    return 200, "Restored."
