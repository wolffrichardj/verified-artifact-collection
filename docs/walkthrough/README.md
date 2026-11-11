# Verified Artifact Collection walkthrough

An 80-second presentation of genuinely recorded local output, with synthetic fixtures. Neutral synthetic narration uses the installed macOS Samantha voice, not Richard's voice. JSON fields are selected/reflowed; diagrams explain boundaries and excerpts identify public source.

## Reproduce the source behavior

Requires Python 3.11+ and Git. Run `sh reproduce.sh` from this directory. The script clones public source into a fresh temporary workspace, checks out the exact revision, runs the source tests and scenario assertions, then prints/retains its workspace for inspection. It does not change an existing checkout or delete the retained workspace.

Repository: https://github.com/wolffrichardj/verified-artifact-collection

Revision: `df5efa8c1cc118a12746e69e05c9537317c577dc`

The exact commands are in `reproduce.sh`; actual recorded results and shot mapping are in `evidence.json`. `source-manifest.json` contains the pinned source-file hashes. Scripts reproduce source behavior, not bit-identical audiovisual rendering. Production used Python/Pillow, local macOS speech synthesis and ffmpeg; cue timing and render checks are retained in the production evidence. No paid service was used.

## Boundaries

This offline generated-record sample binds request identity and exact bytes before landing. The sink reuses identical bytes, preserves changed content as a full-hash variant and checks final disk readback. A separate read-only component reopens the physical file and checks size/hash. The MCP-shaped wrapper is an in-process mock, not a real MCP server or authenticated provider session. Hash consistency does not prove origin or future integrity. Required POSIX primitives and a trusted user-owned root limit portability; hostile ancestor replacement/concurrent mutation are outside scope. A post-publication fault can produce landing_uncertain and preserve the candidate; covered by source tests, not demonstrated in the film. SignatureVerifier is not a cryptographic signature validator.

## Attribution

Richard Wolff's selected personal acquisition/verification code supplied the source. Adaptation, hardening and review used AI assistance; commit identity does not establish per-line human authorship. See the public repository's PROVENANCE.md and MIT license. No adoption, performance, production deployment or business outcome is claimed.

## Files

`walkthrough.mp4` has burned captions and an optional embedded English subtitle track. `captions.srt`/`captions.vtt` and `transcript.txt` provide reusable text. The local index plays the media without external dependencies.
