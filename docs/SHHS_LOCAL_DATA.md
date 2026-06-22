# SHHS Local Data Setup

This document describes the safe local setup for authorized SHHS data during
Stage 2. It is intentionally conservative: do not fully extract the dataset at
this stage, and do not commit any raw or derived data files.

## Local Directory Layout

Use `data/` as a local-only sibling of the code repository:

```text
SLEEPAGENT/
  sleepagent/        # project code repository
  yasa/              # local YASA-related code
  data/              # local data directory, not committed to Git
    raw/
      shhs.zip       # raw SHHS archive
      shhs/          # future full extraction directory; currently can be empty
      shhs_sample/   # future 1-3 sample XML/EDF records for smoke tests
    processed/
      sleepagent/    # future preprocessing outputs
    manifests/       # future manifest files
```

For the current workspace, the existing archive at:

```text
/mnt/data4/wz/SleepAgent/shhs.zip
```

should be treated as local raw data. The recommended final location is:

```text
/mnt/data4/wz/SleepAgent/data/raw/shhs.zip
```

Move or symlink it only when you are ready. Do not copy large data into
`sleepagent/`.

## Never Commit Data Files

The following files must remain local and out of Git:

- SHHS zip archives.
- EDF signal files.
- SHHS XML annotation files.
- NPY and NPZ arrays.
- Parquet tables.
- Any derived preprocessing output under `data/processed/`.
- Any generated manifest that might expose local file lists or record metadata,
  unless it has been explicitly reviewed and sanitized.

## Current Stage 2 Rule

Do not fully extract the 140 GB SHHS zip in the current task window.

The only safe check right now is to inspect zip member names so we can confirm
the internal path layout. This reads zip metadata only; it does not extract
files and does not read EDF signal contents.

## Safe Zip Check

From the project code root:

```bash
cd /mnt/data4/wz/SleepAgent/sleepagent
```

If the archive has already been moved to the recommended local data directory:

```bash
SLEEPAGENT_SHHS_ZIP=../data/raw/shhs.zip python scripts/check_shhs_zip.py
```

If the archive is still in the current known location:

```bash
python scripts/check_shhs_zip.py /mnt/data4/wz/SleepAgent/shhs.zip
```

The command prints only a small number of `.edf` and `.xml` member names and
then exits. It does not extract any data.

## Next Safe Data Step

After confirming the zip internal paths, extract only 1-3 matched XML/EDF sample
records into:

```text
/mnt/data4/wz/SleepAgent/data/raw/shhs_sample/
```

Those samples are for local smoke tests only and must still remain out of Git.
Full SHHS extraction should wait until the preprocessing contracts and manifest
format are settled.

For a single local smoke-test record, use exact zip member names:

```bash
unzip -n ../data/raw/shhs.zip \
  polysomnography/edfs/shhs1/shhs1-200001.edf \
  polysomnography/annotations-events-nsrr/shhs1/shhs1-200001-nsrr.xml \
  polysomnography/annotations-events-profusion/shhs1/shhs1-200001-profusion.xml \
  -d ../data/raw/shhs_sample
```

This preserves the `polysomnography/...` layout under `shhs_sample/`, so local
path discovery can treat `../data/raw/shhs_sample` as a tiny SHHS-like root.

To print the current XML-derived summary for the local sample:

```bash
python scripts/summarize_shhs_sample.py \
  --root ../data/raw/shhs_sample \
  --record-id shhs1-200001
```

The summary reports XML metadata and mapped label counts only. It checks the EDF
path exists but does not read EDF signal contents.

To print the minimal Stage 2 preprocessing manifest schema:

```bash
python scripts/summarize_shhs_sample.py \
  --root ../data/raw/shhs_sample \
  --record-id shhs1-200001 \
  --manifest
```

This manifest is a local smoke-test aid. Do not commit manifests that expose raw
local file paths or record metadata unless they have been explicitly reviewed
and sanitized.

To explicitly write the local manifest under `../data/manifests/`:

```bash
python scripts/summarize_shhs_sample.py \
  --root ../data/raw/shhs_sample \
  --record-id shhs1-200001 \
  --manifest \
  --write-manifest \
  --manifest-dir ../data/manifests
```

The command prints the written JSON path. The default filename includes the
manifest schema version, record id, and UTC timestamp.

To validate a local manifest JSON file:

```bash
python scripts/summarize_shhs_sample.py \
  --validate-manifest ../data/manifests/<manifest>.json
```

Validation checks the schema version, required fields, record count, annotation
summary shape, and safety notes. It does not read raw EDF/XML files.
