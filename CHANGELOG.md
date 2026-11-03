# Changelog

## 0.3.0 — 2026-11-04

- Refresh supported model schemas and exact USD prices from the current PopNow catalog.

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-11-01

The PopNow API bills in exact USD from its own API balance, separate from website credits. The add-on now shows, confirms
and records every amount in US dollars.

### Changed

- Quotes are read from `maxChargeUsd` / `expectedChargeUsd`, and createTask confirms the hold with `expectedCostUsd` set
  to the quote's `maxChargeUsd`, unchanged, instead of `expectedCredits`. Holds and charges come from `estimatedCostUsd`
  and `costUsd`; the balance from `availableUsd` / `heldUsd`.
- All money is handled with `decimal.Decimal` (new `popnow/core/money.py`), never floats, including the local estimates,
  which now come out in exact USD (for example $0.1097 instead of 110 credits).
- The panel, confirm button, task list, status lines and refund notes show US dollars. The **+** button next to the
  balance opens https://popnow.ai/api/console/billing; insufficient balance (`40201`) points there too, and a spend cap
  (`40202`) gets its own message.
- "Start without asking up to" is now a US dollar amount (default $0.00, up to $5.00). A threshold saved in credits by
  0.1.x is moved over once (1 credit = $0.001, rounded down to the cent).
- `scripts/sync_models.py` keeps the catalog's USD price table (`usd`) next to the credit table when the catalog sends
  one, and the estimator prefers it.
- README, llms.txt and the in-Blender self-test use USD, link API pricing and API billing, and point to the developer
  MCP server instead of the consumer one.
- The fake backend in `tests/` answers in the current USD shape, with a legacy mode for the older fields.
- `User-Agent: PopNow-Blender/0.2.0`.

### Deprecated

- The credit fields on `Job` (`max_credits`, `expected_credits`, `estimate`, `credits`), `Estimate.credits`, the
  `expected_credits` argument of `PopNowClient.create_task` and the `auto_confirm_below` argument of `JobRunner`. They
  keep working: task history written by 0.1.x still loads and shows its amounts in USD, and credit fields from an
  older server are used only when no USD field is present.
- Earlier, unreleased: bundled prices followed the 2026-10-19 credit unit change (1 credit = $0.001).

## [0.1.0] - 2026-10-16

### Added

- **Render with AI:** clay render from the camera, the viewport or the last F12 render, optional depth / normal / line-art structure pass, up to three reference images, eight style presets.
- **Animate with AI:** Workbench preview of the camera animation as a reference clip, motion transfer onto a character with Wan 2.2 Animate, results ready for the Video Sequencer.
- **Texture:** prompt-to-texture with local seamless blending and a box-projected material.
- Live price estimates, exact quotes with a confirm step, automatic refunds reported for failed tasks.
- Background jobs with a Tasks panel, stop and resume, and task history that survives restarts.
- Seventeen bundled model endpoints across the three workflows.
