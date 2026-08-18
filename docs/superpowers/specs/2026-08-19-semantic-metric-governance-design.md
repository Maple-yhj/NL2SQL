# Semantic Metric Governance Design

Status: implementation baseline
Source: `docs/semantic-metric-hybrid-implementation-plan.docx`

## Decision

Metric execution authority is independent from relationship binding authority.
Domain packs, web research, and language models may propose definitions, but
only an immutable published metric set or a validated, user-confirmed overlay
may enter query planning and compilation.

## Non-negotiable invariants

1. Metric formulas use the finite V2 AST. Arbitrary SQL is never accepted.
2. Every definition references logical fields only. Physical identifiers remain
   inside the activated relationship binding.
3. A metric set pins tenant, datasource snapshot, schema fingerprint, domain,
   relationship binding, metric-set version, and content digest.
4. Published metric-set versions are immutable. Moving the active pointer does
   not rewrite or retire an older exact version. Revocation is explicit.
5. An overlay pins its proposal revision and digest, validation report digest,
   base metric context, confirming user, scope, and expiry.
6. A running query never silently changes metric authority. Confirmation or
   publication produces new pins and therefore a new run.
7. Web content and model output are untrusted proposal inputs. Neither is an
   approver or an execution authority.
8. Existing embedded V1 metrics remain readable through `LegacyMetricAdapter`.
   They are not rewritten in place.

## V2 expression boundary

Allowed value operations:

- logical field and typed literal;
- add, subtract, multiply, divide, and negate;
- coalesce, nullif, decimal cast, and absolute value.

Allowed predicates:

- typed comparisons;
- `in` and `not_in` with bounded literal lists;
- null predicates;
- bounded boolean composition.

Allowed formulas:

- count, count distinct, sum, average, minimum, maximum, and median;
- arithmetic between aggregate formulas;
- numeric constants.

AST depth, node count, argument count, list length, literal size, and floating
point finiteness are bounded before compiler lowering.

## Control-plane state

The additive control-plane schema contains:

- `control_schema_migrations`;
- `metric_proposals`;
- `metric_validation_reports`;
- `metric_sets` and `active_metric_sets`;
- `metric_overlays`;
- `conversation_metric_pins`;
- `domain_pack_assignments`;
- `semantic_audit_events`.

Proposal, assignment, pointer, and conversation-pin updates use compare-and-set
revisions. Validation reports, audit events, and metric-set versions are
append-only. Datasource deletion removes tenant-accessible semantic metadata in
the same SQLite control-plane transaction.

## Runtime resolution order

1. Resolve exact ref, display name, or synonym in the pinned effective catalog.
2. If unresolved, consult the assigned domain pack.
3. If still unresolved and tenant policy permits, perform controlled research.
4. Ground candidates against the activated logical schema.
5. Run deterministic static and preview validation.
6. Require the appropriate user confirmation or steward approval.
7. Start a new run using the resulting metric-set and overlay pins.

## Initial GMV acceptance case

The commerce pack must compare at least `SUM(price)`,
`SUM(price + freight_value)`, and `SUM(payment_value)`. It must not select one
without resolving amount basis, event time, cancellations, refunds, currency,
and grain. Item/payment fan-out must be rejected before executable SQL is
created.

## Rollout

Rollout proceeds through `off`, `shadow`, `assist`, and `governed`. Network
discovery remains independently disabled by default. Moving to the next mode
requires contract, migration, compatibility, security, and semantic-evaluation
gates to pass.
