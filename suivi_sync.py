#!/usr/bin/env python3
"""
Agent « Suivi Réparation » AUTOP : Odoo -> Firebase (Firestore).

Deux fois par jour (GitHub Actions), il lit dans Odoo, en LECTURE SEULE :
  - les dossiers de la tuile « Nombre de véhicule en Réparation » (Dashboard Manager),
  - pour chacun : véhicule, garage (adhérent), tâche courante, dates de réparation,
  - les heures de main d'oeuvre du devis sinistre validé par l'expert
    (section « MAIN D'OEUVRE », lignes au taux horaire du garage ;
     produits de peinture, ingrédients et forfaits exclus),
et écrit le résultat dans Firestore : suiviReparation/odoo.

Il ne touche jamais aux saisies manuelles (suiviReparation/saisies) : le dashboard
garde la saisie manuelle prioritaire et affiche les écarts.

Variables d'environnement (secrets GitHub) :
  ODOO_URL, ODOO_DB, ODOO_USER, ODOO_API_KEY  (Odoo 13 : ODOO_API_KEY = mot de passe)
  FIREBASE_SERVICE_ACCOUNT                    (contenu JSON du compte de service)
Option : SUIVI_TILE (nom de la tuile Odoo, par défaut « Nombre de véhicule en Réparation »)

Usage :
  python suivi_sync.py            # synchronise
  python suivi_sync.py --dry-run  # affiche le résultat sans rien écrire
"""
import ast
import json
import os
import re
import sys
import xmlrpc.client
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Africa/Tunis")
TILE = os.environ.get("SUIVI_TILE", "").strip() or "Nombre de véhicule en Réparation"
FALLBACK_DOMAIN = [["state", "=", "open"], "|",
                   ["current_task_name", "=like", "Réparation:%"],
                   ["current_task_name", "=", "Fin Réparation"]]
HOURLY_MAX = 100          # un prix unitaire MO au-delà = forfait, pas un taux horaire
PRODUCT_RE = re.compile(r"produit|ingr[eé]d", re.I)


def env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"❌ Variable d'environnement manquante : {name}")
    return v


class Odoo:
    def __init__(self):
        self.url = env("ODOO_URL").rstrip("/")
        self.db = env("ODOO_DB")
        self.key = env("ODOO_API_KEY")
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)
        self.uid = common.authenticate(self.db, env("ODOO_USER"), self.key, {})
        if not self.uid:
            sys.exit("❌ Authentification Odoo refusée (vérifier ODOO_DB / ODOO_USER / ODOO_API_KEY).")
        self.models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)

    def call(self, model, method, args, kw=None):
        kw = dict(kw or {})
        kw.setdefault("context", {"lang": "fr_FR", "tz": "Africa/Tunis", "active_test": False})
        return self.models.execute_kw(self.db, self.uid, self.key, model, method, args, kw)


def tile_domain(odoo):
    """Même filtre que la tuile affichée dans Odoo (Ksolves Dashboard Ninja)."""
    try:
        recs = odoo.call("ks_dashboard_ninja.item", "search_read", [[["name", "=", TILE]]],
                         {"fields": ["name", "ks_domain", "ks_model_name"], "limit": 1})
    except Exception as e:
        print(f"  ! tuiles Odoo illisibles ({e}) : filtre de secours utilisé")
        return FALLBACK_DOMAIN
    if not recs:
        print(f"  ! tuile « {TILE} » introuvable : filtre de secours utilisé")
        return FALLBACK_DOMAIN
    raw = (recs[0].get("ks_domain") or "[]").strip() or "[]"
    raw = re.sub(r"""["']?%UID["']?""", str(odoo.uid), raw)
    dom = ast.literal_eval(raw)
    print(f"  ↳ tuile « {recs[0]['name']} » : filtre {dom}")
    return [list(x) if isinstance(x, (list, tuple)) else x for x in dom]


def local_date(s):
    """Datetime Odoo (UTC, 'AAAA-MM-JJ HH:MM:SS') -> date de Tunis 'AAAA-MM-JJ'."""
    if not s:
        return None
    d = datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return d.astimezone(TZ).strftime("%Y-%m-%d")


def name_of(v):
    return v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else ""


def mo_hours(lines):
    """Heures MO d'un devis : lignes de la section « MAIN D'OEUVRE » au taux horaire dominant."""
    lines = sorted(lines, key=lambda l: (l.get("sequence") or 0, l["id"]))
    mo, in_mo = [], False
    for l in lines:
        if l.get("display_type") == "line_section":
            in_mo = "main" in (l.get("name") or "").lower()
            continue
        if in_mo and not l.get("display_type"):
            mo.append(l)
    weight = {}
    for l in mo:
        if not PRODUCT_RE.search(l.get("name") or ""):
            weight[l["price_unit"]] = weight.get(l["price_unit"], 0) + (l["product_uom_qty"] or 0)
    rate = max(weight.items(), key=lambda kv: kv[1])[0] if weight else 0
    hours, others = 0.0, 0
    for l in mo:
        if rate and abs(l["price_unit"] - rate) < 0.01 and not PRODUCT_RE.search(l.get("name") or ""):
            hours += l["product_uom_qty"] or 0
        else:
            others += 1
    return {"rate": rate, "h": hours, "n": len(mo), "oth": others}


def collect(odoo):
    domain = tile_domain(odoo)
    projects = odoo.call("project.project", "search_read", [domain], {
        "fields": ["ref_interne", "vehicle_id", "member_id", "current_task_name",
                   "date_start_reparation", "date_end_reparation", "date_rdv_reparation"]})
    print(f"  • {len(projects)} dossier(s) en réparation")
    by_id = {p["id"]: p for p in projects}
    orders = odoo.call("sale.order", "search_read", [[
        ["sinister_id", "in", list(by_id)], ["devis_sinister", "=", True],
        ["state", "!=", "cancel"], ["state_sinister", "=", "valide_expert"]]],
        {"fields": ["name", "sinister_id"]}) if by_id else []
    lines = odoo.call("sale.order.line", "search_read", [[["order_id", "in", [o["id"] for o in orders]]]],
                      {"fields": ["order_id", "display_type", "name", "product_uom_qty", "price_unit", "sequence"]}) if orders else []
    lines_by_order = {}
    for l in lines:
        lines_by_order.setdefault(l["order_id"][0], []).append(l)
    devis = {}
    for o in orders:
        devis.setdefault(o["sinister_id"][0], []).append({"so": o["name"], **mo_hours(lines_by_order.get(o["id"], []))})

    rows = []
    for p in projects:
        ds = p.get("ref_interne") or ""
        if not ds:
            continue
        rs = devis.get(p["id"], [])
        hourly = [r for r in rs if 0 < r["rate"] <= HOURLY_MAX]
        rows.append({
            "ds": ds,
            "veh": name_of(p.get("vehicle_id")),
            "garage": name_of(p.get("member_id")).strip(),
            "task": p.get("current_task_name") or "",
            "st": local_date(p.get("date_start_reparation")),
            "en": local_date(p.get("date_end_reparation")),
            "rdv": local_date(p.get("date_rdv_reparation")),
            "h": round(sum(r["h"] for r in hourly), 2) if hourly else None,
            "t": hourly[0]["rate"] if hourly else None,
            "so": "+".join(r["so"] for r in rs),
            "n": sum(r["n"] for r in rs),
            "f": "F" if any(r["rate"] > HOURLY_MAX for r in rs) else ("A" if any(r["oth"] for r in rs) else ""),
        })
    rows.sort(key=lambda r: r["ds"])
    return rows


def summary(old_rows, rows):
    old = {r["ds"]: r for r in old_rows or []}
    new = {r["ds"]: r for r in rows}
    out = []
    added = sorted(set(new) - set(old))
    gone = sorted(set(old) - set(new))
    if added:
        out.append(f"  + {len(added)} entré(s) en réparation : {', '.join(added)}")
    if gone:
        out.append(f"  - {len(gone)} sorti(s) de la liste : {', '.join(gone)}")
    for ds in sorted(set(new) & set(old)):
        a, b = old[ds], new[ds]
        if a.get("task") != b.get("task"):
            out.append(f"  ~ {ds} : {a.get('task')} → {b.get('task')}")
        if a.get("h") != b.get("h"):
            out.append(f"  ~ {ds} : heures devis {a.get('h')} → {b.get('h')}")
        if a.get("st") != b.get("st"):
            out.append(f"  ~ {ds} : début réparation {a.get('st')} → {b.get('st')}")
    return out


def main():
    dry = "--dry-run" in sys.argv
    odoo = Odoo()
    print(f"✅ Connecté à Odoo {odoo.url} (base {odoo.db}, uid {odoo.uid})")
    rows = collect(odoo)
    sans = [r["ds"] for r in rows if r["h"] is None]
    print(f"  • {len(rows) - len(sans)} avec heures de devis, {len(sans)} sans heures (forfait ou pas de MO)")
    if dry:
        for r in rows[:10]:
            print("   ", json.dumps(r, ensure_ascii=False))
        return

    import firebase_admin
    from firebase_admin import credentials, firestore
    sa_file = os.environ.get("FIREBASE_SERVICE_ACCOUNT_FILE", "").strip()
    sa = json.load(open(sa_file, encoding="utf-8")) if sa_file else json.loads(env("FIREBASE_SERVICE_ACCOUNT"))
    firebase_admin.initialize_app(credentials.Certificate(sa))
    db = firestore.client()
    ref = db.collection("suiviReparation").document("odoo")
    snap = ref.get()
    old_rows = (snap.to_dict() or {}).get("rows", []) if snap.exists else []
    changes = summary(old_rows, rows)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not changes and snap.exists:
        ref.update({"checkedAt": now})
        print("  = aucune modification dans Odoo")
        return
    ref.set({"at": now, "source": f"Odoo {odoo.url} — tuile « {TILE} »", "count": len(rows), "rows": rows})
    print(f"  ✓ {len(rows)} dossier(s) écrit(s) dans Firebase")
    print("\n".join(changes[:200]) or "  (première synchronisation)")


if __name__ == "__main__":
    main()
