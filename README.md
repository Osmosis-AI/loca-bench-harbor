# loca-bench-harbor

Generated Harbor tasks for [LOCA-bench](https://github.com/hkust-nlp/LOCA-bench),
one Harbor task per upstream configuration entry
(15 families x 7 EDL levels x 5 seeds = 525).

Generated artifacts only; the generator lives in `Osmosis-AI/harbor` under
`adapters/loca-bench`. Nothing here may be hand-edited: CI re-validates every
tree against the adapter.

## Status

`v1.2.1` is generated against
`ghcr.io/osmosis-ai/loca-bench-runtime@sha256:169db7226a27ee3e1091125d46ffc22cf9a64554216d14c9053ef38ef9cd291c`
(multi-arch index: linux/amd64 + linux/arm64, tag
`loca-8b6fac49-adapter-9e309cad0d97-contract-1`, built by the
`loca-runtime-9e309cad0d97` release run of `Osmosis-AI/harbor`) from
`adapters/loca-bench` at commit `9e309cad0` (harness `1.2.0+loca.8b6fac49`).
This harness grades model-caused API terminations (a context overflow, or an
HTTP 400 after at least one completion) as reward 0 instead of ungraded, sets
`[agent].timeout_sec` per EDL with an inner episode budget the runner enforces
itself, and runs the uv-launched MCP servers from the image's pinned packages.
`v1.2.0` is the same harness built from the adapter commit before the
process-group cleanup fix; prefer `v1.2.1`. The manifest in `manifests/`
records the same values. `v1.1.0` and `v1.0.0` remain valid for the earlier
harness semantics; scores are not comparable across the graded-zero change.

## Layout

```
registry.json                     seven dataset entries; task items are {name, path}
tasks/<task_id>/                  task.toml, instruction.md, tests/test.sh, environment/Dockerfile
manifests/loca-bench-<version>.json  upstream pin, runtime image digest, matrix
```

## Datasets

`loca-bench` 525, `loca-bench-core` 225, `loca-bench-8k` / `-64k` / `-128k` /
`-256k` 75 each, `loca-bench-smoke` 8.

## Consuming

```bash
harbor run --repo Osmosis-AI/loca-bench-harbor@v<version> --dataset loca-bench@<version>
```

Registry task entries carry only `name` and `path`; Harbor binds `git_url` and
the resolved commit at resolve time. The platform sets
`harbor_repo="Osmosis-AI/loca-bench-harbor@v<version>"` and
`harbor_dataset="loca-bench@<version>"`.

## Release procedure

The order is one-way. Never generate against a mutable tag.

1. Release the runtime image from `Osmosis-AI/harbor`: tag the adapter commit
   `loca-runtime-<sha12>` and push the tag. The workflow
   `.github/workflows/loca-bench-runtime-image.yml` builds linux/amd64 and
   linux/arm64 natively, pushes the multi-arch tag and prints the index digest
   in its summary.
2. Take that digest, or resolve it: `docker buildx imagetools inspect <ref> --format '{{.Manifest.Digest}}'`.
3. `scripts/release.sh --runtime-image <ref>@sha256:<digest> --dataset-version <x.y.z> --harbor-src <harbor checkout> --loca-src <pinned LOCA-bench checkout>`
   (this runs `loca-bench generate` then `loca-bench validate`).
4. `git add -A && git commit -m "Release loca-bench <x.y.z>"`
5. `git tag -a v<x.y.z> -m "loca-bench <x.y.z>"`
6. `git push origin main && git push origin v<x.y.z>`

Tags are immutable. A corrected release is a new version, never a moved tag.

## License

MIT. Task metadata derives from LOCA-bench; see `LICENSE`.
