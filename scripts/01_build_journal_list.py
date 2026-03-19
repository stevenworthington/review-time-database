#!/usr/bin/env python3
"""
01_build_journal_list.py

Build a master journal list from the SJR (Scimago Journal Rankings) CSV.
Groups ~334 Scopus categories into ~50 broader fields, then selects the
top 10-20 journals per field ranked by SJR score.

Input:  data/scimagojr_2024.csv  (downloaded from scimagojr.com)
Output: data/journal_list.csv
"""

import argparse
import pandas as pd
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Category → Field mapping
# We collapse ~334 Scopus subject categories into ~50 research fields.
# Each journal is assigned to a single primary category (first listed in SJR).
# ---------------------------------------------------------------------------

CATEGORY_TO_FIELD = {
    # ── Medicine ──────────────────────────────────────────────────────────
    "Cardiology and Cardiovascular Medicine": "Cardiovascular Medicine",
    "Critical Care and Intensive Care Medicine": "Critical Care & Emergency Medicine",
    "Emergency Medicine": "Critical Care & Emergency Medicine",
    "Oncology": "Oncology",
    "Cancer Research": "Oncology",
    "Hematology": "Hematology",
    "Infectious Diseases": "Infectious Disease",
    "Microbiology (medical)": "Infectious Disease",
    "Immunology": "Immunology",
    "Immunology and Allergy": "Immunology",
    "Immunology and Microbiology (miscellaneous)": "Immunology",
    "Gastroenterology": "Gastroenterology & Hepatology",
    "Hepatology": "Gastroenterology & Hepatology",
    "Endocrinology, Diabetes and Metabolism": "Endocrinology & Metabolism",
    "Endocrinology": "Endocrinology & Metabolism",
    "Pulmonary and Respiratory Medicine": "Respiratory Medicine",
    "Nephrology": "Nephrology & Urology",
    "Urology": "Nephrology & Urology",
    "Neurology (clinical)": "Neurology",
    "Neurology": "Neurology",  # added extra mapping
    "Dermatology": "Dermatology",
    "Rheumatology": "Rheumatology",
    "Ophthalmology": "Ophthalmology",
    "Otorhinolaryngology": "Otolaryngology",
    "Orthopedics and Sports Medicine": "Orthopedics & Sports Medicine",
    "Surgery": "Surgery",
    "Transplantation": "Surgery",
    "Obstetrics and Gynecology": "Obstetrics & Gynecology",
    "Pediatrics, Perinatology and Child Health": "Pediatrics",
    "Psychiatry and Mental Health": "Psychiatry",
    "Radiology, Nuclear Medicine and Imaging": "Radiology & Imaging",
    "Anesthesiology and Pain Medicine": "Anesthesiology",
    "Pathology and Forensic Medicine": "Pathology",
    "Anatomy": "Anatomy & Physiology",
    "Physiology": "Anatomy & Physiology",
    "Physiology (medical)": "Anatomy & Physiology",
    "Geriatrics and Gerontology": "Geriatrics",
    "Rehabilitation": "Rehabilitation",
    "Public Health, Environmental and Occupational Health": "Public Health & Epidemiology",
    "Epidemiology": "Public Health & Epidemiology",
    "Health Policy": "Health Policy & Services",
    "Health Informatics": "Health Policy & Services",
    "Health Information Management": "Health Policy & Services",
    "Medicine (miscellaneous)": "General & Internal Medicine",
    "Internal Medicine": "General & Internal Medicine",
    "General Medicine": "General & Internal Medicine",
    "Family Practice": "General & Internal Medicine",
    "Complementary and Manual Therapy": "General & Internal Medicine",

    # ── Pharmacology & Drug Discovery ─────────────────────────────────────
    "Pharmacology": "Pharmacology & Toxicology",
    "Toxicology": "Pharmacology & Toxicology",
    "Pharmacology, Toxicology and Pharmaceutics (miscellaneous)": "Pharmacology & Toxicology",
    "Drug Discovery": "Pharmacology & Toxicology",
    "Pharmaceutical Science": "Pharmacology & Toxicology",

    # ── Dentistry ─────────────────────────────────────────────────────────
    "Dentistry (miscellaneous)": "Dentistry",
    "Oral Surgery": "Dentistry",
    "Orthodontics": "Dentistry",
    "Periodontics": "Dentistry",

    # ── Nursing & Health Professions ──────────────────────────────────────
    "Nursing (miscellaneous)": "Nursing",
    "Advanced and Specialized Nursing": "Nursing",
    "Community and Home Care": "Nursing",
    "Critical Care Nursing": "Nursing",
    "Emergency Nursing": "Nursing",
    "Gerontology": "Nursing",
    "Issues, Ethics and Legal Aspects": "Nursing",
    "Leadership and Management": "Nursing",
    "LPN and LVN": "Nursing",
    "Maternity and Midwifery": "Nursing",
    "Medical and Surgical Nursing": "Nursing",
    "Nutrition and Dietetics": "Nutrition & Food Science",
    "Assessment and Diagnosis": "Health Professions",
    "Chiropractics": "Health Professions",
    "Health Professions (miscellaneous)": "Health Professions",
    "Medical Laboratory Technology": "Health Professions",
    "Occupational Therapy": "Health Professions",
    "Optometry": "Health Professions",
    "Physical Therapy, Sports Therapy and Rehabilitation": "Health Professions",
    "Podiatry": "Health Professions",
    "Radiological and Ultrasound Technology": "Health Professions",
    "Respiratory Care": "Health Professions",
    "Speech and Hearing": "Health Professions",

    # ── Neuroscience ──────────────────────────────────────────────────────
    "Neuroscience (miscellaneous)": "Neuroscience",
    "Behavioral Neuroscience": "Neuroscience",
    "Biological Psychiatry": "Neuroscience",
    "Cellular and Molecular Neuroscience": "Neuroscience",
    "Cognitive Neuroscience": "Neuroscience",
    "Developmental Neuroscience": "Neuroscience",
    "Endocrine and Autonomic Systems": "Neuroscience",
    "Sensory Systems": "Neuroscience",

    # ── Biology & Life Sciences ───────────────────────────────────────────
    "Molecular Biology": "Molecular & Cell Biology",
    "Cell Biology": "Molecular & Cell Biology",
    "Genetics": "Genetics & Genomics",
    "Genetics (clinical)": "Genetics & Genomics",
    "Biochemistry": "Biochemistry",
    "Structural Biology": "Biochemistry",
    "Biophysics": "Biochemistry",
    "Biochemistry, Genetics and Molecular Biology (miscellaneous)": "Biochemistry",
    "Biotechnology": "Biotechnology",
    "Biomedical Engineering": "Biomedical Engineering",
    "Bioengineering": "Biomedical Engineering",
    "Microbiology": "Microbiology",
    "Parasitology": "Microbiology",
    "Virology": "Microbiology",
    "Applied Microbiology and Biotechnology": "Microbiology",
    "Ecology, Evolution, Behavior and Systematics": "Ecology & Evolution",
    "Ecology": "Ecology & Evolution",
    "Plant Science": "Plant & Agricultural Science",
    "Agronomy and Crop Science": "Plant & Agricultural Science",
    "Soil Science": "Plant & Agricultural Science",
    "Horticulture": "Plant & Agricultural Science",
    "Animal Science and Zoology": "Animal Science & Veterinary",
    "Veterinary (miscellaneous)": "Animal Science & Veterinary",
    "Equine": "Animal Science & Veterinary",
    "Food Animals": "Animal Science & Veterinary",
    "Small Animals": "Animal Science & Veterinary",
    "Aquatic Science": "Marine & Aquatic Science",
    "Insect Science": "Ecology & Evolution",
    "Agricultural and Biological Sciences (miscellaneous)": "Plant & Agricultural Science",
    "Forestry": "Plant & Agricultural Science",
    "Food Science": "Nutrition & Food Science",

    # ── Chemistry ─────────────────────────────────────────────────────────
    "Chemistry (miscellaneous)": "Chemistry",
    "Organic Chemistry": "Chemistry",
    "Inorganic Chemistry": "Chemistry",
    "Analytical Chemistry": "Chemistry",
    "Physical and Theoretical Chemistry": "Chemistry",
    "Electrochemistry": "Chemistry",
    "Spectroscopy": "Chemistry",

    # ── Materials Science ─────────────────────────────────────────────────
    "Materials Science (miscellaneous)": "Materials Science",
    "Biomaterials": "Materials Science",
    "Ceramics and Composites": "Materials Science",
    "Electronic, Optical and Magnetic Materials": "Materials Science",
    "Materials Chemistry": "Materials Science",
    "Metals and Alloys": "Materials Science",
    "Polymers and Plastics": "Materials Science",
    "Surfaces, Coatings and Films": "Materials Science",
    "Nanoscience and Nanotechnology": "Materials Science",

    # ── Physics ───────────────────────────────────────────────────────────
    "Physics and Astronomy (miscellaneous)": "Physics",
    "Atomic and Molecular Physics, and Optics": "Physics",
    "Condensed Matter Physics": "Physics",
    "Nuclear and High Energy Physics": "Physics",
    "Statistical and Nonlinear Physics": "Physics",
    "Mathematical Physics": "Physics",
    "Instrumentation": "Physics",
    "Astronomy and Astrophysics": "Astronomy & Astrophysics",
    "Space and Planetary Science": "Astronomy & Astrophysics",

    # ── Earth & Environmental Science ─────────────────────────────────────
    "Earth and Planetary Sciences (miscellaneous)": "Earth Science",
    "Geochemistry and Petrology": "Earth Science",
    "Geology": "Earth Science",
    "Geophysics": "Earth Science",
    "Geotechnical Engineering and Engineering Geology": "Earth Science",
    "Paleontology": "Earth Science",
    "Stratigraphy": "Earth Science",
    "Atmospheric Science": "Atmospheric & Ocean Science",
    "Oceanography": "Atmospheric & Ocean Science",
    "Environmental Science (miscellaneous)": "Environmental Science",
    "Ecological Modeling": "Environmental Science",
    "Environmental Chemistry": "Environmental Science",
    "Environmental Engineering": "Environmental Science",
    "Global and Planetary Change": "Environmental Science",
    "Health, Toxicology and Mutagenesis": "Environmental Science",
    "Management, Monitoring, Policy and Law": "Environmental Science",
    "Nature and Landscape Conservation": "Environmental Science",
    "Pollution": "Environmental Science",
    "Waste Management and Disposal": "Environmental Science",
    "Water Science and Technology": "Environmental Science",

    # ── Engineering ───────────────────────────────────────────────────────
    "Engineering (miscellaneous)": "General Engineering",
    "Aerospace Engineering": "Mechanical & Aerospace Engineering",
    "Automotive Engineering": "Mechanical & Aerospace Engineering",
    "Mechanical Engineering": "Mechanical & Aerospace Engineering",
    "Mechanics of Materials": "Mechanical & Aerospace Engineering",
    "Civil and Structural Engineering": "Civil Engineering",
    "Building and Construction": "Civil Engineering",
    "Ocean Engineering": "Civil Engineering",
    "Electrical and Electronic Engineering": "Electrical & Electronic Engineering",
    "Control and Systems Engineering": "Electrical & Electronic Engineering",
    "Signal Processing": "Electrical & Electronic Engineering",
    "Industrial and Manufacturing Engineering": "Industrial Engineering",
    "Safety, Risk, Reliability and Quality": "Industrial Engineering",
    "Media Technology": "Industrial Engineering",

    # ── Chemical Engineering ──────────────────────────────────────────────
    "Chemical Engineering (miscellaneous)": "Chemical Engineering",
    "Bioengineering": "Chemical Engineering",
    "Catalysis": "Chemical Engineering",
    "Chemical Health and Safety": "Chemical Engineering",
    "Colloid and Surface Chemistry": "Chemical Engineering",
    "Filtration and Separation": "Chemical Engineering",
    "Fluid Flow and Transfer Processes": "Chemical Engineering",
    "Process Chemistry and Technology": "Chemical Engineering",

    # ── Energy ────────────────────────────────────────────────────────────
    "Energy (miscellaneous)": "Energy",
    "Energy Engineering and Power Technology": "Energy",
    "Fuel Technology": "Energy",
    "Nuclear Energy and Engineering": "Energy",
    "Renewable Energy, Sustainability and the Environment": "Energy",

    # ── Computer Science ──────────────────────────────────────────────────
    "Computer Science (miscellaneous)": "Computer Science (General)",
    "Artificial Intelligence": "Artificial Intelligence",
    "Computer Graphics and Computer-Aided Design": "Computer Science (General)",
    "Computer Networks and Communications": "Computer Science (General)",
    "Computer Science Applications": "Computer Science (General)",
    "Computer Vision and Pattern Recognition": "Artificial Intelligence",
    "Computational Theory and Mathematics": "Computer Science (General)",
    "Hardware and Architecture": "Computer Science (General)",
    "Human-Computer Interaction": "Computer Science (General)",
    "Information Systems": "Information Systems",
    "Software": "Computer Science (General)",
    "Computational Mathematics": "Computer Science (General)",

    # ── Mathematics ───────────────────────────────────────────────────────
    "Mathematics (miscellaneous)": "Mathematics",
    "Algebra and Number Theory": "Mathematics",
    "Analysis": "Mathematics",
    "Applied Mathematics": "Mathematics",
    "Discrete Mathematics and Combinatorics": "Mathematics",
    "Geometry and Topology": "Mathematics",
    "Logic": "Mathematics",
    "Modeling and Simulation": "Mathematics",
    "Numerical Analysis": "Mathematics",
    "Theoretical Computer Science": "Mathematics",
    "Statistics and Probability": "Statistics",
    "Statistics, Probability and Uncertainty": "Statistics",

    # ── Decision Sciences ─────────────────────────────────────────────────
    "Decision Sciences (miscellaneous)": "Operations Research & Decision Science",
    "Information Systems and Management": "Operations Research & Decision Science",
    "Management Science and Operations Research": "Operations Research & Decision Science",

    # ── Economics & Finance ───────────────────────────────────────────────
    "Economics and Econometrics": "Economics",
    "Economics, Econometrics and Finance (miscellaneous)": "Economics",
    "Finance": "Finance",
    "Accounting": "Accounting",

    # ── Business & Management ─────────────────────────────────────────────
    "Business, Management and Accounting (miscellaneous)": "Business & Management",
    "Business and International Management": "Business & Management",
    "Management of Technology and Innovation": "Business & Management",
    "Management Information Systems": "Business & Management",
    "Organizational Behavior and Human Resource Management": "Business & Management",
    "Strategy and Management": "Business & Management",
    "Marketing": "Marketing",
    "Tourism, Leisure and Hospitality Management": "Business & Management",
    "Industrial Relations": "Business & Management",

    # ── Social Sciences ───────────────────────────────────────────────────
    "Social Sciences (miscellaneous)": "Sociology & Social Sciences",
    "Anthropology": "Sociology & Social Sciences",
    "Communication": "Communication & Media",
    "Cultural Studies": "Sociology & Social Sciences",
    "Demography": "Sociology & Social Sciences",
    "Development": "Development Studies",
    "Gender Studies": "Sociology & Social Sciences",
    "Geography, Planning and Development": "Geography & Urban Studies",
    "Urban Studies": "Geography & Urban Studies",
    "Transportation": "Geography & Urban Studies",
    "Human Factors and Ergonomics": "Sociology & Social Sciences",
    "Library and Information Sciences": "Library & Information Science",
    "Life-span and Life-course Studies": "Sociology & Social Sciences",
    "Linguistics and Language": "Linguistics",
    "Language and Linguistics": "Linguistics",
    "Safety Research": "Sociology & Social Sciences",
    "Sociology and Political Science": "Political Science",
    "Political Science and International Relations": "Political Science",
    "Law": "Law",
    "Health (social science)": "Public Health & Epidemiology",

    # ── Psychology ────────────────────────────────────────────────────────
    "Psychology (miscellaneous)": "Psychology",
    "Applied Psychology": "Psychology",
    "Clinical Psychology": "Psychology",
    "Developmental and Educational Psychology": "Psychology",
    "Experimental and Cognitive Psychology": "Psychology",
    "Neuropsychology and Physiological Psychology": "Psychology",
    "Social Psychology": "Psychology",

    # ── Education ─────────────────────────────────────────────────────────
    "Education": "Education",
    "Developmental and Educational Psychology": "Education",
    "e-learning": "Education",

    # ── Arts & Humanities ─────────────────────────────────────────────────
    "Arts and Humanities (miscellaneous)": "Arts & Humanities",
    "Archeology (arts and humanities)": "Arts & Humanities",
    "Archeology": "Arts & Humanities",
    "Classics": "Arts & Humanities",
    "Conservation": "Arts & Humanities",
    "History": "History",
    "History and Philosophy of Science": "History",
    "Literature and Literary Theory": "Arts & Humanities",
    "Music": "Arts & Humanities",
    "Philosophy": "Philosophy",
    "Religious Studies": "Arts & Humanities",
    "Visual Arts and Performing Arts": "Arts & Humanities",

    # ── Multidisciplinary ─────────────────────────────────────────────────
    "Multidisciplinary": "Multidisciplinary",

    # ── Catch-all for unmapped ────────────────────────────────────────────
    "Aging": "Geriatrics",
    "Drug Guides": "Pharmacology & Toxicology",
    "Review and Exam Preparation": "General & Internal Medicine",
    "Developmental Biology": "Molecular & Cell Biology",
    "Cancer Research": "Oncology",

    # ── Additional unmapped categories ────────────────────────────────────
    "Acoustics and Ultrasonics": "Physics",
    "Architecture": "Arts & Humanities",
    "Biochemistry (medical)": "Biochemistry",
    "Care Planning": "Nursing",
    "Clinical Biochemistry": "Biochemistry",
    "Complementary and Alternative Medicine": "General & Internal Medicine",
    "Computational Mechanics": "Mechanical & Aerospace Engineering",
    "Computers in Earth Sciences": "Earth Science",
    "Control and Optimization": "Mathematics",
    "Dental Assisting": "Dentistry",
    "Dental Hygiene": "Dentistry",
    "E-learning": "Education",
    "Earth-Surface Processes": "Earth Science",
    "Economic Geology": "Earth Science",
    "Embryology": "Molecular & Cell Biology",
    "Emergency Medical Services": "Critical Care & Emergency Medicine",
    "Fundamentals and Skills": "Nursing",
    "Histology": "Pathology",
    "Medical Assisting and Transcription": "Health Professions",
    "Medical Terminology": "Health Professions",
    "Molecular Medicine": "Molecular & Cell Biology",
    "Museology": "Arts & Humanities",
    "Nurse Assisting": "Nursing",
    "Oncology (nursing)": "Nursing",
    "Pathophysiology": "Pathology",
    "Pediatrics": "Pediatrics",
    "Pharmacology (medical)": "Pharmacology & Toxicology",
    "Pharmacology (nursing)": "Nursing",
    "Pharmacy": "Pharmacology & Toxicology",
    "Public Administration": "Political Science",
    "Radiation": "Radiology & Imaging",
    "Reproductive Medicine": "Obstetrics & Gynecology",
    "Research and Theory": "Nursing",
    "Reviews and References (medical)": "General & Internal Medicine",
    "Social Work": "Sociology & Social Sciences",
    "Sports Science": "Orthopedics & Sports Medicine",
    "Surfaces and Interfaces": "Materials Science",
}


def parse_sjr(val):
    """Parse SJR value which may use comma as decimal separator."""
    if pd.isna(val):
        return 0.0
    return float(str(val).replace(",", "."))


def parse_primary_category(categories_str):
    """Extract the first (primary) category name from SJR categories string.
    Format: 'Category1 (Q1); Category2 (Q2); ...'
    """
    if pd.isna(categories_str):
        return None
    first = str(categories_str).split(";")[0].strip()
    # Remove quartile suffix like " (Q1)"
    name = re.sub(r"\s*\(Q[1-4]\)\s*$", "", first).strip()
    return name if name else None


def map_category_to_field(category):
    """Map a Scopus category to our broader field. Returns None if unmapped."""
    if category is None:
        return None
    return CATEGORY_TO_FIELD.get(category)


# Publishers that NEVER deposit Crossref assertion dates (0% hit rate in Phase 1)
CROSSREF_SKIP_PUBLISHERS = [
    "Institute of Electrical and Electronics Engineers",
    "Annual Reviews",
    "American Chemical Society",
    "American Medical Association",
    "Emerald Group Publishing",
    "American Economic Association",
    "INFORMS",
    "University of Chicago Press",
    "Institute of Mathematical Statistics",
    "Copernicus Publications",
    "Now Publishers",
]

# Biomedical SJR areas — journals with these are worth querying PubMed
BIOMEDICAL_AREAS = {
    "Medicine", "Biochemistry, Genetics and Molecular Biology",
    "Immunology and Microbiology", "Neuroscience",
    "Pharmacology, Toxicology and Pharmaceutics",
    "Health Professions", "Nursing", "Dentistry", "Veterinary",
    "Agricultural and Biological Sciences",
}


def should_query_crossref(publisher):
    """Check if a publisher is known to never deposit Crossref assertion dates."""
    if pd.isna(publisher):
        return True  # unknown publisher, try anyway
    pub_lower = str(publisher).lower()
    for skip in CROSSREF_SKIP_PUBLISHERS:
        if skip.lower() in pub_lower:
            return False
    return True


def should_query_pubmed(areas_str):
    """Check if a journal's SJR areas overlap with biomedical fields."""
    if pd.isna(areas_str):
        return False
    areas = str(areas_str)
    for bio_area in BIOMEDICAL_AREAS:
        if bio_area.lower() in areas.lower():
            return True
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true",
                        help="Include ALL SJR journals (not just top 15/field)")
    args = parser.parse_args()

    data_dir = Path("data")
    sjr_file = data_dir / "scimagojr_2024.csv"

    if not sjr_file.exists():
        print(f"ERROR: {sjr_file} not found. Download from scimagojr.com first.")
        sys.exit(1)

    print("Loading SJR data...")
    df = pd.read_csv(sjr_file, sep=";")
    print(f"  Loaded {len(df)} records")

    # Filter to journals only (exclude book series, conferences, trade journals)
    df = df[df["Type"] == "journal"].copy()
    print(f"  {len(df)} journals after filtering")

    # Parse SJR scores
    df["sjr_score"] = df["SJR"].apply(parse_sjr)

    # Parse primary category
    df["primary_category"] = df["Categories"].apply(parse_primary_category)

    # Map to field
    df["field"] = df["primary_category"].apply(map_category_to_field)

    # Report unmapped categories
    unmapped = df[df["field"].isna() & df["primary_category"].notna()]["primary_category"].unique()
    if len(unmapped) > 0:
        print(f"\n  WARNING: {len(unmapped)} unmapped categories:")
        for cat in sorted(unmapped):
            count = len(df[df["primary_category"] == cat])
            print(f"    - {cat} ({count} journals)")

    # For --full mode, keep journals even without field mapping (assign "Unmapped")
    if args.full:
        df.loc[df["field"].isna(), "field"] = "Unmapped"
        print(f"  {len(df)} journals total (full mode, unmapped → 'Unmapped')")
    else:
        df = df[df["field"].notna()].copy()
        print(f"  {len(df)} journals with field mapping")

    # Parse ISSNs — store both print and electronic ISSN
    def format_issn(raw):
        """Format a raw ISSN like '15424863' to '1542-4863'."""
        raw = raw.strip()
        if len(raw) == 8 and "-" not in raw:
            return raw[:4] + "-" + raw[4:]
        return raw

    def parse_issns(issn_str):
        """Return (issn1, issn2) from SJR format like '15424863, 00079235'."""
        if pd.isna(issn_str):
            return "", ""
        parts = [p.strip() for p in str(issn_str).split(",")]
        issn1 = format_issn(parts[0]) if len(parts) > 0 and parts[0] else ""
        issn2 = format_issn(parts[1]) if len(parts) > 1 and parts[1] else ""
        return issn1, issn2

    issn_pairs = df["Issn"].apply(parse_issns)
    df["issn_clean"] = issn_pairs.apply(lambda x: x[0])
    df["issn_alt"] = issn_pairs.apply(lambda x: x[1])

    if args.full:
        # Full mode: include ALL journals with query flags
        results = []
        for _, row in df.iterrows():
            results.append({
                "field": row["field"],
                "field_category": row["primary_category"] if pd.notna(row["primary_category"]) else "",
                "journal_name": row["Title"],
                "issn": row["issn_clean"],
                "issn_alt": row["issn_alt"],
                "publisher": row["Publisher"],
                "sjr_rank": row["Rank"],
                "sjr_score": row["sjr_score"],
                "areas": row["Areas"],
                "query_crossref": should_query_crossref(row["Publisher"]),
                "query_pubmed": should_query_pubmed(row["Areas"]),
            })

        result_df = pd.DataFrame(results)
        # Drop journals with no ISSN (can't query without it)
        result_df = result_df[result_df["issn"] != ""].copy()

        out_file = data_dir / "journal_list_full.csv"
        result_df.to_csv(out_file, index=False)

        fields = sorted(result_df["field"].unique())
        n_crossref = result_df["query_crossref"].sum()
        n_pubmed = result_df["query_pubmed"].sum()
        print(f"\n  Saved {len(result_df)} journals across {len(fields)} fields to {out_file}")
        print(f"  Query Crossref: {n_crossref} journals")
        print(f"  Query PubMed: {n_pubmed} journals")
        print(f"  Skip both: {len(result_df) - len(result_df[result_df['query_crossref'] | result_df['query_pubmed']])} journals")

    else:
        # Original mode: top 15 per field
        TOP_N = 15
        results = []

        fields = sorted(df["field"].unique())
        print(f"\n  {len(fields)} fields identified")

        for field in fields:
            field_df = df[df["field"] == field].sort_values("sjr_score", ascending=False)
            top = field_df.head(TOP_N)
            for _, row in top.iterrows():
                results.append({
                    "field": field,
                    "field_category": row["primary_category"],
                    "journal_name": row["Title"],
                    "issn": row["issn_clean"],
                    "issn_alt": row["issn_alt"],
                    "publisher": row["Publisher"],
                    "sjr_rank": row["Rank"],
                    "sjr_score": row["sjr_score"],
                    "areas": row["Areas"],
                })

        result_df = pd.DataFrame(results)
        out_file = data_dir / "journal_list.csv"
        result_df.to_csv(out_file, index=False)

        print(f"\n  Saved {len(result_df)} journals across {len(fields)} fields to {out_file}")
        print(f"\n  Field breakdown:")
        for field in fields:
            n = len(result_df[result_df["field"] == field])
            print(f"    {field}: {n} journals")


if __name__ == "__main__":
    main()
