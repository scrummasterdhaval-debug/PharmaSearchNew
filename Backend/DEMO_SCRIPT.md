# PharmaSearch Demo Script

## Overview
This demo shows how PharmaSearch aggregates regulatory data for a single molecule across 50+ global regulators. We'll search for **metformin**, a common diabetes medication available in most markets.

## Pre-Demo Checklist

1. **Data repairs applied**: Run `python services/data_repairs.py` to fix:
   - ATC codes lent between molecules and combinations (805+ rows)
   - MHRA documents filed under wrong molecules
   - All data now shows what its regulator published only

2. **Admin credentials configured** (optional):
   ```bash
   python tools/make_admin_password.py demo_user --write-env
   ```
   Then restart the backend.

## Demo Flow

### Step 1: Search for Metformin
- Go to the home page
- Type "metformin" in the search box
- Click Search

**What to expect**: Results from 30+ regulators including:
- **FDA** (multiple formulations, NDC numbers)
- **EMA** (EU approvals, marketing authorization numbers)
- **MHRA** (UK registrations with documents)
- **AIFA** (Italy) and **ANVISA** (Brazil) open registers
- **National registers** from 15+ European countries
- **Asia-Pacific**: Japan (MHLW), Australia (TGA), New Zealand (Medsafe)

### Step 2: Filter & Sort
- Observe results are grouped by molecule
- Click "Strength" or "Registration Date" to sort
- All results are for pure metformin (no combinations)

**Verification points**:
- ✓ No "Janumet" (metformin+sitagliptin) in results
- ✓ No combinations bleeding into search
- ✓ ATC code: A10BA02 for all rows

### Step 3: Export
- Click "Export as Excel"
- Open the file

**Verification points**:
- ✓ No "Germany" or "BfArM" rows (view-only, not exported)
- ✓ Every row has a regulator and registration number
- ✓ Dates are formatted consistently across all sources

### Step 4: Document Inspection (MHRA)
- Find a UK (MHRA) result with "SPC" or "PIL" documents
- Click the document link

**Verification points**:
- ✓ Document opens (MHRA link is current)
- ✓ Document is for metformin (not another molecule like amlodipine)

## Known Limitations

- **Germany (BfArM)**: View-only (terms forbid storing/passing on)
  - Shows in results with "View only" badge
  - Not included in exports
  
- **Pakistan**: Blocked (403 error on DRAP website)
  
- **Bangladesh**: Deferred (expired TLS certificate on DGDA)

- **Admin UI**: Requires sign-in; can only be verified indirectly until deployed

## Data Sources by Region

| Region | Primary Sources |
|--------|-----------------|
| **US** | FDA (NDC), FDA Orange Book, Drugs@FDA |
| **EU** | EMA, MHRA (UK), AIFA (Italy), + 13 national registers |
| **Asia-Pacific** | Japan (MHLW), Australia (TGA), NZ (Medsafe), NMPA (China), Thailand FDA |
| **Middle East** | SFDA (Saudi), MoPH (Lebanon), Israel |
| **Americas** | FDA, Health Canada, ANVISA (Brazil), INVIMA (Colombia) |
| **Central Asia** | NDDA (Kazakhstan) |

## Talking Points

1. **Global coverage**: 50+ regulators in 60+ countries
2. **Regulator-published only**: Every row is from the regulator itself, no guesses or lending across borders
3. **Quality control**: Combinations never bleed into single-molecule searches; ATC codes are corrected
4. **Live documents**: MHRA and other sources provide real-time links to official documents
5. **Export-ready**: Excel exports include all fields, consistent formatting, no view-only rows

## Troubleshooting

- **Search returns nothing**: May need to rebuild indexes. Run startup script.
- **MHRA links are dead**: A scheduled repair runs weekly (Windows task "PharmaSearch weekly MHRA links")
- **Combination appears in results**: Data repairs may not have been run; execute `python services/data_repairs.py`

---

**Last updated**: 2026-09-22
**Status**: Ready for driven demo on single molecules
