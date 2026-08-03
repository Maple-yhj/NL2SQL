---
name: Data Assistant
description: "A governed analytics workspace that makes every answer traceable from data to evidence."
colors:
  paper: "#f4f7f9"
  surface: "#ffffff"
  surface-soft: "#f8fafc"
  ink: "#102a43"
  ink-strong: "#071a2b"
  muted: "#5e7184"
  subtle: "#8a9aaa"
  line: "#dbe4ea"
  line-strong: "#c7d4dd"
  cobalt: "#2563eb"
  cobalt-deep: "#174fd0"
  cobalt-soft: "#eaf1ff"
  governed-green: "#0f9675"
  governed-green-soft: "#e8f7f2"
  warning: "#d08a17"
  warning-soft: "#fff5df"
  danger: "#b42318"
  danger-soft: "#fff0ee"
  navigation-deep: "#0d263b"
  navigation-muted: "#9db0bf"
typography:
  display:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif"
    fontSize: "clamp(48px, 7vw, 88px)"
    fontWeight: 700
    lineHeight: 1.04
    letterSpacing: "-0.035em"
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif"
    fontSize: "clamp(24px, 3vw, 36px)"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "-0.025em"
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif"
    fontSize: "16px"
    fontWeight: 780
    lineHeight: 1.3
    letterSpacing: "-0.02em"
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.68
    letterSpacing: "normal"
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif"
    fontSize: "12px"
    fontWeight: 750
    lineHeight: 1.3
    letterSpacing: "normal"
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 500
    lineHeight: 1.6
    letterSpacing: "normal"
rounded:
  xs: "3px"
  sm: "8px"
  md: "10px"
  lg: "13px"
  xl: "16px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "22px"
  xxl: "28px"
components:
  button-primary:
    backgroundColor: "{colors.cobalt}"
    textColor: "{colors.surface}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "0 14px"
    height: "40px"
  button-primary-hover:
    backgroundColor: "{colors.cobalt-deep}"
    textColor: "{colors.surface}"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "0 10px"
    height: "38px"
  navigation-rail:
    backgroundColor: "{colors.navigation-deep}"
    textColor: "{colors.navigation-muted}"
    width: "76px"
  status-complete:
    backgroundColor: "{colors.governed-green-soft}"
    textColor: "{colors.governed-green}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "0 8px"
    height: "26px"
  result-container:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "0"
---

# Design System: Data Assistant

## Overview

**Creative North Star: "The Evidence Rail"**

Data Assistant is a compact analytical workbench built around a visible route from governed data to a defensible answer. Its atmosphere is calm, precise, and technical without becoming developer-centric: the business conclusion stays dominant while data scope, query details, and runtime proof remain one deliberate action away.

The visual world combines a deep-ink navigation spine with matte white drafting surfaces, cobalt actions, and governed-green status. Decoration is subordinate to state and evidence. Density is purposeful: navigation and controls stay compact, while answers, charts, and tables receive generous reading space.

**Key Characteristics:**

- A persistent five-station route is the system's signature.
- Business answers lead; technical evidence is progressively disclosed.
- Deep navigation, cool paper surfaces, thin rules, and restrained shadows create trustworthy depth.
- Simplified Chinese is the primary product language; necessary technical terms stay contextual.

## Colors

The palette uses one action accent, one governed-state accent, a deep navigation anchor, and a cool neutral surface stack.

### Primary

- **Cobalt Action:** Reserved for primary buttons, active navigation, focus, and interactive route states.

### Secondary

- **Governed Green:** Communicates completed, verified, active, or safe states; it is not used as a generic decorative accent.

### Neutral

- **Deep Navigation Ink:** Anchors the utility rail and login story surface.
- **Drafting Paper:** Forms the application canvas and separates the workbench from pure white data surfaces.
- **Evidence Ink:** Carries high-contrast headings and body content.
- **Cool Rule:** Defines tables, drawers, cards, and section boundaries without heavy framing.

### Named Rules

**The Two-Signal Rule.** Cobalt means “act or select”; governed green means “complete or verified.” Never swap their roles.

**The Quiet Canvas Rule.** Most of the viewport remains neutral. Accent colors identify state and action, not decoration.

## Typography

**Display Font:** Native system sans with Chinese platform fallbacks<br>
**Body Font:** Native system sans with Chinese platform fallbacks<br>
**Label/Mono Font:** System UI for labels; UI monospace for SQL and logical plans

**Character:** The type system is compact and operational. Large display type appears only in the login story; the workspace relies on small, strong labels and highly readable body copy.

### Hierarchy

- **Display** (700, fluid 48–88px, 1.04): Login story only.
- **Headline** (700, fluid 24–36px, 1.25): Empty-state prompts and major first-view messages.
- **Title** (780, 16px, 1.3): Product, panel, and section anchors.
- **Body** (400, 14px, 1.68): Answers and explanatory copy, usually capped near 72 characters.
- **Label** (750, 12px, 1.3): Controls, statuses, table labels, and route stations.
- **Mono** (500, 12px, 1.6): SQL, logical plans, version pins, and identifiers.

### Named Rules

**The Business-First Type Rule.** The answer uses normal reading typography; machine details switch to mono only inside the evidence layer.

## Layout

Desktop uses a 336px navigation shell—76px utility rail plus a 260px conversation index—and a fluid work area. The conversation index can collapse, leaving the 76px rail. The reading column and composer cap at 900px, while the five-station route spans the workspace.

Spacing follows a compact 4/8/12/16/22/28px rhythm. The workspace header and route remain structurally separate from the scrollable conversation; the composer stays at the bottom.

At 920px and below, navigation becomes an off-canvas drawer and the work area owns the full viewport. At 700px and below, route labels compact, the evidence drawer becomes a bottom sheet, datasource sections stack, and wide tables or step rails scroll inside their own containers. The page itself must not gain horizontal overflow.

**The Local Overflow Rule.** Wide evidence, tables, and step rails may scroll inside bounded containers; they must never widen the application shell.

## Elevation & Depth

The system is flat by default and uses thin rules plus tonal surfaces for most separation. Shadows are ambient and limited to floating layers, the sticky composer, account menus, focus treatment, and the login card. No surface uses ornamental beveling or simulated material.

### Shadow Vocabulary

- **Composer Lift** (`0 8px 22px rgb(16 42 67 / 0.1)`): Keeps the input visually available above the thread.
- **Drawer Depth** (`-14px 0 38px rgb(16 42 67 / 0.17)`): Separates evidence from the answer it overlays.
- **Floating Menu** (`0 12px 28px rgb(16 42 67 / 0.16)`): Used for temporary account and conversation actions.

**The Flat-by-Default Rule.** Resting content surfaces use borders and tonal change; shadows belong to floating or focused interaction layers.

## Shapes

The form language uses gently curved operational controls: 8–13px for buttons, fields, compact surfaces, and result containers; 16px for larger messages, composer shells, and mobile sheets. Pills are reserved for compact statuses. Route nodes and avatars are circular because they represent progress or identity, not generic decoration.

## Components

### Buttons

- **Shape:** Compact rounded rectangles, usually 10px.
- **Primary:** Cobalt fill, white label, 40px minimum height.
- **Hover / Focus:** Deepen cobalt on hover; use a 3px translucent cobalt focus ring.
- **Secondary / Ghost:** White or transparent surfaces with cool rules; preserve the same compact height.

### Chips

- **Style:** Soft tonal background, compact label, pill silhouette.
- **State:** Green is reserved for verified completion; danger and warning chips keep text labels so color is never the only signal.

### Cards / Containers

- **Corner Style:** 13px for result surfaces; 16px for prominent floating or mobile surfaces.
- **Background:** White over cool paper.
- **Shadow Strategy:** Flat at rest; see Elevation & Depth for floating layers.
- **Border:** One-pixel cool rule.
- **Internal Padding:** Usually 12–22px depending on density.

### Inputs / Fields

- **Style:** White fill, cool one-pixel stroke, 10px corners.
- **Focus:** Cobalt border plus a restrained translucent focus ring.
- **Error / Disabled:** Error uses danger text and a pale danger surface; disabled controls reduce contrast without removing their label.

### Navigation

The utility rail is deep ink with icon-only actions and a cobalt active state. The conversation index uses compact text, timestamps, search, and contextual actions. On mobile the full navigation moves off canvas behind a visible menu button and dismissible scrim.

### Evidence Rail

Five connected stations express dataset, semantic scope, query, evidence, and answer. Every station combines icon or number, title, and status text. Active progress may animate once; reduced-motion preferences collapse motion to effectively instantaneous state changes.

### Evidence Drawer

The evidence layer is closed by default. It contains data pins, logical plan, SQL, runtime trace, result summary, version pins, proposals, and typed failures in that order. Desktop uses a right drawer; mobile uses a bottom sheet.

## Do's and Don'ts

### Do:

- **Do** keep the active dataset visible before the user asks a question.
- **Do** lead with the business answer, then chart, table, and evidence action.
- **Do** pair every colored status with explicit text or an icon.
- **Do** keep technical identifiers and SQL inside the evidence layer.
- **Do** preserve local scrolling for wide tables and narrow-screen step rails.

### Don't:

- **Don't** turn the workspace into a generic chatbot with hidden execution state.
- **Don't** use governed green for generic calls to action or cobalt as a success color.
- **Don't** open the evidence drawer automatically after a successful run.
- **Don't** introduce decorative gradients, glass effects, excessive pills, or unsupported commercial claims.
- **Don't** let a fixed sidebar or wide table remove functionality on mobile.
