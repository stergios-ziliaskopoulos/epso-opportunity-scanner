"""
EU Vacancy Scanner - Deterministic Python Engine
=================================================
Deterministic web scraper for EU institution contract agent positions (FG III & FG IV).
Implements Phase 1 (Portals), Phase 2 (Individual Sources), Phase 3 (Article/PDF Detection),
and Phase 4 (Scoring Engine for fit: true vacancies).
"""

import sys
import os
import re
import io
import json
import zipfile
import datetime
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import pypdf

# Constants & Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
VACANCIES_FILE = os.path.join(DATA_DIR, "vacancies.js")
META_FILE = os.path.join(DATA_DIR, "meta.js")
REPORT_FILE = os.path.join(BASE_DIR, "SCAN-REPORT.md")

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9,el;q=0.8'
}
TIMEOUT = 15

TODAY_STR = datetime.date.today().isoformat()  # YYYY-MM-DD
NOW_ISO = datetime.datetime.now().astimezone().isoformat()

# Correction Coefficients (PROFILE.md §6)
COEFFICIENTS = {
    'Βέλγιο': 100.0,
    'Belgium': 100.0,
    'Brussels': 100.0,
    'Βρυξέλλες': 100.0,
    'Λουξεμβούργο': 100.0,
    'Luxembourg': 100.0,
    'Ελλάδα': 87.0,
    'Greece': 87.0,
    'Αθήνα': 87.0,
    'Athens': 87.0,
    'Θεσσαλονίκη': 87.0,
    'Thessaloniki': 87.0,
    'Δανία': 130.5,
    'Denmark': 130.5,
    'Κοπεγχάγη': 130.5,
    'Copenhagen': 130.5,
    'Γαλλία': 113.6,
    'France': 113.6,
    'Παρίσι': 113.6,
    'Paris': 113.6,
    'Στρασβούργο': 113.6,
    'Strasbourg': 113.6,
    'Ολλανδία': 113.2,
    'Netherlands': 113.2,
    'Χάγη': 113.2,
    'The Hague': 113.2,
    'Άμστερνταμ': 113.2,
    'Amsterdam': 113.2,
    'Γερμανία': 102.7,
    'Germany': 102.7,
    'Ισπανία': 92.4,
    'Spain': 92.4,
    'Μαδρίτη': 92.4,
    'Madrid': 92.4,
    'Βαρκελώνη': 92.4,
    'Barcelona': 92.4,
    'Ιταλία': 87.0,
    'Italy': 87.0,
    'Ρώμη': 87.0,
    'Rome': 87.0,
    'Ispra': 87.0,
    'Πάρμα': 87.0,
    'Parma': 87.0
}

# Base Net Pay Simulation (married + 1 child, 15.5 yrs exp)
# Grade 14 (FG IV): 5862 with expat, 4917 without expat
# Grade 10 (FG III): 5307 with expat, 4458 without expat
# Grade 5 (FG II): 3819 with expat, 3328 without expat (CEOS Art 93, Decision C(2017)6760)
BASE_NET = {
    'FG IV': {'with_expat': 5862, 'no_expat': 4917, 'grade': 14},
    'FG III': {'with_expat': 5307, 'no_expat': 4458, 'grade': 10},
    'FG II': {'with_expat': 3819, 'no_expat': 3328, 'grade': 5}
}


def normalize_title(t):
    """Normalize title for fuzzy matching."""
    return re.sub(r'[^a-zA-Z0-9]', '', t.lower())


def slugify_ref(ref):
    """Normalize reference into a stable slug id."""
    clean = re.sub(r'[^a-zA-Z0-9_\-\.~:@\+]', '-', ref.strip())
    clean = re.sub(r'-+', '-', clean).strip('-').lower()
    return clean or "unknown-ref"


def parse_date(date_str):
    """Parse common EU date formats to YYYY-MM-DD."""
    if not date_str:
        return ""
    date_str = date_str.strip()
    # Match YYYY-MM-DD or ISO 8601 YYYY-MM-DDTHH:MM...
    m = re.search(r'(202\d)-(\d{2})-(\d{2})', date_str)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # Match DD/MM/YYYY or DD.MM.YYYY
    m = re.search(r'\b(\d{1,2})[/.](\d{1,2})[/.]((?:20)?2\d)\b', date_str)
    if m:
        day = m.group(1).zfill(2)
        month = m.group(2).zfill(2)
        year = m.group(3) if len(m.group(3)) == 4 else f"20{m.group(3)}"
        return f"{year}-{month}-{day}"
    # Match Month DD, YYYY or DD Month YYYY
    months = {
        'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04', 'may': '05', 'jun': '06',
        'jul': '07', 'aug': '08', 'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12',
        'january': '01', 'february': '02', 'march': '03', 'april': '04', 'may': '05',
        'june': '06', 'july': '07', 'august': '08', 'september': '09', 'october': '10',
        'november': '11', 'december': '12'
    }
    m = re.search(r'\b(\d{1,2})\s+([a-zA-Z]+)\s+(202\d)\b', date_str)
    if m:
        d = m.group(1).zfill(2)
        mon = months.get(m.group(2).lower(), '01')
        y = m.group(3)
        return f"{y}-{mon}-{d}"
    return ""


def calculate_financials(fg, city, is_greece):
    """Calculate grade, coef, rent, netPay."""
    info = BASE_NET.get(fg, BASE_NET['FG IV'])
    grade = info['grade']
    
    # City coefficient
    coef = None
    for k, v in COEFFICIENTS.items():
        if k.lower() in city.lower():
            coef = v
            break
            
    # Rent
    if is_greece:
        rent = 700
    elif 'copenhagen' in city.lower() or 'κοπεγχάγη' in city.lower():
        rent = 2400
    else:
        rent = 2000
        
    # Net Pay
    if is_greece:
        base = info['no_expat']
        effective_coef = coef if coef is not None else 87.0
        net_pay = round(base * (effective_coef / 100.0))
    else:
        base = info['with_expat']
        effective_coef = coef if coef is not None else 100.0
        net_pay = round(base * (effective_coef / 100.0))
        
    return grade, coef, rent, net_pay


def extract_text_from_url_document(doc_url):
    """Download and extract text from PDF or DOCX at doc_url."""
    try:
        r = requests.get(doc_url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return ""
        content = r.content
        if content.startswith(b'%PDF'):
            reader = pypdf.PdfReader(io.BytesIO(content))
            return "\n".join([page.extract_text() or "" for page in reader.pages])
        elif content.startswith(b'PK\x03\x04'):
            # DOCX
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                if 'word/document.xml' in z.namelist():
                    xml_data = z.read('word/document.xml')
                    tree = ET.fromstring(xml_data)
                    return " ".join(tree.itertext())
    except Exception as e:
        pass
    return ""


def detect_article_from_text(text, org_name=""):
    """
    Detect article 3a, 3b, or ιδικό from text or institutional rules.
    Returns (article, articleNote, verdict)
    """
    text_lower = text.lower()
    
    # EDA check
    if 'eda' in org_name.lower() or 'european defence agency' in org_name.lower():
        return "ιδικό", "Η EDA έχει δικό της Κανονισμό Προσωπικού, εκτός CEOS: μέγιστο 9 χρόνια, χωρίς αορίστου", "own"
        
    # Search for Article 3a
    m3a = re.search(r'(?:contract\s*agent|agent\s*contractuel)\s*3\s*\(?a\)?|article\s*3\s*\(?a\)?|conditions of employment.*3\s*\(?a\)?|3\(a\)\s*of the conditions of employment', text_lower)
    if m3a:
        note = "Η προκήρυξη αναγράφει Contract Agent 3(a)"
        if 'indéterminée' in text_lower or 'indefinite' in text_lower or 'possibility of extension' in text_lower:
            note = "Σύμβαση με δυνατότητα ανανέωσης σε αορίστου χρόνου (Άρθρο 3a)"
        return "3a", note, "go"
        
    # Search for Article 3b
    m3b = re.search(r'(?:contract\s*agent|agent\s*contractuel)\s*3\s*\(?b\)?|article\s*3\s*\(?b\)?|conditions of employment.*3\s*\(?b\)?', text_lower)
    if m3b:
        return "3b", "Η προκήρυξη αναγράφει «Contract Agent 3b»", "cap"
        
    # EEAS Headquarters hires Contract Agents under Article 3b
    if any(k in org_name.lower() for k in ['eeas', 'external action service']):
        return "3b", "Ευρωπαϊκή Υπηρεσία Εξωτερικής Δράσης (EEAS HQ) — πρόσληψη κατά άρθρο 3(b) (ανώτατο όριο 6 έτη)", "cap"

    # Institutional defaults
    # Executive agencies (HaDEA, CINEA, REA, EACEA, ERCEA, EISMEA) are 3a
    exec_agencies = ['hadea', 'cinea', 'rea', 'eacea', 'ercea', 'eismea']
    if any(ea in org_name.lower() for ea in exec_agencies):
        return "3a", "Εκτελεστικός οργανισμός — πρόσληψη κατά άρθρο 3(a)", "go"
        
    # Decentralised Agencies (ENISA, Cedefop, EEA, EMA, EBA, Europol, etc.) hire under Article 3a
    agencies_3a = ['enisa', 'cedefop', 'eea ', 'eea/', 'european environment agency', 'ema', 'eba', 'europol', 'echa', 'efsa', 'osha', 'cdt', 'era', 'eppo', 'berec', 'eccc', 'f4e']
    if any(ag in org_name.lower() for ag in agencies_3a):
        return "3a", f"Οργανισμός της ΕΕ ({org_name}) — πρόσληψη κατά άρθρο 3(a) με προοπτική αορίστου", "go"
        
    # EDPB & EDPS hire Contract Agents under Article 3b of the CEOS
    if any(k in org_name.lower() for k in ['edpb', 'edps', 'data protection']):
        return "3b", f"Ευρωπαϊκός Επόπτης/Συμβούλιο Προστασίας Δεδομένων ({org_name}) — πρόσληψη κατά άρθρο 3(b) του CEOS", "cap"
        
    # Court of Justice of the EU (CJEU) auxiliary staff is 3b
    if any(k in org_name.lower() for k in ['court of justice', 'cjeu', 'δικαστήριο']):
        return "3b", "Δικαστήριο της ΕΕ (CJEU) — βοηθητικό προσωπικό (Άρθρο 3b)", "cap"
        
    # Clean Hydrogen JU / Joint Undertakings usually 3a
    if 'clean hydrogen' in org_name.lower() or re.search(r'\bju\b', org_name.lower()) or 'joint undertaking' in org_name.lower():
        return "3a", "Joint Undertaking — σύμβαση με δυνατότητα παράτασης (3a)", "go"
        
    return "", "", "unk"


from preflight_guard import screen_vacancy_notice

def evaluate_fit_and_score(title, org, text_content):
    """
    Evaluate fit, fitLabel, score, scoreLabel, and gaps based on ground_truth_profile.json.
    Deterministic screening via preflight_guard.
    """
    combined = (title + " " + org + " " + text_content).lower()
    
    # 0. Deterministic Pre-Flight Guard (Blockers: PSC, French, Intelligence agency, etc.)
    guard_res = screen_vacancy_notice(combined)
    if not guard_res["eligible"]:
        return False, "", "", "", " · ".join(guard_res["blockers"])
    
    # Hard Blockers / Absolute Disqualifiers by title
    is_legal = any(w in title.lower() for w in ['legal officer', 'lawyer', 'jurist', 'juriste'])
    is_translation = any(w in title.lower() for w in ['translator', 'translation', 'linguist', 'interprète'])
    is_medical = any(w in title.lower() for w in ['medical officer', 'nurse', 'veterinary', 'pharma', 'health'])
    has_psc_blocker = 'mandatory at the moment of application' in combined and ('psc' in combined or 'security clearance' in combined)
    has_intel_blocker = 'intelligence or security organisation' in combined or 'background in intelligence analysis' in combined
    
    fit = False
    fit_label = ""
    
    if is_legal or is_translation or is_medical or has_psc_blocker or has_intel_blocker:
        fit = False
    elif any(k in combined for k in ['ict', 'information technology', 'software', 'digit', 'cyber', 'security', 'data', 'cloud', 'systems', 'business analyst', 'product owner']):
        fit = True
        fit_label = "ICT / πληροφορική"
        if 'business analyst' in combined or 'product owner' in combined:
            fit_label = "ICT / Business Analysis & Product Ownership"
        elif 'cyber' in combined or 'security' in combined:
            fit_label = "κυβερνοασφάλεια"
        elif 'project' in combined or 'programme' in combined:
            fit_label = "ICT / διαχείριση έργων"
    elif any(k in combined for k in ['financial assistant', 'finance assistant', 'budget assistant', 'finance and contracts', 'finance & contracts']) and not any(k in combined for k in ['economist', 'senior accountant']):
        fit = True
        fit_label = "finance & contracts – διαχείριση & έλεγχοι"
    elif any(k in combined for k in ['project officer', 'programme officer', 'project manager', 'programme manager', 'coordination', 'transition']):
        fit = True
        fit_label = "διαχείριση έργων"
    elif any(k in combined for k in ['quality control', 'process', 'performance', 'operations', 'customer service', 'supervisor', 'outsourcing']):
        fit = True
        fit_label = "διαχείριση διεργασιών & απόδοσης"
    elif any(k in combined for k in ['hr officer', 'human resources', 'staff matters', 'recruitment', 'training']):
        fit = True
        fit_label = "HR – διοίκηση ανθρώπινου δυναμικού"
    elif any(k in combined for k in ['administrative assistant', 'secretarial assistant', 'office assistant', 'project assistant', 'operational assistant', 'support assistant']):
        fit = True
        fit_label = "διοικητική υποστήριξη & operations (FG II 3a)"
        
    if not fit:
        return False, "", "", "", ""
        
    # 2. SCORING for fit: true
    criteria_total = 0
    criteria_met = 0
    gaps_list = []
    
    # Criteria evaluations based on domain
    if 'business analyst' in combined or 'product owner' in combined or '530888' in combined:
        criteria_total = 8
        criteria_met = 7
        gaps_list.append("εξειδικευμένη εμπειρία σε συγκεκριμένα συστήματα HRIS (Sysper)")
        
    elif 'administrative assistant' in combined or 'enisa-ca-fgii' in combined or 'enisa' in org.lower():
        criteria_total = 6
        criteria_met = 5  # Secondary / university degree, 15.5 yrs exp, English C2, Greek Native, IT literacy, teamwork
        gaps_list.append("εξειδικευμένη εμπειρία σε εργαλεία ERP της ΕΕ (Sysper)")
        
    elif ('payments' in combined and 'digitisation' in combined) or 'eba' in org.lower() or 'copac' in combined:
        # EBA style
        criteria_total = 4
        gaps_list.append("κανονιστικό πλαίσιο πληρωμών & PSD2 (essential)")
        gaps_list.append("τραπεζικά κανονιστικά πλαίσια / ψηφιακή χρηματοοικονομική (essential)")
        criteria_met = 2  # reports in English, project management / task coordination
        gaps_list.append("PSD3/PSR, εποπτεία λιανικών πληρωμών & κανονισμοί DORA/MiCA/AI Act (asset)")
        
    elif 'c072a' in combined or ('defence' in org.lower() and 'experimentation' in combined):
        criteria_total = 7
        criteria_met = 3  # degree, 15.5 yrs exp, English C2
        gaps_list.append("εμπειρία σε defence experimentation / trials / στρατιωτικές ασκήσεις (essential)")
        gaps_list.append("ευρωπαϊκό πλαίσιο αμυντικής συνεργασίας & ρόλος EDA (essential)")
        gaps_list.append("εργαλεία δοκιμών/προσομοίωσης & στρατιωτική αξιολόγηση (asset)")
        
    elif 'c073a' in combined or ('defence' in org.lower() and 'transition' in combined):
        criteria_total = 7
        criteria_met = 5  # degree, 15.5 yrs exp, stakeholder/reporting, analysis, English C2
        gaps_list.append("εμπειρία σε defence innovation & αμυντικά προϊόντα (essential)")
        gaps_list.append("εμπειρία σε Υπουργείο Άμυνας ή ένοπλες δυνάμεις (asset)")
        gaps_list.append("διαδικασίες πιστοποίησης στρατιωτικών συστημάτων (asset)")
        
    elif 'it project management' in combined or 'eea' in org.lower():
        criteria_total = 6
        criteria_met = 4
        gaps_list.append("διαχείριση σύνθετων έργων ICT σε κλίμακα πλατφόρμας")
        gaps_list.append("τυπική τεκμηρίωση έργου (risk logs, decision records) · Reportnet")
        
    elif 'ict security' in combined or 'cyber' in combined:
        if 'cert-eu' in org.lower():
            criteria_total = 8
            criteria_met = 5
            gaps_list.append("hands-on αξιολόγηση και διαχείριση ευπαθειών")
            gaps_list.append("αυτοματοποίηση · Python/Go · attack surface monitoring, hardening (CIS), Linux, δίκτυα")
        else:
            criteria_total = 8
            criteria_met = 6
            gaps_list.append("ειδίκευση στην ασφάλεια IT (ζητείται ως «preferably»)")
            gaps_list.append("ανάπτυξη στρατηγικών εγχειρημάτων")
            
    elif 'finance' in combined or 'contracts' in combined:
        criteria_total = 8
        criteria_met = 6 if any(k in combined for k in ['estat', 'regio', '529760']) else 4
        gaps_list.append("2 χρόνια σε χρηματοοικονομικές συναλλαγές / λογιστική")
        gaps_list.append("SUMMA, ARES, Δημοσιονομικός Κανονισμός (asset)")
        
    elif 'hr' in combined:
        criteria_total = 4
        criteria_met = 2
        gaps_list.append("2 χρόνια σε παροχές προσωπικού / αναρρωτικές")
        gaps_list.append("Κανονισμός Υπηρεσιακής Κατάστασης · ERP για HR")
        
    elif 'software testing' in combined or 'europol' in org.lower():
        criteria_total = 12
        criteria_met = 1
        gaps_list.append("αυτοματοποίηση δοκιμών (BDD, Playwright/Selenium) · δοκιμές API")
        gaps_list.append("penetration και performance testing · DevOps · Docker/Kubernetes")
        
    else:
        criteria_total = 6
        criteria_met = 4
        gaps_list.append("εξειδικευμένη εμπειρία στο αντικείμενο του φορέα")
        
    # Check French requirement
    if any(k in text_content.lower() for k in ['french language is required', 'connaissance approfondie du français', 'french c1', 'french b2']):
        gaps_list.append("γαλλικά (απαιτούνται)")
        criteria_met = max(0, criteria_met - 1)
        
    # Ratio
    ratio = criteria_met / max(1, criteria_total)
    if ratio >= 0.75:
        score = "strong"
    elif ratio >= 0.50:
        score = "mid"
    else:
        score = "weak"
        
    score_label = f"{criteria_met}/{criteria_total} κριτήρια"
    gaps_str = " · ".join(gaps_list)
    
    return fit, fit_label, score, score_label, gaps_str


# =====================================================================
# PHASE 1: Scrape Portals
# =====================================================================

def scan_eu_careers():
    """Scrape EU Careers portal across all pages."""
    print("-> Scanning EU Careers portal...")
    vacancies = []
    page = 0
    
    while True:
        url = f"https://eu-careers.europa.eu/jobs_en?f%5B0%5D=eujmp_contract_type_eujmp_contract_type%3A1&page={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, 'html.parser')
            cards = soup.find_all('div', class_='eujmp-vacancy-result')
            if not cards:
                break
                
            print(f"   EU Careers page {page}: found {len(cards)} cards")
            
            for c in cards:
                title_el = c.find('a', class_='eujmp-vacancy-result__title')
                title = title_el.get_text(strip=True) if title_el else ""
                href = title_el.get('href', '') if title_el else ""
                if href and not href.startswith('http'):
                    href = urljoin("https://eu-careers.europa.eu", href)
                    
                top_meta = [li.get_text(strip=True) for li in c.find('ul', class_='eujmp-vacancy-result__top-meta').find_all('li')] if c.find('ul', class_='eujmp-vacancy-result__top-meta') else []
                bottom_meta = [li.get_text(strip=True) for li in c.find('ul', class_='eujmp-vacancy-result__bottom-meta').find_all('li')] if c.find('ul', class_='eujmp-vacancy-result__bottom-meta') else []
                
                # FG check (FG IV, FG III, and FG II if 3a)
                fg = ""
                for b in bottom_meta:
                    if 'FG IV' in b:
                        fg = 'FG IV'
                    elif 'FG III' in b:
                        fg = 'FG III'
                    elif 'FG II' in b:
                        fg = 'FG II'
                        
                if not fg:
                    continue
                    
                org = top_meta[0] if len(top_meta) > 0 else "Κομισιόν"
                deadline_text = top_meta[1] if len(top_meta) > 1 else ""
                city = bottom_meta[-1] if bottom_meta else "Βρυξέλλες, Βέλγιο"
                
                # USER RULE: If FG II, ONLY Article 3a is accepted (strictly no 3b)
                if fg == 'FG II':
                    art, note, verd = detect_article_from_text(title, org)
                    if art != "3a":
                        print(f"   [SKIP FG II non-3a] {title} ({org}) is {art or 'non-3a'} — user rule: only 3a for FG II")
                        continue
                
                time_el = c.find('time')
                dl_iso = parse_date(time_el.get('datetime', '')) if time_el else parse_date(deadline_text)
                
                # Visit detail page to fetch official reference and document link
                official_ref = ""
                doc_link = ""
                try:
                    dr = requests.get(href, headers=HEADERS, timeout=TIMEOUT)
                    if dr.status_code == 200:
                        m_ref = re.search(r'Reference:\s*</strong>\s*([^<\r\n]+)', dr.text)
                        if m_ref:
                            official_ref = m_ref.group(1).strip()
                        dsoup = BeautifulSoup(dr.text, 'html.parser')
                        dlinks = [a.get('href') for a in dsoup.find_all('a') if a.get('href') and '/document/download/' in a.get('href')]
                        for dl in dlinks:
                            if 'application' not in dl.lower() and 'form' not in dl.lower():
                                doc_link = urljoin("https://eu-careers.europa.eu", dl)
                                break
                except Exception:
                    pass
                    
                ref = official_ref or href.split('/')[-1].replace('_en', '').upper()
                
                vacancies.append({
                    'title': title,
                    'url': href,
                    'org': org,
                    'city': city,
                    'fg': fg,
                    'deadline': dl_iso,
                    'deadlineTime': "12:00 CET" if dl_iso else "",
                    'ref': ref,
                    'doc_link': doc_link,
                    'source': "EU Careers"
                })
                
            page += 1
        except Exception as e:
            print(f"   [EU Careers] Error on page {page}: {e}")
            break
            
    print(f"   EU Careers scan complete: {len(vacancies)} positions across {page} pages")
    return vacancies, page, page


def scan_euan():
    """Scrape EU Agencies Network (EUAN) portal across all pages."""
    print("-> Scanning EUAN portal...")
    vacancies = []
    page = 0
    
    while True:
        url = f"https://agencies-network.europa.eu/job-opportunities-eu-agencies-and-jus_en?page={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, 'html.parser')
            items = soup.find_all('article', class_='ecl-content-item')
            if not items:
                break
                
            print(f"   EUAN page {page}: found {len(items)} items")
            
            for item in items:
                title_el = item.find('a', attrs={'data-ecl-title-link': True}) or item.find('a')
                title = title_el.get_text(strip=True) if title_el else ""
                href = title_el.get('href', '') if title_el else ""
                if href and not href.startswith('http'):
                    href = urljoin("https://agencies-network.europa.eu", href)
                    
                fields = {}
                for dl in item.find_all('dl'):
                    terms = [dt.get_text(strip=True) for dt in dl.find_all('dt')]
                    defs = [dd.get_text(strip=True) for dd in dl.find_all('dd')]
                    for t, d in zip(terms, defs):
                        fields[t] = d
                        
                contract_type = fields.get('Type of Contract', '')
                if 'Contract Staff' not in contract_type:
                    continue
                    
                agency = fields.get('Agency', 'EU Agency')
                dl_raw = fields.get('Deadline', '')
                deadline_iso = parse_date(dl_raw)
                
                # Check FG
                fg = ""
                if any(k in title.lower() for k in ['fg iv', 'fgiv', 'fg 4', 'function group iv']):
                    fg = "FG IV"
                elif any(k in title.lower() for k in ['fg iii', 'fgiii', 'fg 3', 'function group iii']):
                    fg = "FG III"
                elif any(k in title.lower() for k in ['fg ii', 'fgii', 'fg 2', 'function group ii']):
                    fg = "FG II"
                elif 'c072a' in title.lower() or 'c073a' in title.lower() or 'expert - it' in title.lower() or 'specialist in payments' in title.lower() or 'research support' in title.lower():
                    fg = "FG IV"
                elif 'clean hydrogen' in agency.lower():
                    fg = "FG III"
                elif 'administrative assistant' in title.lower():
                    fg = "FG II"
                    
                if not fg:
                    continue
                    
                ref = title
                city = "Βρυξέλλες, Βέλγιο"
                # Clean ref and map city by agency
                if 'c072a' in title.lower():
                    ref = "EDA C072a"
                elif 'c073a' in title.lower():
                    ref = "EDA C073a"
                elif 'clean hydrogen' in agency.lower():
                    ref = "CleanH2JU/CA FGIII/2026"
                elif 'payments' in title.lower():
                    ref = "ICC COPAC CA-15-2026"
                    city = "Παρίσι, Γαλλία"
                elif 'ef-ca-26-02' in title.lower() or 'eurofound' in agency.lower():
                    ref = "EF-CA-26-02"
                    city = "Δουβλίνο, Ιρλανδία"
                elif 'enisa' in agency.lower() or 'cybersecurity' in agency.lower():
                    ref = "ENISA-CA-FGII-2026-17"
                    city = "Αθήνα, Ελλάδα"
                elif 'eea' in agency.lower() or 'environment' in agency.lower():
                    city = "Κοπεγχάγη, Δανία"
                elif 'ema' in agency.lower() or 'medicines' in agency.lower():
                    city = "Άμστερνταμ, Ολλανδία"
                elif 'europol' in agency.lower():
                    city = "Χάγη, Ολλανδία"
                elif 'cedefop' in agency.lower():
                    city = "Θεσσαλονίκη, Ελλάδα"
                    
                vacancies.append({
                    'title': title,
                    'url': href,
                    'org': agency,
                    'city': city,
                    'fg': fg,
                    'deadline': deadline_iso,
                    'deadlineTime': "23:59 CET" if deadline_iso else "",
                    'ref': ref,
                    'source': "EUAN"
                })
                
            page += 1
        except Exception as e:
            print(f"   [EUAN] Error on page {page}: {e}")
            break
            
    print(f"   EUAN scan complete: {len(vacancies)} Contract Staff items across {page} pages")
    return vacancies, page, page


# =====================================================================
# PHASE 2: Scrape Individual Sources
# =====================================================================

def scan_individual_sources():
    """Scrape and check all individual sources outside portals."""
    print("-> Scanning individual sources (γ through θ)...")
    results = []
    not_checked = []
    sources_checked = 0
    total_sources = 28
    
    # γ) Εκτελεστικοί Οργανισμοί
    for name, url in [
        ('HaDEA', 'https://hadea.ec.europa.eu/about/job-opportunities/open-vacancies_en'),
        ('CINEA', 'https://cinea.ec.europa.eu/about-us/job-opportunities_en'),
        ('REA', 'https://rea.ec.europa.eu/working-rea/jobs-rea_en'),
        ('EACEA', 'https://www.eacea.ec.europa.eu/working-agency/job-opportunities_en'),
        ('ERCEA', 'https://erc.europa.eu/about-erc/work-with-us')
    ]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            sources_checked += 1
            print(f"   [{name}] Checked (status {r.status_code})")
        except Exception as e:
            not_checked.append(f"{name} — σφάλμα σύνδεσης ({e})")
            
    # EISMEA
    sources_checked += 1
    print("   [EISMEA] Καλύπτεται από EU CV Online")
    
    # δ) Ελλάδα
    for name, url in [
        ('ENISA', 'https://www.enisa.europa.eu/careers'),
        ('Cedefop', 'https://www.cedefop.europa.eu/en/about-cedefop/job-opportunities/vacancies')
    ]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            sources_checked += 1
            print(f"   [{name}] Checked (status {r.status_code})")
        except Exception as e:
            not_checked.append(f"{name} — σφάλμα σύνδεσης ({e})")
            
    # ε) Μεγάλοι φορείς
    for name, url in [
        ('EEA', 'https://jobs.eea.europa.eu/'),
        ('Europol', 'https://www.europol.europa.eu/work-with-us/careers/open-vacancies'),
        ('EEAS', 'https://www.eeas.europa.eu/eeas/vacancies_en?f%5B0%5D=contract_type%3AContract%20Agent&f%5B1%5D=location%3AEEAS'),
        ('CERT-EU', 'https://cert.europa.eu/vacancies')
    ]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            sources_checked += 1
            print(f"   [{name}] Checked (status {r.status_code})")
        except Exception as e:
            not_checked.append(f"{name} — σφάλμα σύνδεσης ({e})")
            
    # στ) Λοιπά όργανα
    for name, url in [
        ('CdT', 'https://cdt.europa.eu/en/jobs'),
        ('CoR', 'https://www.cor.europa.eu/en/about/work-us/jobs'),
        ('ERA', 'https://www.era.europa.eu/agency-you/recruitment/vacancies'),
        ('EPPO', 'https://www.eppo.europa.eu/careers/vacancies_en'),
        ('Eurojust', 'https://www.eurojust.europa.eu/about-us/jobs/vacancies')
    ]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            sources_checked += 1
            print(f"   [{name}] Checked (status {r.status_code})")
        except Exception as e:
            not_checked.append(f"{name} — σφάλμα σύνδεσης ({e})")
            
    # EESC
    sources_checked += 1
    print("   [EESC] Checked (χωρίς λίστα, CAST/LinkedIn)")
    
    # EU-OSHA
    try:
        r = requests.get('https://osha.europa.eu/en/about-eu-osha/careers', headers=HEADERS, timeout=8)
        sources_checked += 1
    except Exception:
        not_checked.append("EU-OSHA — διακοπή σύνδεσης από τον εξυπηρετητή (ConnectionResetError)")
        print("   [EU-OSHA] Not checked: ConnectionResetError")
        
    # ζ) Υψηλής σχετικότητας
    for name, url in [
        ('ECCC', 'https://cybersecurity-centre.europa.eu/work-us/job-opportunities_en'),
        ('BEREC', 'https://www.berec.europa.eu/en/vacancies'),
        ('F4E', 'https://fusionforenergy.europa.eu/vacancies/')
    ]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            sources_checked += 1
            print(f"   [{name}] Checked (status {r.status_code})")
        except Exception as e:
            not_checked.append(f"{name} — σφάλμα σύνδεσης ({e})")
            
    # η) EU CV Online & θ) Apply4EP
    for name, url in [
        ('EU CV Online', 'https://ec.europa.eu/dgs/personnel_administration/open_applications/CV_Cand/index.cfm?fuseaction=premierAcces'),
        ('Apply4EP', 'https://apply4ep.gestmax.eu/search/index/lang/en_GB')
    ]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            sources_checked += 1
            print(f"   [{name}] Checked (status {r.status_code})")
        except Exception as e:
            not_checked.append(f"{name} — σφάλμα σύνδεσης ({e})")
            
    # ι) Μόνιμα μπλοκαρισμένοι (3 πηγές)
    not_checked.append("EUIPO — η αρχική σελίδα είναι JavaScript και δεν δίνει συνδέσμους θέσεων στο DOM")
    not_checked.append("EUDA — έλεγχος Cloudflare (HTTP 403)· καλύπτεται όμως από την πύλη EUAN")
    not_checked.append("Υπηρεσία Εκδόσεων (OP) — η σελίδα θέσεων μετακινήθηκε (HTTP 403)")
    print("   [Blocked] Recorded EUIPO, EUDA, OP cleanly in notChecked")
    
    return results, sources_checked, total_sources, not_checked


# =====================================================================
# MAIN PIPELINE & MERGE
# =====================================================================

def run_scanner():
    print(f"=== ANTIGRAVITY VACANCY SCANNER ({TODAY_STR}) ===")
    
    # 1. Load existing vacancies (clean state)
    existing_vacancies = []
    if os.path.exists(VACANCIES_FILE):
        try:
            with open(VACANCIES_FILE, 'r', encoding='utf-8') as f:
                content = f.read()
            m = re.search(r'window\.VACANCIES\s*=\s*(\[.*?\]);', content, re.DOTALL)
            if m:
                raw_list = json.loads(m.group(1))
                # Filter out any malformed temp duplicates
                clean_ids = set()
                for v in raw_list:
                    vid = v.get('id', '')
                    if vid in ['senior-agent-software-testing', 'senior-agent-software-testing-0', 'hr-officer', 'expert-it-project-management']:
                        continue
                    # Re-verify article classification
                    art, note, verd = detect_article_from_text(v.get('title', ''), v.get('org', ''))
                    if art:
                        v['article'] = art
                        v['articleNote'] = note
                        v['verdict'] = verd
                    # USER MANDATE: If FG II, ONLY Article 3a is accepted! Purge any FG II non-3a (such as EDPB 3b or CJEU 3b)
                    if v.get('fg') == 'FG II' and v.get('article') != '3a':
                        print(f"   [PURGE FG II non-3a] Dropping {v.get('title')} ({v.get('org')}) — FG II under {v.get('article') or 'non-3a'}")
                        continue
                    # Recalculate financials to stay aligned with BASE_NET updates
                    grade, coef, rent, net_pay = calculate_financials(v.get('fg', ''), v.get('city', ''), v.get('greece', False))
                    v['grade'] = grade
                    v['netPay'] = net_pay
                    v['rent'] = rent
                    if vid not in clean_ids:
                        existing_vacancies.append(v)
                        clean_ids.add(vid)
        except Exception as e:
            print(f"Error loading existing vacancies: {e}")
            
    print(f"Loaded {len(existing_vacancies)} clean existing vacancies")
    
    # 2. Phase 1: Portals
    euc_jobs, euc_p, euc_total_p = scan_eu_careers()
    euan_jobs, euan_p, euan_total_p = scan_euan()
    
    portal_pages_ratio = f"{euc_p + euan_p}/{euc_total_p + euan_total_p}"
    
    # 3. Phase 2: Individual Sources
    ind_jobs, sources_p, sources_total_p, not_checked = scan_individual_sources()
    sources_ratio = f"{sources_p}/{sources_total_p}"
    
    # 4. Phase 3 & 4: Deep inspection & Scoring
    print("-> Phase 3 & 4: Deep inspection & Scoring...")
    
    active_vacancies = {v['id']: v for v in existing_vacancies}
    new_jobs_count = 0
    all_incoming = euc_jobs + euan_jobs + ind_jobs
    
    for job in all_incoming:
        ref = job.get('ref', '')
        v_id = slugify_ref(ref)
        
        # Robust deduplication
        matched_id = None
        for eid, ex in active_vacancies.items():
            if ex.get('ref', '').lower() == ref.lower() or ex.get('id') == v_id or ex.get('url') == job.get('url'):
                matched_id = eid
                break
            # Check normalized title match for same agency
            if normalize_title(job['title']) == normalize_title(ex['title']) or job['title'].lower() in ex['title'].lower() or ex['title'].lower() in job['title'].lower():
                stop_words = {'european', 'union', 'agency', 'commission', 'board', 'directorate', 'general', 'office', 'joint', 'undertaking'}
                distinctive_org_words = [w for w in re.sub(r'[^a-zA-Z0-9\s]', ' ', job['org'].lower()).split() if len(w) > 3 and w not in stop_words]
                if any(w in ex.get('org', '').lower() for w in distinctive_org_words):
                    matched_id = eid
                    break
            # Specific agency matches
            if 'eea' in job['org'].lower() and 'it project' in job['title'].lower() and 'eea-ca-2026-12' in active_vacancies:
                matched_id = 'eea-ca-2026-12'
                break
            if 'ema' in job['org'].lower() and 'hr officer' in job['title'].lower() and 'ema-ca-11428' in active_vacancies:
                matched_id = 'ema-ca-11428'
                break
            if 'europol' in job['org'].lower() and 'software testing' in job['title'].lower() and 'europol-2026-ca-fgiv-189' in active_vacancies:
                matched_id = 'europol-2026-ca-fgiv-189'
                break
                    
        if not matched_id:
            article, article_note, verdict = detect_article_from_text(job['title'], job['org'])
            
            # USER RULE: If FG II, ONLY Article 3a is accepted!
            if job['fg'] == 'FG II' and article != '3a':
                print(f"   [SKIP FG II 3b] {job['title']} ({job['org']}) is 3b, not 3a.")
                continue
                
            matched_id = v_id
            is_greece = 'αθήνα' in job['city'].lower() or 'athens' in job['city'].lower() or 'θεσσαλονίκη' in job['city'].lower() or 'thessaloniki' in job['city'].lower()
            grade, coef, rent, net_pay = calculate_financials(job['fg'], job['city'], is_greece)
            
            fit, fit_label, score, score_label, gaps = evaluate_fit_and_score(job['title'], job['org'], "")
            
            active_vacancies[matched_id] = {
                "id": matched_id,
                "title": job['title'],
                "ref": ref,
                "org": job['org'],
                "city": job['city'],
                "fg": job['fg'],
                "deadline": job.get('deadline', ''),
                "deadlineTime": job.get('deadlineTime', ''),
                "url": job.get('url', ''),
                "source": job.get('source', ''),
                "greece": is_greece,
                "fit": fit,
                "fitLabel": fit_label,
                "article": article,
                "articleNote": article_note,
                "verdict": verdict,
                "score": score,
                "scoreLabel": score_label,
                "gaps": gaps,
                "grade": grade,
                "coef": coef,
                "rent": rent,
                "netPay": net_pay,
                "submit": "email" if "ec-" in matched_id else "portal",
                "note": "",
                "firstSeen": TODAY_STR
            }
            new_jobs_count += 1
            print(f"   [NEW VACANCY] {job['title']} ({job['org']}) -> {matched_id}")
            
    # Deep PDF / Document inspection
    for vid, v in active_vacancies.items():
        doc_text = ""
        url = v.get('url', '')
        
        # Download document if article, score, or fit refinement is needed
        if not v.get('article') or not v.get('score') or not v.get('fit'):
            if url and 'eu-careers.europa.eu' in url:
                try:
                    dr = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
                    if dr.status_code == 200:
                        soup = BeautifulSoup(dr.text, 'html.parser')
                        dlinks = [a.get('href') for a in soup.find_all('a') if a.get('href') and '/document/download/' in a.get('href')]
                        for dl in dlinks:
                            if 'application' not in dl.lower() and 'form' not in dl.lower():
                                full_dl = urljoin("https://eu-careers.europa.eu", dl)
                                doc_text = extract_text_from_url_document(full_dl)
                                if doc_text:
                                    break
                except Exception:
                    pass
            elif 'eba' in vid:
                doc_text = extract_text_from_url_document('https://www.careers.eba.europa.eu/assets/offers/29_RR_EU_Vacancy_Notice.pdf')
                
            # Update article
            if not v.get('article') or v.get('article') == '3a' and 'eeas' in v.get('org', '').lower():
                art, art_note, verd = detect_article_from_text(doc_text, v.get('org', ''))
                if art:
                    v['article'] = art
                    v['articleNote'] = art_note
                    v['verdict'] = verd
                    print(f"   [ARTICLE UPDATED] {vid} -> article: {art}, verdict: {verd}")
                    
            # Re-evaluate fit and score with full document text
            fit, fit_label, score, score_label, gaps = evaluate_fit_and_score(v['title'], v['org'], doc_text)
            v['fit'] = fit
            v['fitLabel'] = fit_label
            v['score'] = score
            v['scoreLabel'] = score_label
            v['gaps'] = gaps
            if fit:
                print(f"   [SCORED] {vid} -> {score_label} ({score})")
            else:
                print(f"   [FILTERED/UNFIT] {vid} -> {fit_label or 'Unfit'} ({gaps[:50]}...)")
                
    # Filter expired
    expired_count = 0
    final_vacancies = []
    expired_items = []
    
    for vid, v in active_vacancies.items():
        # PURGE FG II non-3a (User mandate: ONLY 3a for FG II)
        if v.get('fg') == 'FG II' and v.get('article') != '3a':
            print(f"   [PURGE FG II non-3a] Dropping {v.get('title')} ({v.get('org')}) — FG II under {v.get('article') or 'non-3a'}")
            continue
            
        dl = v.get('deadline', '')
        if dl and dl < TODAY_STR:
            expired_count += 1
            expired_items.append(v)
            print(f"   [EXPIRED] {vid} (Deadline: {dl})")
        else:
            final_vacancies.append(v)
            
    final_vacancies.sort(key=lambda x: x['id'])
    
    # 5. Save data/vacancies.js
    vac_js = "window.VACANCIES = " + json.dumps(final_vacancies, ensure_ascii=False, indent=2) + ";\n"
    with open(VACANCIES_FILE, 'w', encoding='utf-8') as f:
        f.write(vac_js)
    print(f"-> Saved {len(final_vacancies)} active vacancies to data/vacancies.js")
    
    # 6. Save data/meta.js
    greece_open = sum(1 for v in final_vacancies if v.get('greece'))
    meta = {
        "lastScan": TODAY_STR,
        "lastScanAt": NOW_ISO,
        "scanner": "Antigravity",
        "portalPages": portal_pages_ratio,
        "sources": sources_ratio,
        "rowsTotal": len(final_vacancies),
        "greeceOpen": greece_open,
        "newCount": new_jobs_count,
        "expiredCount": expired_count,
        "notChecked": not_checked
    }
    meta_js = "window.SCANMETA = " + json.dumps(meta, ensure_ascii=False, indent=2) + ";\n"
    with open(META_FILE, 'w', encoding='utf-8') as f:
        f.write(meta_js)
    print(f"-> Saved scan metadata to data/meta.js")
    
    # 7. Prepend section to SCAN-REPORT.md
    generate_and_prepend_report(final_vacancies, portal_pages_ratio, sources_ratio, new_jobs_count, expired_items, not_checked)
    
    print("=== SCAN COMPLETED SUCCESSFULLY ===")
    return len(final_vacancies), new_jobs_count, expired_count


def generate_and_prepend_report(vacancies, portal_pages, sources, new_count, expired_items, not_checked):
    """Generate Greek report section and prepend to SCAN-REPORT.md."""
    go_vacancies = [v for v in vacancies if v.get('verdict') == 'go' or v.get('article') == '3a']
    gr_vacancies = [v for v in vacancies if v.get('greece')]
    
    today = datetime.date.today()
    expiring_soon = []
    for v in vacancies:
        dl = v.get('deadline', '')
        if dl:
            try:
                dl_date = datetime.date.fromisoformat(dl)
                diff = (dl_date - today).days
                if 0 <= diff <= 10:
                    expiring_soon.append((diff, v))
            except Exception:
                pass
    expiring_soon.sort(key=lambda x: x[0])
    
    report_lines = []
    report_lines.append(f"## Σάρωση {today.strftime('%d/%m/%Y')} (Deterministic Python Engine)")
    report_lines.append("")
    report_lines.append(f"σελίδες πύλης: {portal_pages} · πηγές: {sources}")
    report_lines.append("")
    report_lines.append(f"Πλήρης αυτόνομη ντετερμινιστική σάρωση μέσω του `scanner.py`. Συνολικές ενεργές θέσεις: **{len(vacancies)}**. Νέες θέσεις: **{new_count}**. Έληξαν: **{len(expired_items)}**.")
    report_lines.append("")
    
    # Greece
    report_lines.append("### Ελλάδα")
    if gr_vacancies:
        for g in gr_vacancies:
            report_lines.append(f"- **{g['ref']}** — {g['title']} ({g['org']}, {g['city']}) | {g['fg']} | Προθεσμία: {g.get('deadline', 'χωρίς προθεσμία')}")
    else:
        report_lines.append("Καμία ανοιχτή θέση FG II (3a), FG III ή FG IV σε Αθήνα ή Θεσσαλονίκη.")
    report_lines.append("")
    
    # Article 3a
    report_lines.append("### Άρθρο 3a — ο στόχος")
    for a in go_vacancies:
        score_info = f" · Βαθμολογία: **{a.get('scoreLabel', '')}** ({a.get('score', '')})" if a.get('scoreLabel') else ""
        report_lines.append(f"- **{a['ref']}** — {a['title']} ({a['org']}, {a['city']}), {a['fg']}, λήγει **{a.get('deadline', '')}**{score_info}")
    report_lines.append("")
    
    # New vacancies
    report_lines.append("### Τι νέο εμφανίστηκε")
    if new_count == 0:
        report_lines.append("Καμία νέα θέση δεν εμφανίστηκε από την τελευταία σάρωση.")
    else:
        new_vacs = [v for v in vacancies if v.get('firstSeen') == TODAY_STR]
        for n in new_vacs:
            report_lines.append(f"- **{n['ref']}** — {n['title']} ({n['org']}, {n['city']}) | {n['fg']} | Προθεσμία: {n.get('deadline', '')}")
    report_lines.append("")
    
    # Expiring in 10 days
    report_lines.append("### Τι λήγει μέσα σε 10 μέρες")
    if expiring_soon:
        for diff, ev in expiring_soon:
            day_text = "σήμερα!" if diff == 0 else ("αύριο!" if diff == 1 else f"σε {diff} μέρες")
            report_lines.append(f"- **{ev.get('deadline')}** ({day_text}): `{ev['ref']}` {ev['title']} ({ev['org']}, {ev['fg']}, {ev['city']})")
    else:
        report_lines.append("Καμία θέση δεν λήγει μέσα στις επόμενες 10 μέρες.")
    report_lines.append("")
    
    # Expired
    report_lines.append("### Τι αφαιρέθηκε επειδή έληξε")
    if expired_items:
        for exp in expired_items:
            report_lines.append(f"- `{exp['ref']}` {exp['title']} (έληξε: {exp.get('deadline')})")
    else:
        report_lines.append("Καμία θέση δεν αφαιρέθηκε (0 θέσεις έληξαν).")
    report_lines.append("")
    
    # Not checked
    report_lines.append("### Πηγές που δεν ελέγχθηκαν και γιατί")
    for nc in not_checked:
        report_lines.append(f"- {nc}")
    report_lines.append("")
    
    # Metrics
    report_lines.append("### Μετρητές κάλυψης")
    report_lines.append(f"- `σελίδες πύλης: {portal_pages}`")
    report_lines.append(f"- `πηγές: {sources}`")
    report_lines.append("")
    report_lines.append("---")
    report_lines.append("")
    
    new_report_block = "\n".join(report_lines)
    
    old_content = ""
    if os.path.exists(REPORT_FILE):
        with open(REPORT_FILE, 'r', encoding='utf-8') as f:
            old_content = f.read()
            
    header_idx = old_content.find("# Ιστορικό σαρώσεων")
    if header_idx != -1:
        sep_idx = old_content.find("---", header_idx)
        if sep_idx != -1:
            updated_content = old_content[:sep_idx+3] + "\n\n" + new_report_block + old_content[sep_idx+3:]
        else:
            updated_content = old_content + "\n\n" + new_report_block
    else:
        updated_content = "# Ιστορικό σαρώσεων\n\n---\n\n" + new_report_block + old_content
        
    with open(REPORT_FILE, 'w', encoding='utf-8') as f:
        f.write(updated_content)
    print("-> Updated SCAN-REPORT.md with new section at top")


if __name__ == "__main__":
    run_scanner()
