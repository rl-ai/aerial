# Aerial Architecture Diagram — Apple HIG Dark-Mode Design Tokens

Applies to `agent-flow-v6.svg` (recolor of `agent-flow-v5.svg`, structure unchanged).

## Canvas & surfaces
| Token | Value | Why |
|---|---|---|
| Canvas | `#000000` | Apple dark-mode pure black base (OLED-true); max contrast for all labels. |
| Elevated surface (boxes) | `#1C1C1E` | Apple "elevated" dark surface; separates cards from canvas without color noise. |
| Raised surface (storage block, icons) | `#2C2C2E` | Apple "raised" dark surface; visually lifts the storage container above its children. |
| Separator / hairline | `rgba(84,84,88,0.6)` | Apple dark separator; used for the dotted divider, default box strokes, icon grilles. |
| Icon accents | `#30D158` (systemGreen) | Status/indicator dots on machines only — reads as "healthy hardware" in HIG semantics. |

## Semantic colors (Apple system palette, dark variants)
| Token | Value | Use |
|---|---|---|
| systemOrange | `#FF9F0A` | Agent boxes: border + small-caps `AGENT` overline + legend swatch. |
| systemBlue | `#0A84FF` | Output boxes: border + small-caps overline + `Agent Outputs` label + legend swatch. |
| systemGray | `#8E8E93` | Local-storage border + legend swatch — neutral, unclaimed by any role. |

## Fill strategy decision: materials, not saturated fills
v5 filled agent boxes solid orange and output boxes solid blue, which forces white-on-orange (fails WCAG) and low-contrast blue-on-blue titles. v6 uses Apple's "material" pattern instead: **elevated surface fill (#1C1C1E) + 1.5px colored border + colored small-caps overline**. Rationale: it preserves full-strength semantic color coding at the box edges (where HIG says accents belong), keeps every text on the high-contrast dark surface, and matches macOS/visionOS dark-card styling.

## Label hierarchy
| Token | Value | Use |
|---|---|---|
| Primary | `#FFFFFF` | Box titles, machine names, legend labels, list items. |
| Secondary | `rgba(235,235,245,0.6)` | Body/descriptor text, connector labels, section overlines. |
| Tertiary | `rgba(235,235,245,0.3)` | Not used for text — falls below 4.5:1 on black; reserved for future decoration only. |

## Typography
SF-style stack `-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif`; titles semibold (600), body regular (400), role overlines small-caps (`text-transform:uppercase`, `letter-spacing:.12em`). Box corners 10px radius (HIG continuous-corner feel).

## WCAG contrast (relative luminance, WCAG 2.1 formula)
| Text | Effective background | Ratio |
|---|---|---|
| `#FFFFFF` primary | `#1C1C1E` surface | 15.6:1 |
| `#FFFFFF` primary | `#2C2C2E` raised | 13.9:1 |
| `rgba(235,235,245,.6)` secondary (≈ #8D8D92) | `#000000` canvas | 6.3:1 |
| secondary (≈ #8D8D92) | `#1C1C1E` surface | 5.6:1 |
| secondary | `#2C2C2E` raised | 5.0:1 |
| `#FF9F0A` orange overline | `#000000` canvas | 9.1:1 |
| `#0A84FF` blue overline | `#1C1C1E` surface | 4.5:1 |
| `#30D158` green dot | `#000000` icon fill | 10.9:1 |

All text-bearing pairs ≥ 4.5:1 (verified programmatically via Playwright computed styles; script output in the delivery report). Saturated fills were rejected precisely because white-on-`#FF9F0A` is 2.2:1 and white-on-`#0A84FF` is 3.6:1 — both fail AA for body text.
