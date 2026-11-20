# Data files

## cpv_2008.json.gz

- Content: Common Procurement Vocabulary (CPV 2008, Regulation (EC) No 213/2008), 9454 codes,
  labels in French and English. Format: `{"retrieved": "YYYY-MM-DD", "codes": [[code, fr, en], ...]}`.
  Codes are stored as 8 digits, without the check digit.
- Source: EU Vocabularies, Publications Office of the European Union, CPV concept scheme
  `http://data.europa.eu/cpv/cpv`, read from the official SPARQL endpoint
  `https://publications.europa.eu/webapi/rdf/sparql` (see https://op.europa.eu/en/web/eu-vocabularies/cpv).
- Retrieved: 2026-11-20, rebuilt with `scripts/build_cpv.py`.
- Reuse terms: EU Publications Office metadata and reference data are reusable under
  Commission Decision 2011/833/EU (CC BY 4.0 compatible); attribution to the
  Publications Office of the European Union, no warranty.
