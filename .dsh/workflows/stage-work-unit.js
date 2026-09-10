// RoutePilot work-unit loop — BUILDER -> REVIEWER with a bounded REVISE cycle.
//
// The Supervisor passes this file's body verbatim to the `workflow` tool (docs/WORKFLOW.md) with:
//
//   args = {
//     unit_id:        string  authorised work-unit identifier
//     brief:          string  what the Builder implements (deliverable, governing decisions,
//                             targeted verification, hard boundary)
//     reviewer_brief: string  the contract the Reviewer checks the delivered tree against
//     repo:           string  absolute repository path
//   }
//
// and meta.phases = [{title: 'build'}, {title: 'review'}].
//
// Local limits (owner policy — never rely on the harness's global maxTotalAgents):
//   MAX_REVISE_CYCLES = 3  -> at most 3 Builder attempts and 3 Reviewer attempts per work unit.
// A null or throwing child counts as a failed cycle and is handled explicitly; there is no retry
// storm and no unbounded loop.
//
// Write safety: every Reviewer verdict carries a workspace fingerprint taken before and after the
// review. A PASS is rejected when the Reviewer changed the tree, or when its "before" value does
// not match the fingerprint the Builder reported.

const MAX_REVISE_CYCLES = 3;

const builderSchema = {
  type: 'object',
  properties: {
    files_changed: { type: 'array', items: { type: 'string' } },
    implementation_summary: { type: 'string' },
    governing_spec_decisions: { type: 'array', items: { type: 'string' } },
    verification_commands: { type: 'array', items: { type: 'string' } },
    verification_results: { type: 'array', items: { type: 'string' } },
    benchmarks: { type: 'array', items: { type: 'string' } },
    blockers: { type: 'array', items: { type: 'string' } },
    workspace_fingerprint: { type: 'string' },
    git_status: { type: 'string' },
    notes: { type: 'string' },
  },
  required: [
    'files_changed',
    'implementation_summary',
    'governing_spec_decisions',
    'verification_commands',
    'verification_results',
    'benchmarks',
    'blockers',
    'workspace_fingerprint',
    'git_status',
    'notes',
  ],
  additionalProperties: false,
};

const reviewerSchema = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['PASS', 'REVISE', 'ESCALATE_TO_OWNER'] },
    workspace_fingerprint_before_review: { type: 'string' },
    workspace_fingerprint_after_review: { type: 'string' },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          issue: { type: 'string' },
          evidence: { type: 'string' },
          governing_spec_or_decision: { type: 'string' },
          required_fix: { type: 'string' },
          required_verification: { type: 'string' },
        },
        required: [
          'issue',
          'evidence',
          'governing_spec_or_decision',
          'required_fix',
          'required_verification',
        ],
        additionalProperties: false,
      },
    },
    escalation: {
      type: 'object',
      properties: {
        question: { type: 'string' },
        options: { type: 'array', items: { type: 'string' } },
        recommendation: { type: 'string' },
        why_this_requires_owner_judgment: { type: 'string' },
        what_depends_on_the_answer: { type: 'string' },
      },
      required: [
        'question',
        'options',
        'recommendation',
        'why_this_requires_owner_judgment',
        'what_depends_on_the_answer',
      ],
      additionalProperties: false,
    },
    notes: { type: 'string' },
  },
  required: [
    'verdict',
    'workspace_fingerprint_before_review',
    'workspace_fingerprint_after_review',
    'issues',
    'escalation',
    'notes',
  ],
  additionalProperties: false,
};

const input = (typeof args === 'object' && args !== null) ? args : {};
const unitId = (typeof input.unit_id === 'string' && input.unit_id.length > 0)
  ? input.unit_id
  : 'unnamed-unit';
const repo = (typeof input.repo === 'string' && input.repo.length > 0) ? input.repo : '.';
const brief = (typeof input.brief === 'string') ? input.brief : '';
const reviewerBrief =
  (typeof input.reviewer_brief === 'string' && input.reviewer_brief.length > 0)
    ? input.reviewer_brief
    : brief;

const fingerprintCommand =
  'python "' + repo + '\\tools\\workspace_fingerprint.py" --repo "' + repo + '"';

const builderReports = [];
const reviews = [];
const fingerprintChecks = [];
const notes = [];

function errorName(error) {
  return (error && typeof error.name === 'string') ? error.name : 'unknown-error';
}

function envelope(status, cycles, escalation) {
  return {
    status: status,
    unit_id: unitId,
    cycles_used: cycles,
    max_revise_cycles: MAX_REVISE_CYCLES,
    builder_reports: builderReports,
    reviews: reviews,
    fingerprint_checks: fingerprintChecks,
    escalation: escalation || null,
    notes: notes,
  };
}

function builderPrompt(cycle, previousReview) {
  const parts = [];
  parts.push(
    'You are the BUILDER for RoutePilot work unit "' + unitId + '" ' +
    '(cycle ' + cycle + ' of at most ' + MAX_REVISE_CYCLES + ').'
  );
  parts.push(
    'First call the skill tool with name "builder" and follow it exactly. ' +
    'Then read AGENTS.md and docs/WORKFLOW.md.'
  );
  parts.push(
    'Authorized work unit — implement exactly this, and nothing beyond it:\n' +
    '----- BEGIN UNIT -----\n' + brief + '\n----- END UNIT -----'
  );
  if (previousReview) {
    parts.push(
      'A reviewer returned REVISE. Fix exactly the reported issues and nothing else. ' +
      'Reviewer finding:\n' + JSON.stringify(previousReview)
    );
  }
  parts.push(
    'Never commit. Run the targeted verification for this unit, then compute the workspace ' +
    'fingerprint with:\n  ' + fingerprintCommand + '\n' +
    'Return only the structured report the builder skill defines.'
  );
  return parts.join('\n\n');
}

function reviewerPrompt(cycle, report) {
  const parts = [];
  parts.push(
    'You are the independent REVIEWER for RoutePilot work unit "' + unitId + '" ' +
    '(cycle ' + cycle + ').'
  );
  parts.push(
    'First call the skill tool with name "reviewer" and follow it exactly. You are read-only: ' +
    'create, modify and delete nothing, and never commit.'
  );
  parts.push(
    'The governing contract for this unit is:\n' +
    '----- BEGIN CONTRACT -----\n' + reviewerBrief + '\n----- END CONTRACT -----'
  );
  parts.push(
    'The Builder reports the following. Treat it as a claim, never as evidence:\n' +
    '----- BEGIN BUILDER REPORT -----\n' + JSON.stringify(report, null, 2) +
    '\n----- END BUILDER REPORT -----'
  );
  parts.push(
    'Verify against the repository yourself: read git diff, the changed files, the relevant tests, ' +
    'docs/PRODUCT_SPEC_v2.md, the relevant docs/DECISIONS.md sections and docs/ARCHITECTURE.md.'
  );
  parts.push(
    'Step 1 — before inspecting anything, run:\n  ' + fingerprintCommand + '\n' +
    'and keep the digest as workspace_fingerprint_before_review.'
  );
  parts.push(
    'Step 2 — review, re-running the targeted tests yourself and adding any adversarial check you ' +
    'consider decisive.'
  );
  parts.push(
    'Step 3 — run the fingerprint command again and report it as ' +
    'workspace_fingerprint_after_review. It must be identical to the before value.'
  );
  parts.push(
    'Return exactly one verdict — PASS, REVISE or ESCALATE_TO_OWNER — with the fields and issue ' +
    'format the reviewer skill defines. Use "" and [] for fields that do not apply.'
  );
  return parts.join('\n\n');
}

let lastReviewForBuilder = null;

for (let cycle = 1; cycle <= MAX_REVISE_CYCLES; cycle++) {
  phase('build');
  log('cycle ' + cycle + '/' + MAX_REVISE_CYCLES + ': builder');

  let report = null;
  try {
    report = await agent(builderPrompt(cycle, lastReviewForBuilder), {
      label: 'builder-' + cycle,
      phase: 'build',
      schema: builderSchema,
    });
  } catch (error) {
    notes.push('cycle ' + cycle + ': builder child failed (' + errorName(error) +
      '); counted as a failed cycle');
    report = null;
  }
  builderReports.push(report);

  if (!report) {
    reviews.push(null);
    fingerprintChecks.push(null);
    continue;
  }

  phase('review');
  log('cycle ' + cycle + '/' + MAX_REVISE_CYCLES + ': reviewer');

  let review = null;
  try {
    review = await agent(reviewerPrompt(cycle, report), {
      label: 'reviewer-' + cycle,
      phase: 'review',
      schema: reviewerSchema,
    });
  } catch (error) {
    notes.push('cycle ' + cycle + ': reviewer child failed (' + errorName(error) +
      '); counted as a failed cycle');
    review = null;
  }
  reviews.push(review);

  if (!review) {
    fingerprintChecks.push(null);
    continue;
  }

  const check = {
    cycle: cycle,
    builder_fingerprint: report.workspace_fingerprint,
    reviewer_before: review.workspace_fingerprint_before_review,
    reviewer_after: review.workspace_fingerprint_after_review,
    builder_matches_reviewer_before:
      report.workspace_fingerprint === review.workspace_fingerprint_before_review,
    reviewer_read_only:
      review.workspace_fingerprint_before_review === review.workspace_fingerprint_after_review,
  };
  fingerprintChecks.push(check);

  if (!check.builder_matches_reviewer_before || !check.reviewer_read_only) {
    notes.push('cycle ' + cycle + ': workspace fingerprint gate failed — PASS is not accepted');
    return envelope('INTEGRITY_FAILURE', cycle, null);
  }

  if (review.verdict === 'PASS') {
    return envelope('PASS', cycle, null);
  }
  if (review.verdict === 'ESCALATE_TO_OWNER') {
    return envelope('ESCALATE_TO_OWNER', cycle, review.escalation);
  }

  notes.push('cycle ' + cycle + ': reviewer returned REVISE');
  lastReviewForBuilder = review;
}

return envelope('REVISE_EXHAUSTED', MAX_REVISE_CYCLES, null);
