export const meta = {
  name: 'fix-review-round',
  description: 'Fix one review round: fable plans, sonnet implements, opus verifies, fable signs off',
  whenToUse:
    'After a review round is posted on the PR: Workflow({name: "fix-review-round", args: {review: "<comment URL>"}}). ' +
    'Optional args: {max_attempts: 2, commit: true}. Batches run sequentially (shared files), so wall-clock is the sum of batches.',
  phases: [
    { title: 'Plan', detail: 'fable reads the review and batches the findings into coherent fixes', model: 'fable' },
    { title: 'Implement', detail: 'sonnet fixes one batch at a time, tests first', model: 'sonnet' },
    { title: 'Verify', detail: 'opus adversarially verifies each batch and demands rework', model: 'opus' },
    { title: 'Sign-off', detail: 'fable audits the whole diff, runs the gate, commits', model: 'fable' },
  ],
}

// ---- inputs -------------------------------------------------------------
const review = args && args.review
if (!review) throw new Error('pass args: {review: "<PR review-comment URL>"}')
const MAX_ATTEMPTS = (args && args.max_attempts) || 2
const COMMIT = args && args.commit === false ? false : true

const REPO = '/Users/elias/ClaudeCyclingMCP'
const BRANCH = (args && args.branch) || 'dev'
const PR = (args && args.pr) || 10
const HOUSE = `
Repo: ${REPO}, branch ${BRANCH} (PR #${PR}). Read CLAUDE.md first; its rules are load-bearing:
a null is never folded to zero and a placeholder zero is never compared as a measurement;
never overwrite a stored value with a null (the clear verb is the one deliberate exception);
tolerate-and-flag beats reject when a valid datum exists; refusals are raised as CoachError
and must be catchable by _coach's handler set (no bare AssertionError/OverflowError escapes);
migrations are append-only; golden files change only deliberately. Gate:
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/python -m pytest -q
(offline suite must stay green; DB tests point CLAUDE_CYCLING_DB at a tmp_path and must never
touch ~/.claude-cycling/coach.db).`

// ---- Phase 1: fable plans ----------------------------------------------
phase('Plan')
const PLAN_SCHEMA = {
  type: 'object',
  required: ['batches', 'shared_context'],
  properties: {
    shared_context: {
      type: 'string',
      description: 'Facts every implementer needs: review URL, round title, cross-batch interactions, ordering constraints',
    },
    batches: {
      type: 'array',
      minItems: 1,
      maxItems: 4,
      items: {
        type: 'object',
        required: ['name', 'spec'],
        properties: {
          name: { type: 'string', description: 'short slug, e.g. date-family' },
          spec: {
            type: 'string',
            description:
              'A complete, self-contained implementation spec: which findings it covers (numbers + file:line), ' +
              'the required approach per finding, exact tests to write FIRST (each pinning the reproduced failure ' +
              'scenario, asserting response contents not counts, plus a test for the value just OUTSIDE every new ' +
              'guard), and which cleanup items ride along.',
          },
        },
      },
    },
  },
}
const plan = await agent(
  `You are the planner for a fix round. ${HOUSE}

Fetch the review with: gh pr view ${PR} --comments — the review to fix is the comment at ${review}
(match it by URL; it is the latest "Review round N" comment). Read the findings, the cleanup
list, and the "verified clean" section. Then read the cited code.

Produce a plan that groups all findings AND cleanup items into 1-4 coherent batches, where a
batch = changes that belong in one reviewable unit (same defect family or same functions).
Order the batches so earlier ones don't invalidate later specs (e.g. a helper move before its
new callers; a behavior change before the docstring that must describe it). Each batch spec
must be executable by an implementer who has NOT read the review: restate everything needed.
Demand test-first: for every guard or boundary in the spec, name the test for the value just
outside it. Note explicitly in each spec which findings interact with fixes from earlier
batches in this same run.`,
  { model: 'fable', label: 'plan', phase: 'Plan', schema: PLAN_SCHEMA },
)
log(`plan: ${plan.batches.length} batch(es): ${plan.batches.map((b) => b.name).join(', ')}`)

// ---- Phase 2+3: sequential implement -> verify loop per batch -----------
const VERDICT_SCHEMA = {
  type: 'object',
  required: ['approved', 'feedback'],
  properties: {
    approved: { type: 'boolean' },
    feedback: {
      type: 'string',
      description:
        'If not approved: precise rework instructions (file:line, what is wrong, what to do). ' +
        'If approved: residual notes worth carrying to sign-off (may be empty).',
    },
    boundary_probes: {
      type: 'string',
      description: 'The just-outside-the-guard values you actually tested, and what happened',
    },
  },
}

const results = []
for (const batch of plan.batches) {
  let attempt = 0
  let feedback = ''
  let verdict = { approved: false, feedback: 'never ran' }
  while (attempt < MAX_ATTEMPTS) {
    attempt += 1
    const done = results.map((r) => `${r.batch}: ${r.approved ? 'landed' : 'landed unapproved'}`).join('; ') || 'none yet'
    await agent(
      `You are the implementer. ${HOUSE}

Shared context from the planner: ${plan.shared_context}
Batches already implemented this run (their changes are in the working tree): ${done}

Implement this batch spec, TEST FIRST (write the failing regression tests, watch them fail,
then fix until green). Do not commit — a later sign-off step commits. Do not touch anything
outside the spec except where the spec's cleanup items say so.

${batch.spec}
${feedback ? `\nA verifier rejected your previous attempt. Address every point:\n${feedback}` : ''}

Run the gate before finishing. Your final text: a factual change list (files, functions,
tests added, gate result) for the verifier — raw data, not prose for a human.`,
      { model: 'sonnet', label: `impl:${batch.name}#${attempt}`, phase: 'Implement' },
    )
    verdict = await agent(
      `You are the adversarial verifier. ${HOUSE}

A batch of fixes was just implemented in the working tree (NOT committed — inspect with
git diff). The spec it had to satisfy:

${batch.spec}

Verify adversarially, in this order:
1. Does each fix land at the boundary, not one value inside it? For EVERY new or moved guard,
   try/except, comparison, or cap in the diff, construct and RUN the input just outside it
   (use .venv/bin/python with CLAUDE_CYCLING_DB at a scratch path under /private/tmp/claude-501/).
   Six rounds of this PR's history say boundary misses are the dominant regression class.
2. Did the fix break what the earlier rounds fixed? Run the full offline suite; also re-probe
   the specific behaviors the spec says interact with earlier batches.
3. Are the new tests real? They must fail on the pre-fix code path (check by reasoning or by
   reverting the fix hunk mentally) and assert response contents, not counts.
4. Any survivor of the batch's pattern left in src/cycling_mcp/ (except verify.py)? Grep.
Approve ONLY if all four pass. If rejecting, give file:line-precise rework instructions.`,
      { model: 'opus', label: `verify:${batch.name}#${attempt}`, phase: 'Verify', schema: VERDICT_SCHEMA },
    )
    if (verdict.approved) break
    feedback = verdict.feedback
    log(`${batch.name}: rejected on attempt ${attempt} — ${feedback.slice(0, 120)}`)
  }
  results.push({ batch: batch.name, approved: verdict.approved, attempts: attempt, notes: verdict.feedback })
  log(`${batch.name}: ${verdict.approved ? 'approved' : 'NOT approved'} after ${attempt} attempt(s)`)
}

// ---- Phase 4: fable signs off -------------------------------------------
phase('Sign-off')
const unapproved = results.filter((r) => !r.approved)
const signoff = await agent(
  `You are the final auditor. ${HOUSE}

Every batch of this fix round has been implemented in the working tree (uncommitted).
Batch outcomes: ${JSON.stringify(results)}

1. Read the FULL diff (git diff) end to end, as one reviewer, looking for cross-batch
   interactions the per-batch verifiers could not see: one batch's helper move breaking
   another's caller, duplicate helpers introduced twice, docstrings describing pre-fix
   behavior of another batch, CHANGELOG collisions.
2. Run the complete gate. Fix trivial gate failures (format, an import) yourself; anything
   substantive gets reported, not patched.
3. Self-review for the boundary class: for every guard in the diff, name the value just
   outside it and confirm a test covers it.
4. Update CHANGELOG.md under the unreleased section with this round's entries if the
   implementers have not already done so coherently (merge/dedupe their entries).
${
  COMMIT && unapproved.length === 0
    ? `5. If and only if the gate is green and you found no substantive problem: commit everything to ${BRANCH} with a message describing the round, and push.`
    : `5. DO NOT COMMIT: ${unapproved.length ? `batches not approved: ${unapproved.map((r) => r.batch).join(', ')}` : 'commit disabled by args'}. Leave the tree for a human.`
}

Your final text: gate result, whether you committed (and the SHA), unresolved concerns.`,
  { model: 'fable', label: 'sign-off', phase: 'Sign-off' },
)

return { batches: results, signoff }
