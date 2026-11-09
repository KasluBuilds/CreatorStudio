# Asset Agent — Discovery, Generation, Rights and Organization

**Status:** Separate Asset Acquisition service is DECIDED; detailed policy and implementation are PROPOSED. An offline foundation (`orchestrator/assets/`: metadata schema, fail-closed rights engine, library) is implemented and unit-tested on synthetic data; there is no provider, downloader or automated licence verification (M-AST-01 in `PROJECT_STATUS.json`).

## 1. Mission

Acquire or create supporting material that improves clarity, demonstration, context, humor or visual interest without introducing rights or provenance problems.

## 2. Asset types

- B-roll and stock footage.
- Images and illustrations.
- Diagrams, charts and maps.
- Motion graphics and titles.
- Sound effects and music.
- Memes and reaction visuals.
- Generated images/video/graphics.
- Creator-owned supplementary footage.

## 3. Rights policy

Auto-eligible only when the rights basis is known and compatible with the intended use, such as:
- Creator-owned material.
- Public-domain material with verified status.
- Properly licensed material whose terms allow the intended use.
- Creative Commons material whose exact license and attribution requirements are compatible.
- Generated assets where the generation provider’s terms permit the intended use.

Reject or quarantine:
- Unknown-license assets.
- Watermarked previews.
- Assets whose license forbids commercial use when the project may be monetized.
- Assets with incompatible attribution/share-alike/derivative restrictions.
- Material whose source cannot be established.

No watermark does not mean free to use. Publicly viewable does not mean reusable. The creator’s willingness to remove a video after a takedown does not make an infringement safe or authorized.

## 4. Asset manifest

Recommended fields:
- `asset_id`
- `filename` and local path
- `asset_type`
- `source_url` or provider
- `creator/author`
- `retrieved_at`
- `license_name`
- `license_url`
- `commercial_use_allowed`
- `modification_allowed`
- `attribution_required`
- `attribution_text`
- `restrictions`
- `intended_project`
- `intended_use`
- `rights_status`
- `notes`

## 5. Asset selection

Choose assets that:
- Directly support the point being made.
- Match the tone and creator’s style.
- Are technically suitable for the timeline.
- Do not distract from important speech or demonstrations.
- Have appropriate resolution, aspect ratio and audio quality.
- Can be cropped or reframed without losing essential information.

Do not insert B-roll simply to cover every cut.

## 6. Storage

Suggested project structure:
```text
Assets/
  Images/
  Videos/
  SoundEffects/
  Music/
  Graphics/
  Memes/
  Manifests/
```

Project-specific downloaded/generated assets should be linked to a manifest and copied or referenced in a predictable location. Avoid duplicate uncontrolled downloads.

## 7. Autonomy

Routine insertion of rights-cleared assets does not require user approval. Unknown or incompatible rights must block use automatically. The agent should report rejected assets and find alternatives rather than silently using them.
