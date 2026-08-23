# artifacts/profiles/

Per-hardware-profile measurements of the duration constants, produced by
`../scripts/measure_profiles.py`, and the derivation that turns them into
the values, anchor, and bands in `src/blastoise/verdict/constants.py`.

| file | what it is |
|---|---|
| `laptop-nvme.json` | the anchor profile (2026-08-23): every representative statement at 1k/100k/1M/10M, two passes, with the calibration probe read before and after each pass |
| `derived.json` / `derived.txt` | `../scripts/derive_constants.py --anchor laptop-nvme` over every profile file here: per-profile at-scale values, probe readings, probe-scaled values, and the cross-profile spread |

**Only one profile is here.** The replicated measurement across three
cloud profiles (a small burstable, a compute-optimized, and a
storage-optimized instance) is what the band is supposed to be derived
from; it has not run — see the 2026-08-23 `DECISIONS.md` section for why
and for what it takes. The runbooks are ready:

- `../scripts/run_profiles_gce.sh` — three GCE instances, one command,
  after `gcloud auth login`.
- `../scripts/measure_profiles_workflow.yml` — the same measurement on
  GitHub-hosted runners of distinct hardware (x86, ARM64, Apple silicon,
  Windows), for a private repo holding the measurement bundle.

When profile files land here, re-run the derivation, set each constant's
`profiles`, `per_profile`, and `cross_profile_spread_tenths` from
`derived.json`, and the boundary rule will use the observed cross-profile
spread instead of the single-profile floor.
