# Vendored flagd evaluator conformance suite

These files are copied verbatim from the OpenFeature flagd test bed:

- Repository: https://github.com/open-feature/flagd-testbed
- Tag: `v3.10.2`
- Commit: `31de75b6466e4eff5f13b8bf3b487db90011d8b0`
- Paths: `evaluator/flags/testkit-flags.json` and `evaluator/gherkin/*.feature`

The flagd test bed is part of the OpenFeature project, a Cloud Native Computing Foundation project whose
contributions are licensed under the Apache License 2.0 (the organization's license is reproduced in `LICENSE`).
The repository itself does not carry a license file.

Firefly runs every scenario except those tagged `@fractional-v1`, the legacy float bucketing that the reference
evaluator (`openfeature-flagd-core` 1.0.0) no longer implements. Do not edit these files: update them by copying a
newer tag in full, regenerating `../MANIFEST.sha256`, and doing the same in the other framework.
