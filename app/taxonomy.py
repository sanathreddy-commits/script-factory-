"""Central taxonomy definitions, quota tracking, filename parser, and folder explorer."""
import re
import os
import sqlite3

# Official taxonomy specification based on client targets
TAXONOMY_ITEMS = [
    # Healthcare: Doctor-Patient Consultation (167 scripts total)
    {
        "key": "hc_doc_gp",
        "domain": "Healthcare",
        "subdomain": "Doctor-Patient Consultation",
        "specialisation": "General Physician",
        "quota": 84,
        "share": "50%",
        "roles": "Doctor, Patient",
        "keywords": ["general physician", "physician", "fever", "cough", "consultation", "clinical", "general doctor"]
    },
    {
        "key": "hc_doc_ped",
        "domain": "Healthcare",
        "subdomain": "Doctor-Patient Consultation",
        "specialisation": "Pediatrician",
        "quota": 17,
        "share": "10%",
        "roles": "Doctor, Patient",
        "keywords": ["pediatric", "pediatrician", "child", "infant", "baby"]
    },
    {
        "key": "hc_doc_derm",
        "domain": "Healthcare",
        "subdomain": "Doctor-Patient Consultation",
        "specialisation": "Dermatology",
        "quota": 25,
        "share": "15%",
        "roles": "Doctor, Patient",
        "keywords": ["dermatol", "skin", "rash", "allergy", "itching"]
    },
    {
        "key": "hc_doc_dent",
        "domain": "Healthcare",
        "subdomain": "Doctor-Patient Consultation",
        "specialisation": "Dentist",
        "quota": 25,
        "share": "15%",
        "roles": "Doctor, Patient",
        "keywords": ["dentist", "dentistry", "dental", "tooth", "teeth", "cavity"]
    },
    {
        "key": "hc_doc_gyn",
        "domain": "Healthcare",
        "subdomain": "Doctor-Patient Consultation",
        "specialisation": "Gynecologist",
        "quota": 8,
        "share": "5%",
        "roles": "Doctor, Patient",
        "keywords": ["gynecol", "gynecology", "pregnancy", "women", "maternity"]
    },
    {
        "key": "hc_doc_orth",
        "domain": "Healthcare",
        "subdomain": "Doctor-Patient Consultation",
        "specialisation": "Orthopedist",
        "quota": 8,
        "share": "5%",
        "roles": "Doctor, Patient",
        "keywords": ["orthoped", "orthopedic", "bone", "joint", "fracture", "knee", "spine"]
    },
    # Other Healthcare Sub-domains (167 each)
    {
        "key": "hc_pharm",
        "domain": "Healthcare",
        "subdomain": "Pharmacy",
        "specialisation": "",
        "quota": 167,
        "share": "100%",
        "roles": "Pharmacist, Customer/Patient",
        "keywords": ["pharmacy", "chemist", "medicine", "prescription", "refill", "dosage"]
    },
    {
        "key": "hc_ins",
        "domain": "Healthcare",
        "subdomain": "Health Insurance (Claims/Policy)",
        "specialisation": "",
        "quota": 167,
        "share": "100%",
        "roles": "Insurance Agent, Patient/Policyholder",
        "keywords": ["health insurance", "mediclaim", "tpa", "hospital claim", "pre-auth", "cashless"]
    },
    {
        "key": "hc_tele",
        "domain": "Healthcare",
        "subdomain": "Telehealth",
        "specialisation": "",
        "quota": 167,
        "share": "100%",
        "roles": "Doctor, Patient",
        "keywords": ["telehealth", "telemedicine", "remote consultation", "online consultation", "video call"]
    },
    # BFSI Sub-domains (167 each)
    {
        "key": "bfsi_loans",
        "domain": "BFSI",
        "subdomain": "Banking — Loans",
        "specialisation": "",
        "quota": 167,
        "share": "100%",
        "roles": "Bank Agent, Customer",
        "keywords": ["loan", "home loan", "personal loan", "car loan", "emi", "interest rate", "foreclosure"]
    },
    {
        "key": "bfsi_acc",
        "domain": "BFSI",
        "subdomain": "Banking — Accounts",
        "specialisation": "",
        "quota": 167,
        "share": "100%",
        "roles": "Bank Agent, Customer",
        "keywords": ["account", "savings account", "current account", "kyc", "statement", "passbook", "freeze", "balance"]
    },
    {
        "key": "bfsi_cards",
        "domain": "BFSI",
        "subdomain": "Banking — Cards",
        "specialisation": "",
        "quota": 167,
        "share": "100%",
        "roles": "Bank Agent, Customer",
        "keywords": ["card", "credit card", "debit card", "pin", "block card", "reward points", "card dispute"]
    },
    {
        "key": "bfsi_ins_claims",
        "domain": "BFSI",
        "subdomain": "Insurance — Claims",
        "specialisation": "",
        "quota": 167,
        "share": "100%",
        "roles": "Insurance Agent, Customer",
        "keywords": ["insurance claim", "claim status", "claim settlement", "accident claim", "claim form"]
    },
    {
        "key": "bfsi_ins_pol",
        "domain": "BFSI",
        "subdomain": "Insurance — Policy",
        "specialisation": "",
        "quota": 167,
        "share": "100%",
        "roles": "Insurance Agent, Customer",
        "keywords": ["insurance policy", "policy renewal", "premium payment", "life insurance", "term plan", "surrender"]
    },
]

TOTAL_QUOTA = sum(item["quota"] for item in TAXONOMY_ITEMS) # 1503


def match_taxonomy(domain="", subdomain="", specialisation=""):
    """Matches script metadata to official taxonomy item."""
    d = (domain or "").strip()
    sub = (subdomain or "").strip()
    spec = (specialisation or "").strip()
    combo = f"{d} {sub} {spec}".lower()

    # 1. Check exact match
    for item in TAXONOMY_ITEMS:
        if spec and item["specialisation"].lower() == spec.lower():
            return item
        if not item["specialisation"] and item["subdomain"].lower() == sub.lower() and item["domain"].lower() == d.lower():
            return item

    # 2. Check specialisation in Doctor-Patient Consultation
    if "pediatric" in combo:
        return next(i for i in TAXONOMY_ITEMS if i["key"] == "hc_doc_ped")
    if "dermatol" in combo:
        return next(i for i in TAXONOMY_ITEMS if i["key"] == "hc_doc_derm")
    if "dent" in combo:
        return next(i for i in TAXONOMY_ITEMS if i["key"] == "hc_doc_dent")
    if "gynecol" in combo:
        return next(i for i in TAXONOMY_ITEMS if i["key"] == "hc_doc_gyn")
    if "orthoped" in combo:
        return next(i for i in TAXONOMY_ITEMS if i["key"] == "hc_doc_orth")
    if "general physician" in combo or "physician" in combo:
        return next(i for i in TAXONOMY_ITEMS if i["key"] == "hc_doc_gp")

    # 3. Check other Healthcare subdomains
    if "pharm" in combo:
        return next(i for i in TAXONOMY_ITEMS if i["key"] == "hc_pharm")
    if "telehealth" in combo or "telemedicine" in combo:
        return next(i for i in TAXONOMY_ITEMS if i["key"] == "hc_tele")
    if ("health" in combo and "insurance" in combo) or "mediclaim" in combo:
        return next(i for i in TAXONOMY_ITEMS if i["key"] == "hc_ins")

    # 4. Check BFSI subdomains
    if "loan" in combo:
        return next(i for i in TAXONOMY_ITEMS if i["key"] == "bfsi_loans")
    if "account" in combo:
        return next(i for i in TAXONOMY_ITEMS if i["key"] == "bfsi_acc")
    if "card" in combo:
        return next(i for i in TAXONOMY_ITEMS if i["key"] == "bfsi_cards")
    if "claim" in combo and "insurance" in combo:
        return next(i for i in TAXONOMY_ITEMS if i["key"] == "bfsi_ins_claims")
    if ("policy" in combo or "premium" in combo) and "insurance" in combo:
        return next(i for i in TAXONOMY_ITEMS if i["key"] == "bfsi_ins_pol")

    # 5. Fallback for doctor consultations
    if "consultation" in combo or "doctor" in combo:
        return next(i for i in TAXONOMY_ITEMS if i["key"] == "hc_doc_gp")

    return None


def parse_filename_metadata(filename, relative_path=""):
    """
    Parses filename or folder path like:
    kannada-Healthcare-Doctor-Patient Consultation-General Physician-001.txt
    or path: kannada/Healthcare/Doctor-Patient Consultation/General Physician/001.txt
    Returns: (lang_name, domain, subdomain, specialisation, script_name)
    """
    # Clean up extension
    base = os.path.splitext(os.path.basename(filename))[0].strip()
    
    # Path parts if provided
    parts = []
    if relative_path:
        norm_path = relative_path.replace("\\", "/").strip("/")
        dir_parts = [p.strip() for p in os.path.dirname(norm_path).split("/") if p.strip()]
        parts.extend(dir_parts)
    
    # Check if filename has delimiters (double dash, dash, or underscore)
    file_parts = []
    if "--" in base:
        file_parts = [p.strip() for p in base.split("--") if p.strip()]
    elif "_" in base and not (" " in base and "_" in base and len(base.split("_")) <= 2):
        file_parts = [p.strip() for p in base.split("_") if p.strip()]
    elif "-" in base:
        # Avoid splitting 'Doctor-Patient' directly by normalizing it first
        norm_base = base.replace("Doctor-Patient", "Doctor—Patient").replace("doctor-patient", "Doctor—Patient")
        file_parts = [p.replace("Doctor—Patient", "Doctor-Patient").strip() for p in norm_base.split("-") if p.strip()]

    all_tokens = parts + file_parts
    combo = " ".join(all_tokens).lower()

    # Detect language
    lang = "Kannada"
    if "marathi" in combo:
        lang = "Marathi"
    elif "kannada" in combo:
        lang = "Kannada"

    # Match taxonomy
    item = match_taxonomy(" ".join(all_tokens), " ".join(all_tokens), " ".join(all_tokens))
    if item:
        domain = item["domain"]
        subdomain = item["subdomain"]
        specialisation = item["specialisation"]
    else:
        domain = "Healthcare" if ("health" in combo or "doc" in combo) else "BFSI" if ("bank" in combo or "insur" in combo) else "General"
        subdomain = file_parts[1] if len(file_parts) > 1 else "Consultation"
        specialisation = ""

    title = file_parts[-1] if file_parts else base
    return lang, domain, subdomain, specialisation, title


def get_taxonomy_stats(c, language_id=None):
    """
    Returns live target vs uploaded vs assigned vs completed counts for each taxonomy item.
    """
    filter_sql = ""
    args = []
    if language_id and language_id != "all":
        filter_sql = "WHERE s.language_id = ?"
        args.append(language_id)

    # Fetch all scripts with their assignment status
    rows = c.execute(f"""
        SELECT s.id, s.domain, s.subdomain, s.specialisation, s.status as script_status,
               a.id as aid, a.status as assign_status
        FROM scripts s
        LEFT JOIN assignments a ON a.script_id = s.id AND a.status NOT IN ('RELEASED', 'ABANDONED')
        {filter_sql}
    """, args).fetchall()

    # Initialize buckets for each taxonomy item
    stats = {}
    for item in TAXONOMY_ITEMS:
        stats[item["key"]] = {
            "item": item,
            "quota": item["quota"],
            "uploaded": 0,
            "in_stock": 0,
            "in_prog": 0,
            "completed": 0,
        }

    other_bucket = {
        "item": {
            "key": "other",
            "domain": "Other",
            "subdomain": "General / Uncategorized",
            "specialisation": "",
            "quota": 0,
            "share": "0%",
            "roles": "Speaker A, Speaker B",
        },
        "quota": 0,
        "uploaded": 0,
        "in_stock": 0,
        "in_prog": 0,
        "completed": 0,
    }

    for r in rows:
        m = match_taxonomy(r["domain"], r["subdomain"], r["specialisation"])
        bucket = stats.get(m["key"]) if m else other_bucket

        bucket["uploaded"] += 1
        a_st = r["assign_status"]
        s_st = r["script_status"]

        if a_st == "CONFIRMED":
            bucket["completed"] += 1
        elif a_st in ("ASSIGNED", "IN_SESSION", "DISPUTED"):
            bucket["in_prog"] += 1
        elif s_st in ("READY", "APPROVED") and not a_st:
            bucket["in_stock"] += 1

    # Format result list with calculated completion percentages
    result = []
    tot_quota, tot_up, tot_stock, tot_prog, tot_done = 0, 0, 0, 0, 0

    for item in TAXONOMY_ITEMS:
        b = stats[item["key"]]
        q = b["quota"]
        up = b["uploaded"]
        stock = b["in_stock"]
        prog = b["in_prog"]
        done = b["completed"]
        pct = round((done * 100.0) / q, 1) if q else 0.0

        tot_quota += q
        tot_up += up
        tot_stock += stock
        tot_prog += prog
        tot_done += done

        result.append({
            "key": item["key"],
            "domain": item["domain"],
            "subdomain": item["subdomain"],
            "specialisation": item["specialisation"],
            "share": item["share"],
            "quota": q,
            "uploaded": up,
            "in_stock": stock,
            "in_prog": prog,
            "completed": done,
            "pct": min(100.0, pct),
        })

    if other_bucket["uploaded"] > 0:
        result.append({
            "key": "other",
            "domain": "Other",
            "subdomain": "General / Uncategorized",
            "specialisation": "",
            "share": "—",
            "quota": 0,
            "uploaded": other_bucket["uploaded"],
            "in_stock": other_bucket["in_stock"],
            "in_prog": other_bucket["in_prog"],
            "completed": other_bucket["completed"],
            "pct": 0.0,
        })
        tot_up += other_bucket["uploaded"]
        tot_stock += other_bucket["in_stock"]
        tot_prog += other_bucket["in_prog"]
        tot_done += other_bucket["completed"]

    overall_pct = round((tot_done * 100.0) / tot_quota, 1) if tot_quota else 0.0
    summary = {
        "total_quota": tot_quota,
        "total_uploaded": tot_up,
        "total_stock": tot_stock,
        "total_in_prog": tot_prog,
        "total_completed": tot_done,
        "overall_pct": min(100.0, overall_pct),
    }

    return result, summary


def create_library_folders(base_dir="scripts_library"):
    """Creates the physical on-disk directory hierarchy for all languages and taxonomies."""
    langs = ["Kannada", "Marathi"]
    created = []
    for l in langs:
        for item in TAXONOMY_ITEMS:
            dom = item["domain"]
            sub = item["subdomain"]
            spec = item["specialisation"]
            if spec:
                p = os.path.join(base_dir, l, dom, sub, spec)
            else:
                p = os.path.join(base_dir, l, dom, sub)
            os.makedirs(p, exist_ok=True)
            created.append(p)
    return created


def scan_and_import_folder(c, base_dir="scripts_library", target_lang_id=None):
    """
    Scans base_dir for all .txt, .md files and imports them.
    Auto-detects language, domain, subdomain, and specialisation from path or filename.
    """
    import time
    import json
    from . import codes

    imported_count = 0
    skipped_count = 0
    errors = []

    if not os.path.exists(base_dir):
        create_library_folders(base_dir)
        return {"imported": 0, "skipped": 0, "errors": ["Library folder was empty. Created folder structure."]}

    # Map language names to IDs
    lang_map = {}
    for r in c.execute("SELECT id, name FROM languages").fetchall():
        lang_map[r["name"].lower()] = r["id"]

    for root, dirs, files in os.walk(base_dir):
        for f in files:
            if not f.lower().endswith((".txt", ".md")):
                continue
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, base_dir)

            try:
                with open(full_path, "r", encoding="utf-8-sig", errors="replace") as fh:
                    content = fh.read().strip()
                if not content:
                    continue

                lang_name, dom, sub, spec, title = parse_filename_metadata(f, rel_path)
                
                # Determine language_id
                lid = target_lang_id
                if not lid or lid == "all":
                    lid = lang_map.get(lang_name.lower(), 1)

                # Check if script already exists with same title or brief
                dup = c.execute("SELECT id FROM scripts WHERE language_id=? AND (subdomain=? OR code=?) AND brief=?",
                                (lid, sub, title, f)).fetchone()
                if dup:
                    skipped_count += 1
                    continue

                code = codes.new_code()
                seq = c.execute("SELECT IFNULL(MAX(seq), 0) + 1 FROM scripts WHERE language_id=?", (lid,)).fetchone()[0]

                words = len(content.split())
                c.execute("""
                    INSERT INTO scripts(language_id, seq, code, domain, subdomain, specialisation, status, source, wave, brief, words, created_at, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, 'READY', 'folder_import', 1, ?, ?, ?, ?)
                """, (lid, seq, code, dom, sub, spec, f, words, time.time(), time.time()))
                sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

                # Parse dialogue turns
                turns = []
                lines = [l.strip() for l in content.splitlines() if l.strip()]
                for l in lines:
                    m = re.match(r"^([^:]+):\s*(.+)$", l)
                    if m:
                        turns.append({"role": m.group(1).strip(), "text": m.group(2).strip()})
                    else:
                        turns.append({"role": "Speaker", "text": l})

                turn_json = json.dumps(turns)
                c.execute("INSERT INTO script_versions(script_id, version, turns, words, created_at) VALUES(?, 1, ?, ?, ?)",
                          (sid, turn_json, words, time.time()))
                imported_count += 1

            except Exception as ex:
                errors.append(f"{f}: {str(ex)}")

    return {"imported": imported_count, "skipped": skipped_count, "errors": errors}


def get_folder_navigation(c, L, req_domain="", req_sub="", req_spec="", tab="available"):
    """
    Builds breadcrumbs, child folders with live counts, and script filter predicates.
    """
    L_dict = dict(L) if L else {}
    lid = L_dict.get("id", "all")
    lang_name = L_dict.get("name", "All Languages")
    
    # Clean requested parameters
    req_domain = (req_domain or "").strip()
    req_sub = (req_sub or "").strip()
    req_spec = (req_spec or "").strip()

    # Base URL helper
    def make_url(d="", s="", sp=""):
        p = [f"tab={tab}"]
        if lid != "all":
            p.append(f"lang={lid}")
        if d:
            p.append(f"domain={d}")
        if s:
            p.append(f"subdomain={s}")
        if sp:
            p.append(f"spec={sp}")
        return "/admin/scripts?" + "&".join(p)

    # 1. Build Breadcrumbs
    breadcrumbs = [{"name": "📁 All Scripts", "url": make_url()}]
    if lid != "all":
        breadcrumbs.append({"name": f"🗣️ {lang_name}", "url": make_url()})
    if req_domain:
        breadcrumbs.append({"name": f"📂 {req_domain}", "url": make_url(d=req_domain)})
    if req_sub:
        breadcrumbs.append({"name": f"📁 {req_sub}", "url": make_url(d=req_domain, s=req_sub)})
    if req_spec:
        breadcrumbs.append({"name": f"🩺 {req_spec}", "url": make_url(d=req_domain, s=req_sub, sp=req_spec)})

    # 2. Fetch scripts with taxonomy matching for live folder counts
    filter_sql = ""
    args = []
    if lid != "all":
        filter_sql = "WHERE s.language_id = ?"
        args.append(lid)

    all_scripts = c.execute(f"""
        SELECT s.id, s.language_id, s.domain, s.subdomain, s.specialisation, s.status as script_status,
               a.id as aid, a.status as assign_status
        FROM scripts s
        LEFT JOIN assignments a ON a.script_id = s.id AND a.status NOT IN ('RELEASED', 'ABANDONED')
        {filter_sql}
    """, args).fetchall()

    # Helper to count a list of rows
    def count_rows(rows):
        tot = len(rows)
        done = sum(1 for r in rows if r["assign_status"] == "CONFIRMED")
        prog = sum(1 for r in rows if r["assign_status"] in ("ASSIGNED", "IN_SESSION", "DISPUTED"))
        avail = sum(1 for r in rows if r["script_status"] in ("READY", "APPROVED") and not r["assign_status"])
        return tot, avail, prog, done

    subfolders = []
    current_title = ""
    current_quota = None

    # Case A: "All Languages" selected and no domain picked -> show Language folders
    if lid == "all" and not req_domain:
        current_title = "Select a Language Folder"
        langs_db = c.execute("SELECT * FROM languages ORDER BY id").fetchall()
        for lang_item in langs_db:
            l_rows = [r for r in all_scripts if r["language_id"] == lang_item["id"]]
            tot, avail, prog, done = count_rows(l_rows)
            subfolders.append({
                "name": lang_item["name"],
                "url": f"/admin/scripts?tab={tab}&lang={lang_item['id']}",
                "icon": "🗣️",
                "count_total": tot,
                "count_avail": avail,
                "count_prog": prog,
                "count_done": done,
                "quota": 1503,
            })

    # Case B: Language is set, no domain picked -> show Domain folders (Healthcare, BFSI)
    elif not req_domain:
        current_title = f"{lang_name}: Domains"
        for dom_name in ["Healthcare", "BFSI"]:
            dom_rows = [r for r in all_scripts if (match_taxonomy(r["domain"], r["subdomain"], r["specialisation"]) and match_taxonomy(r["domain"], r["subdomain"], r["specialisation"])["domain"] == dom_name)]
            tot, avail, prog, done = count_rows(dom_rows)
            q = 668 if dom_name == "Healthcare" else 835 # 84+17+25+25+8+8+167*3 = 668; 167*5 = 835
            subfolders.append({
                "name": dom_name,
                "url": make_url(d=dom_name),
                "icon": "🏥" if dom_name == "Healthcare" else "🏦",
                "count_total": tot,
                "count_avail": avail,
                "count_prog": prog,
                "count_done": done,
                "quota": q,
            })

    # Case C: Domain is picked, no subdomain picked -> show Sub-domain folders
    elif req_domain and not req_sub:
        current_title = f"{req_domain}: Sub-domains"
        # Get unique subdomains for this domain
        sub_list = [i for i in TAXONOMY_ITEMS if i["domain"].lower() == req_domain.lower()]
        seen_subs = set()
        for item in sub_list:
            s_name = item["subdomain"]
            if s_name in seen_subs:
                continue
            seen_subs.add(s_name)
            sub_rows = [r for r in all_scripts if (match_taxonomy(r["domain"], r["subdomain"], r["specialisation"]) and match_taxonomy(r["domain"], r["subdomain"], r["specialisation"])["subdomain"] == s_name)]
            tot, avail, prog, done = count_rows(sub_rows)
            subfolders.append({
                "name": s_name,
                "url": make_url(d=req_domain, s=s_name),
                "icon": "📁",
                "count_total": tot,
                "count_avail": avail,
                "count_prog": prog,
                "count_done": done,
                "quota": 167,
            })

    # Case D: Sub-domain is Doctor-Patient Consultation, no specialisation picked -> show 6 Doctor specialisations
    elif req_domain.lower() == "healthcare" and "doctor-patient" in req_sub.lower() and not req_spec:
        current_title = "Doctor-Patient Consultation: Specialisations"
        doc_specs = [i for i in TAXONOMY_ITEMS if i["subdomain"] == "Doctor-Patient Consultation"]
        for item in doc_specs:
            sp_name = item["specialisation"]
            sp_rows = [r for r in all_scripts if (match_taxonomy(r["domain"], r["subdomain"], r["specialisation"]) and match_taxonomy(r["domain"], r["subdomain"], r["specialisation"])["specialisation"] == sp_name)]
            tot, avail, prog, done = count_rows(sp_rows)
            subfolders.append({
                "name": sp_name,
                "url": make_url(d=req_domain, s=req_sub, sp=sp_name),
                "icon": "🩺",
                "count_total": tot,
                "count_avail": avail,
                "count_prog": prog,
                "count_done": done,
                "quota": item["quota"],
                "share": item["share"],
            })

    # Case E: Inside leaf folder
    else:
        current_title = req_spec or req_sub or req_domain
        # Find quota
        matched_item = match_taxonomy(req_domain, req_sub, req_spec)
        if matched_item:
            current_quota = matched_item["quota"]

    # Filter matching script IDs for table rendering
    matching_ids = []
    if req_spec:
        matching_ids = [r["id"] for r in all_scripts if (match_taxonomy(r["domain"], r["subdomain"], r["specialisation"]) and match_taxonomy(r["domain"], r["subdomain"], r["specialisation"])["specialisation"].lower() == req_spec.lower())]
    elif req_sub:
        matching_ids = [r["id"] for r in all_scripts if (match_taxonomy(r["domain"], r["subdomain"], r["specialisation"]) and match_taxonomy(r["domain"], r["subdomain"], r["specialisation"])["subdomain"].lower() == req_sub.lower())]
    elif req_domain:
        matching_ids = [r["id"] for r in all_scripts if (match_taxonomy(r["domain"], r["subdomain"], r["specialisation"]) and match_taxonomy(r["domain"], r["subdomain"], r["specialisation"])["domain"].lower() == req_domain.lower())]
    else:
        matching_ids = [r["id"] for r in all_scripts]

    # Calculate active stats for the filtered scripts
    curr_tot = len(matching_ids)
    curr_done = sum(1 for r in all_scripts if r["id"] in matching_ids and r["assign_status"] == "CONFIRMED")
    curr_prog = sum(1 for r in all_scripts if r["id"] in matching_ids and r["assign_status"] in ("ASSIGNED", "IN_SESSION", "DISPUTED"))
    curr_avail = sum(1 for r in all_scripts if r["id"] in matching_ids and r["script_status"] in ("READY", "APPROVED") and not r["assign_status"])

    active_stats = {
        "title": current_title,
        "quota": current_quota,
        "total": curr_tot,
        "avail": curr_avail,
        "prog": curr_prog,
        "done": curr_done,
        "pct": round((curr_done * 100.0) / current_quota, 1) if current_quota else 0.0,
    }

    return {
        "breadcrumbs": breadcrumbs,
        "subfolders": subfolders,
        "matching_ids": matching_ids,
        "active_stats": active_stats,
        "req_domain": req_domain,
        "req_sub": req_sub,
        "req_spec": req_spec,
    }


