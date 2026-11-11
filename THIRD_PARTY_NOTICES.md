# Third-party notices

## FastTrack formatting preset

- `src/autoresearch/skills/manuscript/template-spec.json` is a local,
  machine-readable formatting specification (section order, word limits, and
  similar structural rules) used by the optional `fasttrack` venue profile.
- The third-party `FastTrack-Academic-paper-template.pdf` teaching template is
  **not included** in this repository.
- The default manuscript venue is `generic_journal`; the pipeline does not
  require the `fasttrack` profile or the PDF.

## Python dependencies

Runtime and test dependencies are declared in `pyproject.toml` and installed
from PyPI by the user. They are not vendored in this repository. Refer to each
package for its license.

## Scholarly metadata sources

The pipeline queries public APIs (Semantic Scholar, OpenAlex, Crossref, arXiv,
OpenReview). Their terms of use and rate limits apply to users of this
software. No source data is bundled.

## Project license

The AutoResearch source is distributed under the MIT License. See `LICENSE`
for the original copyright line. The ED queue optimization packaging and
documentation are provided under the same MIT terms; see `NOTICE`.
