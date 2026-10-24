"""US corporate PAC contributions to federal candidates, from FEC bulk data.

Run: python3 -m influence.sources.us_pac

Inputs (cached under data/influence/raw/us_pac/, read straight from the zips,
nothing is extracted to disk):
  cm{yy}.zip    committee master      -> which committees belong to which company
  cn{yy}.zip    candidate master      -> candidate party (CAND_PTY_AFFILIATION)
  pas2{yy}.zip  committee -> candidate contributions (itemized)
  webk{yy}.zip  PAC & party summary   -> total receipts per committee and cycle
where yy is the last year of the two-year cycle (2016 ... 2026).

Outputs:
  data/influence/clean/us_pac_matches.csv  company_id, committee_id, committee_name,
      connected_org, match_method, note  (one row per company/committee pair kept)
  data/influence/clean/us_pac.csv  one row per company_id x cycle (all companies,
      zeros where no PAC matched): pac_to_candidates_usd, pac_to_rep_usd,
      pac_to_dem_usd, pac_to_other_usd, share_rep, n_candidates, pac_receipts_usd, n_pacs

Method
------
Matching.  Each company has an explicit alias list (regexes, below) that is run
against every name a committee has carried in any cycle's committee master, both
CMTE_NM and CONNECTED_ORG_NM.  Only committees that look like separate segregated
funds are considered: committee type Q/N, designation not leadership (D), joint
fundraiser (J) or candidate (P/A), and organisation type corporation (C), corporation
without capital stock (W) or blank.  This drops trade-association (T), membership (M),
labour (L) and cooperative (V) PACs.  Every hit was reviewed by hand; false hits and
spin-offs are listed in EXCLUDE with the reason.  No fuzzy matching.

Ownership windows.  Subsidiary PACs count only while the subsidiary belonged to the
company: WINDOWS gives a from/to date (deal closing dates) and contributions are
filtered on TRANSACTION_DT.  Spun-off businesses' PACs (GE HealthCare, GE Vernova,
Siemens Energy, Carrier, Otis, Solventum, Haleon, Organon, ...) are excluded for all
cycles; the parent's own PAC keeps its pre-spin history.  PACs of the businesses that
emerged from the 2019 DowDuPont split follow the company that inherited them for
all cycles (Dow AgroSciences PAC -> Corteva; E.I. du Pont PAC -> DuPont).

Contributions.  pac_to_candidates_usd = net sum of non-memo itemized contributions
from matched committees to candidates, transaction types 24K (direct contribution)
and 24Z (in-kind contribution).  Independent expenditures for/against candidates
(24E/24A), communication costs and transfers to other committees (party, leadership
PACs, other PACs) are NOT included; corporate PACs make few IEs.  Party split uses the
candidate master: the party in the same cycle's cn file, else the nearest cycle the
candidate appears in (rows without CAND_ID are resolved through the recipient
committee's CAND_ID / party in the committee master).  DEM and DFL -> dem, REP -> rep, everything else (IND, LIB,
unknown) -> other.  share_rep = rep / (rep + dem).  n_candidates = distinct candidates
with positive net receipts from the company's PACs in the cycle.

Receipts.  pac_receipts_usd = sum over matched committees of TTL_RECEIPTS minus
TRANS_FROM_AFF (transfers from affiliated committees, so money moving between a
company's own PACs is not double counted), from the webk summary.  It cannot be
date-filtered, so a subsidiary PAC's whole-cycle receipts count only in cycles its
ownership window covers for more than half (contributions are date-filtered exactly).
share_rep floors negative net party totals (refunds) at zero.

Caveats.  PAC money is voluntary contributions from employees (and sometimes
shareholders), not corporate treasury funds, which federal law bars from candidate
contributions; the company pays the PAC's administrative costs.  "State" or
"combined" PACs that register with the FEC (BofA, Citi, HSBC, AT&T Ohio) are
included; only their federal candidate giving appears here.  The 2026 cycle is
partial (file as of download date).  Company-level money also flows via trade
associations, super PACs and executives' personal giving, none of which is here.
"""

import csv
import io
import re
import sys
import zipfile
from collections import defaultdict
from datetime import date

from influence.common import CLEAN, RAW, cached, companies, get, write_csv

BASE = "https://www.fec.gov/files/bulk-downloads"
DIR = RAW / "us_pac"
CYCLES = [2016, 2018, 2020, 2022, 2024, 2026]

CM_COLS = ("CMTE_ID CMTE_NM TRES_NM CMTE_ST1 CMTE_ST2 CMTE_CITY CMTE_ST CMTE_ZIP CMTE_DSGN "
           "CMTE_TP CMTE_PTY_AFFILIATION CMTE_FILING_FREQ ORG_TP CONNECTED_ORG_NM CAND_ID").split()
CN_COLS = ("CAND_ID CAND_NAME CAND_PTY_AFFILIATION CAND_ELECTION_YR CAND_OFFICE_ST CAND_OFFICE "
           "CAND_OFFICE_DISTRICT CAND_ICI CAND_STATUS CAND_PCC CAND_ST1 CAND_ST2 CAND_CITY CAND_ST "
           "CAND_ZIP").split()
PAS2_COLS = ("CMTE_ID AMNDT_IND RPT_TP TRANSACTION_PGI IMAGE_NUM TRANSACTION_TP ENTITY_TP NAME CITY "
             "STATE ZIP_CODE EMPLOYER OCCUPATION TRANSACTION_DT TRANSACTION_AMT OTHER_ID CAND_ID "
             "TRAN_ID FILE_NUM MEMO_CD MEMO_TEXT SUB_ID").split()
# webk has no published header file; layout per the FEC data dictionary.
WEBK_COLS = ("CMTE_ID CMTE_NM CMTE_TP CMTE_DSGN CMTE_FILING_FREQ TTL_RECEIPTS TRANS_FROM_AFF "
             "INDV_CONTRIB OTHER_POL_CMTE_CONTRIB CAND_CONTRIB CAND_LOANS TTL_LOANS_RECEIVED "
             "TTL_DISB TRANF_TO_AFF INDV_REFUNDS OTHER_POL_CMTE_REFUNDS CAND_LOAN_REPAY LOAN_REPAY "
             "COH_BOP COH_COP DEBTS_OWED_BY NONFED_TRANS_RECEIVED CONTRIB_TO_OTHER_CMTE IND_EXP "
             "PTY_COORD_EXP NONFED_SHARE_EXP CVG_END_DT").split()

CONTRIB_TYPES = {"24K", "24Z"}

# Per-company alias regexes, matched (case-insensitive) against every committee name
# and connected-organisation name the committee has had.  Subsidiaries included.
ALIASES = {
    "alphabet": [r"\bGOOGLE\b", r"\bALPHABET INC"],
    "meta": [r"\bMETA PLATFORMS", r"\bFACEBOOK,? INC"],
    "microsoft": [r"\bMICROSOFT CORP", r"\bLINKEDIN CORP", r"\bACTIVISION BLIZZARD"],
    "apple": [r"\bAPPLE INC\b", r"\bAPPLE COMPUTER"],
    "amazon": [r"\bAMAZON\.?COM", r"\bAMAZON WEB SERVICES", r"\bWHOLE FOODS MARKET"],
    "oracle": [r"\bORACLE (AMERICA|CORP)", r"\bCERNER CORP"],
    "ibm": [r"\bINTERNATIONAL BUSINESS MACHINES", r"\bIBM\b", r"\bRED HAT,? INC"],
    "intel": [r"\bINTEL CORP"],
    "qualcomm": [r"\bQUALCOMM\b"],
    "nvidia": [r"\bNVIDIA\b"],
    "cisco": [r"\bCISCO SYSTEMS", r"\bSPLUNK\b"],
    "uber": [r"\bUBER TECHNOLOGIES"],
    "airbnb": [r"\bAIRBNB\b"],
    "pfizer": [r"\bPFIZER\b", r"\bHOSPIRA\b", r"\bSEAGEN\b", r"\bWYETH\b"],
    "jnj": [r"\bJOHNSON ?& ?JOHNSON\b", r"\bJANSSEN\b", r"\bABIOMED\b"],
    "merck": [r"\bMERCK & CO"],
    "abbvie": [r"\bABBVIE\b", r"\bALLERGAN\b"],
    "lilly": [r"\bELI LILLY\b"],
    "bms": [r"\bBRISTOL[- ]MYERS", r"\bCELGENE\b"],
    "amgen": [r"\bAMGEN\b", r"\bHORIZON THERAPEUTICS\b"],
    "gilead": [r"\bGILEAD SCIENCES"],
    "exxon": [r"\bEXXON ?MOBIL\b", r"\bXTO ENERGY", r"\bPIONEER NATURAL RESOURCES"],
    "chevron": [r"\bCHEVRON\b", r"\bNOBLE ENERGY\b", r"\bHESS CORP"],
    "conocophillips": [r"\bCONOCOPHILLIPS\b", r"\bMARATHON OIL\b", r"\bCONCHO RESOURCES"],
    "jpmorgan": [r"\bJ\.? ?P\.? ?MORGAN\b", r"\bJPMORGAN\b"],
    "goldman": [r"\bGOLDMAN SACHS\b"],
    "morganstanley": [r"\bMORGAN STANLEY\b", r"\bE\*TRADE\b"],
    "bofa": [r"\bBANK OF AMERICA CORP", r"\bMERRILL LYNCH\b"],
    "citi": [r"\bCITIGROUP\b", r"\bCITIBANK\b"],
    "blackrock": [r"\bBLACKROCK (FUNDS|INC)"],
    "visa": [r"\bVISA,? INC"],
    "mastercard": [r"\bMASTERCARD\b"],
    "boeing": [r"\bBOEING\b", r"\bSPIRIT AEROSYSTEMS"],
    "lockheed": [r"\bLOCKHEED MARTIN", r"\bSIKORSKY\b"],
    "rtx": [r"\bRAYTHEON\b", r"\bRTX CORP", r"\bUNITED TECHNOLOGIES CORP", r"\bROCKWELL COLLINS"],
    "generaldynamics": [r"\bGENERAL DYNAMICS\b", r"\bGULFSTREAM AEROSPACE", r"\bCSRA INC"],
    "northrop": [r"\bNORTHROP GRUMMAN\b", r"\bORBITAL ATK\b"],
    "ge": [r"^(THE )?GENERAL ELECTRIC COMPANY\b"],
    "honeywell": [r"\bHONEYWELL\b"],
    "3m": [r"^3M COMPANY\b"],
    "caterpillar": [r"\bCATERPILLAR INC"],
    "ford": [r"\bFORD MOTOR\b"],
    "gm": [r"\bGENERAL MOTORS\b"],
    "tesla": [r"\bTESLA,? INC", r"\bTESLA MOTORS"],
    "cocacola": [r"^THE COCA-COLA COMPANY\b"],
    "pepsico": [r"\bPEPSICO\b"],
    "pg": [r"\bPROCTER & GAMBLE\b"],
    "pmi": [r"\bPHILIP MORRIS INTERNATIONAL", r"\bPHILIP MORRIS USA INTERNATIONAL", r"\bSWEDISH MATCH\b"],
    "altria": [r"\bALTRIA\b", r"\bPHILIP MORRIS USA\b(?! INTERNATIONAL)"],
    "walmart": [r"\bWAL-?MART\b"],
    "mcdonalds": [r"\bMCDONALD'?S CORP"],
    "att": [r"\bAT&T INC", r"\bDIRECTV\b", r"\bWARNER ?MEDIA\b", r"^TIME WARNER INC"],
    "verizon": [r"\bVERIZON\b", r"\bYAHOO INC", r"\bOATH INC", r"\bTRACFONE\b"],
    "comcast": [r"\bCOMCAST\b", r"\bNBCUNIVERSAL\b"],
    "dow": [r"\bDOW CHEMICAL\b", r"\bDOW INC\b", r"\bDOW CORNING\b"],
    "dupont": [r"\bDU ?PONT\b"],
    "corteva": [r"\bCORTEVA\b", r"\bDOW AGROSCIENCES\b"],
    "siemens": [r"\bSIEMENS\b"],
    "basf": [r"\bBASF\b"],
    "bayer": [r"\bBAYER (US|CORP|HEALTHCARE|CROPSCIENCE)", r"\bMONSANTO\b"],
    "volkswagen": [r"\bVOLKSWAGEN\b"],
    "bmw": [r"\bBMW\b"],
    "mercedes": [r"\bMERCEDES-BENZ\b", r"\bDAIMLER\b"],
    "dtelekom": [r"\bT-MOBILE\b", r"\bSPRINT CORP", r"\bDEUTSCHE TELEKOM\b"],
    "sap": [r"\bSAP AMERICA\b", r"\bSAP SE\b"],
    "allianz": [r"\bALLIANZ\b"],
    "deutschebank": [r"\bDEUTSCHE BANK\b"],
    "airbus": [r"\bAIRBUS\b"],
    "totalenergies": [r"\bTOTALENERGIES\b", r"\bTOTAL (HOLDINGS|PETROCHEMICALS|E&P) USA"],
    "sanofi": [r"\bSANOFI\b", r"\bGENZYME\b"],
    "loreal": [r"\bL'?OR[EÉ]AL\b"],
    "lvmh": [r"\bLVMH\b", r"\bMOET HENNESSY\b", r"\bTIFFANY & CO"],
    "bnpparibas": [r"\bBNP PARIBAS\b", r"\bBANK OF THE WEST\b"],
    "axa": [r"\bAXA\b", r"\bEQUITABLE HOLDINGS\b", r"\bX ?L AMERICA\b"],
    "danone": [r"\bDANONE\b"],
    "orange": [r"\bORANGE S\.?A\b", r"\bORANGE BUSINESS SERVICES\b"],
    "renault": [r"\bRENAULT\b"],
    "safran": [r"\bSAFRAN\b"],
    "thales": [r"\bTHALES\b"],
    "stellantis": [r"\bSTELLANTIS\b", r"\bFCA US\b", r"\bCHRYSLER (GROUP|LLC)\b"],
    "shell": [r"\bSHELL OIL COMPANY\b", r"\bSHELL USA\b"],
    "ing": [r"\bING (AMERICA|FINANCIAL SERVICES|BANK|GROEP)\b"],
    "asml": [r"\bASML\b", r"\bCYMER\b"],
    "philips": [r"\bPHILIPS (NORTH AMERICA|ELECTRONICS)\b"],
    "unilever": [r"\bUNILEVER\b"],
    "heineken": [r"\bHEINEKEN\b"],
    "ahold": [r"\bAHOLD\b"],
    "prosus": [r"\bPROSUS\b", r"\bNASPERS\b"],
    "bp": [r"\bBP CORPORATION NORTH AMERICA\b", r"\bBP AMERICA\b"],
    "astrazeneca": [r"\bASTRAZENECA\b", r"^ZENECA\.? INC", r"\bMEDIMMUNE\b", r"\bALEXION PHARMACEUTICALS\b"],
    "gsk": [r"\bGLAXOSMITHKLINE LLC\b", r"\bGSK PAC\b"],
    "hsbc": [r"\bHSBC\b"],
    "barclays": [r"\bBARCLAYS\b"],
    "bae": [r"\bBAE SYSTEMS\b"],
    "bat": [r"\bREYNOLDS AMERICAN\b", r"\bBRITISH AMERICAN TOBACCO\b"],
    "riotinto": [r"\bRIO TINTO\b"],
    "vodafone": [r"\bVODAFONE\b"],
    "nestle": [r"\bNESTL[EÉ]\b"],
    "novartis": [r"\bNOVARTIS\b", r"\bSANDOZ\b"],
    "roche": [r"\bROCHE DIAGNOSTICS\b", r"\bGENENTECH\b", r"\bHOFFMANN-LA ROCHE\b",
              r"\bSPARK THERAPEUTICS\b"],
    "ubs": [r"\bUBS AMERICAS\b", r"\bCREDIT SUISSE (SECURITIES|HOLDINGS)\b"],
    "equinor": [r"\bEQUINOR\b", r"\bSTATOIL\b"],
    "novonordisk": [r"\bNOVO NORDISK\b"],
    "ericsson": [r"\bERICSSON\b", r"\bVONAGE\b"],
    "nokia": [r"\bNOKIA\b", r"\bALCATEL-LUCENT\b", r"\bINFINERA\b"],
    "iberdrola": [r"\bIBERDROLA\b", r"\bAVANGRID\b"],
    "santander": [r"\bSANTANDER\b", r"\bSOVEREIGN BANCORP\b"],
    "enel": [r"\bENEL\b"],
    "eni": [r"\bENI (US|USA|PETROLEUM)\b"],
    "spotify": [r"\bSPOTIFY\b"],
}

# (company_id, committee_id) -> reason.  Alias hits reviewed and rejected.
EXCLUDE = {
    ("dow", "C00618546"): "Hemlock Semiconductor PAC: Hemlock is a Dow Corning-affiliated JV (Shin-Etsu/Corning), not a Dow subsidiary",
    ("rtx", "C00531772"): "Penn United Technologies Inc - unrelated Pennsylvania firm",
    ("siemens", "C00848820"): "Siemens Energy Inc PAC - Siemens Energy spun off 2020, Siemens holds a minority stake",
    ("abbvie", "C00292102"): "pre-2015 Allergan Inc PAC (acquired by Actavis 2015), never under AbbVie",
    ("ing", "C00184028"): "Voya Financial - ING fully divested 2015",
}

# committee_id -> (from_date, to_date, note): count contributions only within the
# window when the business belonged to the company (deal closing dates).
WINDOWS = {
    "C00042069": ("2018-06-07", None, "Monsanto PAC; Bayer closed Monsanto acquisition 2018-06-07"),
    "C00339291": ("2018-06-14", "2022-04-08", "Time Warner/WarnerMedia PAC; owned by AT&T 2018-06-14 to 2022-04-08"),
    "C00089342": ("2020-04-01", None, "Sprint PAC; T-Mobile (Deutsche Telekom-controlled) merger closed 2020-04-01"),
    "C00097568": ("2020-04-03", None, "Raytheon Co PAC; UTC-Raytheon merger closed 2020-04-03 (continues as RTX employees PAC)"),
    "C00365684": ("2018-11-26", None, "Rockwell Collins PAC; acquired by UTC 2018-11-26"),
    "C00250209": ("2018-06-06", None, "Orbital ATK PAC; acquired by Northrop Grumman 2018-06-06"),
    "C00195263": ("2018-06-06", None, "Orbital ATK legacy PAC; Orbital ATK acquired by Northrop Grumman 2018-06-06"),
    "C00386672": ("2016-06-01", None, "Dow Corning PAC; Dow took full ownership 2016-06-01"),
    "C00471169": ("2021-07-21", None, "Alexion PAC; acquired by AstraZeneca 2021-07-21"),
    "C00042002": ("2017-07-25", None, "Reynolds American PAC; acquired by BAT 2017-07-25"),
    "C00161901": (None, "2019-03-31", "AXA Equitable PAC; AXA lost majority of Equitable Holdings in March 2019 (approx. date)"),
    "C00388975": ("2018-09-12", None, "XL America PAC; XL Group acquired by AXA 2018-09-12"),
    "C00391086": ("2020-05-08", None, "Allergan PAC; acquired by AbbVie 2020-05-08"),
    "C00426445": ("2022-12-22", None, "Abiomed PAC; acquired by J&J 2022-12-22"),
    "C00514331": ("2019-11-20", None, "Celgene PAC; acquired by BMS 2019-11-20"),
    "C00624148": ("2023-10-06", None, "Horizon Therapeutics PAC; acquired by Amgen 2023-10-06"),
    "C00040568": ("2024-11-22", None, "Marathon Oil PAC; acquired by ConocoPhillips 2024-11-22"),
    "C00542092": ("2021-01-15", None, "Concho Resources PAC; acquired by ConocoPhillips 2021-01-15"),
    "C00420950": ("2024-05-03", None, "Pioneer Natural Resources PAC; acquired by ExxonMobil 2024-05-03"),
    "C00479873": ("2020-10-05", None, "Noble Energy PAC; acquired by Chevron 2020-10-05"),
    "C00557322": ("2025-07-18", None, "Hess PAC; acquired by Chevron 2025-07-18"),
    "C00111559": ("2023-06-12", None, "Credit Suisse PAC; Credit Suisse acquired by UBS 2023-06-12"),
    "C00101410": ("2018-04-03", None, "CSRA PAC; acquired by General Dynamics 2018-04-03"),
    "C00428110": ("2025-12-08", None, "Spirit AeroSystems PAC; Boeing acquisition closed Dec 2025"),
    "C00215053": ("2022-11-11", None, "Swedish Match PAC; PMI acquired control 2022-11"),
    "C00356345": ("2020-10-02", None, "E*Trade PAC; acquired by Morgan Stanley 2020-10-02"),
    "C00380535": ("2017-06-13", "2021-09-01", "Yahoo/Oath PAC; owned by Verizon 2017-06-13 to 2021-09-01"),
    "C00561522": ("2021-11-23", None, "TracFone PAC; acquired by Verizon 2021-11-23"),
    "C00410589": ("2022-06-08", None, "Cerner PAC; acquired by Oracle 2022-06-08"),
    "C00662718": ("2019-12-17", None, "Spark Therapeutics PAC; acquired by Roche 2019-12-17"),
    "C00302943": (None, "2021-03-31", "Nestle Waters North America PAC; business sold 2021-03-31"),
    "C00321406": ("2025-01-01", None,
                  "Webster Bank PAC; FEC committee master lists it as 'Santander Holdings USA - Webster PAC' "
                  "only from the 2026 cycle, so counted from 2025 only; ownership change not independently verified"),
}


def fetch(name, year):
    path = DIR / name
    cached(path, lambda: get(f"{BASE}/{year}/{name}"))
    return path


def rows(zpath, cols):
    with zipfile.ZipFile(zpath) as z:
        member = z.namelist()[0]
        with z.open(member) as fh:
            for line in io.TextIOWrapper(fh, encoding="latin-1", newline=""):
                f = line.rstrip("\r\n").split("|")
                yield dict(zip(cols, f))


def parse_date(s):
    s = (s or "").strip()
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return date(int(s[4:]), int(s[:2]), int(s[2:4]))
    except ValueError:
        return None


def iso(s):
    return date.fromisoformat(s) if s else None


def in_window(cid, d, cycle):
    w = WINDOWS.get(cid)
    if not w:
        return True
    lo, hi = iso(w[0]), iso(w[1])
    if d is None:  # undated rows: fall back to the cycle overlapping the window at all
        return cycle_overlaps(cid, cycle)
    return (lo is None or d >= lo) and (hi is None or d <= hi)


def cycle_overlaps(cid, cycle, min_share=0.0):
    """True if the ownership window covers more than `min_share` of the cycle."""
    w = WINDOWS.get(cid)
    if not w:
        return True
    start, end = date(cycle - 1, 1, 1), date(cycle, 12, 31)
    lo, hi = max(iso(w[0]) or start, start), min(iso(w[1]) or end, end)
    if lo > hi:
        return False
    return (hi - lo).days + 1 > min_share * ((end - start).days + 1)


def load_committees(cycles):
    """committee_id -> {names, orgs, latest row}, for SSF-like committees."""
    com = {}
    for cyc in cycles:
        for r in rows(fetch(f"cm{str(cyc)[2:]}.zip", cyc), CM_COLS):
            c = com.setdefault(r["CMTE_ID"], {"names": [], "orgs": [], "cycles": [], "types": set()})
            c["cycles"].append(cyc)
            c["latest"] = r
            c["types"].add((r["CMTE_TP"], r["CMTE_DSGN"], r["ORG_TP"]))
            for k, lst in (("CMTE_NM", "names"), ("CONNECTED_ORG_NM", "orgs")):
                v = r[k].strip()
                if v and v not in c[lst]:
                    c[lst].append(v)
    return com


def ssf_like(c):
    tp, dsgn, org = c["latest"]["CMTE_TP"], c["latest"]["CMTE_DSGN"], c["latest"]["ORG_TP"]
    return tp in ("Q", "N") and dsgn not in ("D", "J", "P", "A") and org in ("C", "W", "")


def match(com):
    pats = {cid: [re.compile(p, re.I) for p in ps] for cid, ps in ALIASES.items()}
    out = []
    for cmte_id, c in sorted(com.items()):
        if not ssf_like(c):
            continue
        for company_id, ps in pats.items():
            how = None
            for field, vals in (("committee_name", c["names"]), ("connected_org", c["orgs"])):
                for v in vals:
                    for p in ps:
                        if p.search(v):
                            how = how or f"alias:{field}:{p.pattern}"
            if not how:
                continue
            if (company_id, cmte_id) in EXCLUDE:
                print(f"  excluded {company_id} {cmte_id} {c['latest']['CMTE_NM']}: "
                      f"{EXCLUDE[(company_id, cmte_id)]}", file=sys.stderr)
                continue
            notes = []
            if cmte_id in WINDOWS:
                w = WINDOWS[cmte_id]
                notes.append(f"window {w[0] or '-'}..{w[1] or '-'}: {w[2]}")
            if len(c["names"]) > 1:
                notes.append("former names: " + "; ".join(c["names"][:-1]))
            notes.append(f"in cm files {min(c['cycles'])}-{max(c['cycles'])}")
            out.append({
                "company_id": company_id, "committee_id": cmte_id,
                "committee_name": c["latest"]["CMTE_NM"].strip(),
                "connected_org": (c["orgs"][-1] if c["orgs"] else ""),
                "match_method": how, "note": " | ".join(notes),
            })
    return out


def party_map(cycles):
    by_cycle = defaultdict(dict)
    for cyc in cycles:
        for r in rows(fetch(f"cn{str(cyc)[2:]}.zip", cyc), CN_COLS):
            by_cycle[cyc][r["CAND_ID"]] = r["CAND_PTY_AFFILIATION"].strip().upper()
    return by_cycle


def party_of(cand, cyc, by_cycle, cycles):
    p = by_cycle[cyc].get(cand)
    if p is None:
        for c in sorted(cycles, key=lambda x: (abs(x - cyc), -x)):
            p = by_cycle[c].get(cand)
            if p is not None:
                break
    return norm_party(p)


def norm_party(p):
    if p == "REP":
        return "rep"
    if p in ("DEM", "DFL"):
        return "dem"
    return "other"


def share(rep, dem):
    """Republican share of two-party giving; net refunds can make a side negative, floor at 0."""
    rep, dem = max(rep, 0.0), max(dem, 0.0)
    return round(rep / (rep + dem), 4) if rep + dem > 0 else ""


def main():
    DIR.mkdir(parents=True, exist_ok=True)
    cycles = []
    for cyc in CYCLES:
        yy = str(cyc)[2:]
        try:
            for f in ("cm", "cn", "pas2", "webk"):
                fetch(f"{f}{yy}.zip", cyc)
            cycles.append(cyc)
        except Exception as e:  # noqa: BLE001 - a future cycle may not be published yet
            print(f"cycle {cyc} unavailable: {e}", file=sys.stderr)

    com = load_committees(cycles)
    matches = match(com)
    dupes = defaultdict(list)
    for m in matches:
        dupes[m["committee_id"]].append(m["company_id"])
    for cid, cos in dupes.items():
        if len(cos) > 1:
            raise SystemExit(f"committee {cid} matched to several companies {cos}: fix ALIASES/EXCLUDE")
    owner = {m["committee_id"]: m["company_id"] for m in matches}
    write_csv(CLEAN / "us_pac_matches.csv", matches,
              ["company_id", "committee_id", "committee_name", "connected_org", "match_method", "note"])

    parties = party_map(cycles)
    # Some rows lack CAND_ID: resolve the recipient committee to its candidate / party.
    cmte_cand = {k: c["latest"]["CAND_ID"].strip() for k, c in com.items() if c["latest"]["CAND_ID"].strip()}
    cmte_party = {k: c["latest"]["CMTE_PTY_AFFILIATION"].strip().upper() for k, c in com.items()}
    agg = defaultdict(lambda: {"rep": 0.0, "dem": 0.0, "other": 0.0, "cands": defaultdict(float),
                               "pacs": set(), "receipts": 0.0})
    ie_total = defaultdict(float)
    for cyc in cycles:
        for r in rows(fetch(f"pas2{str(cyc)[2:]}.zip", cyc), PAS2_COLS):
            co = owner.get(r["CMTE_ID"])
            if co is None or r["MEMO_CD"].strip() == "X":
                continue
            tt = r["TRANSACTION_TP"].strip()
            try:
                amt = float(r["TRANSACTION_AMT"] or 0)
            except ValueError:
                continue
            if not in_window(r["CMTE_ID"], parse_date(r["TRANSACTION_DT"]), cyc):
                continue
            if tt in ("24E", "24A"):
                ie_total[(co, cyc)] += amt
                continue
            if tt not in CONTRIB_TYPES:
                continue
            recip = r["OTHER_ID"].strip()
            cand = r["CAND_ID"].strip() or cmte_cand.get(recip, "") or recip
            side = party_of(cand, cyc, parties, cycles)
            if side == "other" and not any(cand in parties[c] for c in cycles):
                side = norm_party(cmte_party.get(recip, ""))  # recipient committee's own party
            a = agg[(co, cyc)]
            a[side] += amt
            a["cands"][cand] += amt
            a["pacs"].add(r["CMTE_ID"])
        for r in rows(fetch(f"webk{str(cyc)[2:]}.zip", cyc), WEBK_COLS):
            co = owner.get(r["CMTE_ID"])
            if co is None or not cycle_overlaps(r["CMTE_ID"], cyc, 0.5):
                continue
            try:
                rec = float(r["TTL_RECEIPTS"] or 0) - float(r["TRANS_FROM_AFF"] or 0)
            except ValueError:
                continue
            agg[(co, cyc)]["receipts"] += rec
            agg[(co, cyc)]["pacs"].add(r["CMTE_ID"])

    out = []
    for c in companies():
        for cyc in cycles:
            a = agg.get((c["company_id"], cyc))
            if a is None:
                out.append({"company_id": c["company_id"], "cycle": cyc, "pac_to_candidates_usd": 0,
                            "pac_to_rep_usd": 0, "pac_to_dem_usd": 0, "pac_to_other_usd": 0,
                            "share_rep": "", "n_candidates": 0, "pac_receipts_usd": 0, "n_pacs": 0})
                continue
            rep, dem, oth = a["rep"], a["dem"], a["other"]
            out.append({
                "company_id": c["company_id"], "cycle": cyc,
                "pac_to_candidates_usd": round(rep + dem + oth),
                "pac_to_rep_usd": round(rep), "pac_to_dem_usd": round(dem), "pac_to_other_usd": round(oth),
                "share_rep": share(rep, dem),
                "n_candidates": sum(1 for v in a["cands"].values() if v > 0),
                "pac_receipts_usd": round(a["receipts"]),
                "n_pacs": len(a["pacs"]),
            })
    write_csv(CLEAN / "us_pac.csv", out)

    n_co = len({m["company_id"] for m in matches})
    print(f"matched {len(matches)} committees to {n_co} companies; cycles {cycles}")
    ies = {k: v for k, v in ie_total.items() if abs(v) > 0}
    if ies:
        print("independent expenditures (24E/24A) by matched PACs, excluded from totals:")
        for (co, cyc), v in sorted(ies.items()):
            print(f"  {co} {cyc}: ${v:,.0f}")
    latest = max(cycles)
    for cyc in sorted({2024, latest}):
        top = sorted((r for r in out if r["cycle"] == cyc), key=lambda r: -r["pac_to_candidates_usd"])[:15]
        print(f"top 15, cycle {cyc}:")
        for r in top:
            print(f"  {r['company_id']:<16} ${r['pac_to_candidates_usd']:>11,}  R ${r['pac_to_rep_usd']:>10,}"
                  f"  D ${r['pac_to_dem_usd']:>10,}  share_rep {r['share_rep']}  pacs {r['n_pacs']}")


if __name__ == "__main__":
    main()
