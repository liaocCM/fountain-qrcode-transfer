export const meta = {
  name: 'survey-debate',
  description: 'Small research fan-out: N researchers → per-claim source verify → 1 debater refutes → synthesis report written to file',
  whenToUse: 'Mid-weight research: heavier than a single agent, far cheaper than deep-research. Best for tool/approach surveys that benefit from source-grounding + adversarial checking.',
  phases: [
    { title: 'Research', detail: 'parallel researchers, one distinct angle each' },
    { title: 'Verify', detail: 'parallel per-claim: fetch each source, confirm it exists and supports the claim', model: 'haiku' },
    { title: 'Debate', detail: 'one skeptic refutes the grounded claims' },
    { title: 'Synthesize', detail: 'report written to file; only summary returned inline' },
  ],
}

// args: string question, or { question, researchers? (2-4, default 2), model? (model override for ALL subagents) }
// Accepts a JSON-encoded string too — some invocation paths stringify args.
let a = args
if (typeof a === 'string' && a.trim().startsWith('{')) {
  try { a = JSON.parse(a) } catch { /* treat as plain question */ }
}
const q = typeof a === 'string' ? a : (a && a.question)
if (!q) throw new Error('pass the research question as args (string or {question})')
const N = Math.min(4, Math.max(2, (typeof a === 'object' && a.researchers) || 2))
const MODEL = (typeof a === 'object' && a.model) || undefined
log(`researchers=${N}, subagent model=${MODEL || 'session default'}`)
const slug = q.toLowerCase().replace(/[^a-z0-9一-鿿]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40)
const REPORT = `/tmp/survey-debate-${slug}.md`

const FINDINGS = {
  type: 'object', required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object', required: ['claim', 'evidence', 'sources'],
        properties: {
          claim: { type: 'string', description: 'one factual, checkable statement' },
          evidence: { type: 'string', description: 'why you believe it, 1-3 sentences' },
          sources: { type: 'array', items: { type: 'string' }, description: 'URLs' },
        },
      },
    },
  },
}

const GROUNDING = {
  type: 'object', required: ['status', 'note'],
  properties: {
    status: {
      type: 'string',
      enum: ['grounded', 'source-mismatch', 'source-dead', 'no-source'],
      description: 'grounded = a source loads AND supports the claim; source-mismatch = loads but does not support/contradicts; source-dead = URL 404/unreachable; no-source = no usable URL given',
    },
    note: { type: 'string', description: 'what the source actually says, or why it failed — 1-2 sentences' },
  },
}

const VERDICTS = {
  type: 'object', required: ['verdicts'],
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object', required: ['claim', 'verdict', 'reason'],
        properties: {
          claim: { type: 'string' },
          verdict: { type: 'string', enum: ['confirmed', 'weak', 'refuted'] },
          reason: { type: 'string' },
        },
      },
    },
  },
}

phase('Research')
const ANGLES = [
  'mainstream / established solutions: official docs, canonical repos, widely-adopted tools',
  'community experience: benchmarks, comparisons, criticisms, failure reports, recent discussions',
  'alternative or emerging approaches, and lessons from adjacent fields',
  'operational reality: licensing, cost, deployment/hardware requirements, maintenance status',
].slice(0, N)

const research = (await parallel(ANGLES.map((angle, i) => () => agent(
  `Research question:\n${q}\n\n` +
  `Your angle — cover ONLY this, other researchers cover the rest:\n${angle}\n\n` +
  `Use WebSearch/WebFetch. Return 3-8 findings. Every claim must be specific and checkable, ` +
  `with at least one source URL you actually opened. No filler, no hedging.`,
  { label: `research:${i + 1}`, phase: 'Research', schema: FINDINGS, model: MODEL }
)))).filter(Boolean)

const claims = research.flatMap(r => r.findings)
log(`${claims.length} claims from ${research.length} researchers`)
if (claims.length === 0) return { error: 'no findings survived research phase' }

// Verify: per-claim, parallel, cheap. Grounds each claim against its sources BEFORE
// debate — so the single barrier-debater reasons over real evidence, not fabrications.
phase('Verify')
const grounded = []   // {claim, evidence, sources, note}
const dropped = []    // {claim, sources, status, note}
const checks = (await parallel(claims.map((c, i) => () =>
  agent(
    `Verify ONE research claim against its cited sources. Objective fact-check, not opinion.\n\n` +
    `Claim: ${c.claim}\n` +
    `Researcher's evidence: ${c.evidence}\n` +
    `Cited sources: ${JSON.stringify(c.sources)}\n\n` +
    `Open EACH source with WebFetch. Decide: does at least one source actually load AND state ` +
    `something that supports this specific claim? Default to failure if you cannot confirm — ` +
    `a plausible-sounding claim with a dead or off-topic source is NOT grounded.`,
    { label: `verify:${i + 1}`, phase: 'Verify', model: MODEL || 'haiku', effort: 'low', schema: GROUNDING }
  ).then(g => ({ c, g }))
))).filter(Boolean)

for (const { c, g } of checks) {
  if (g && g.status === 'grounded') grounded.push({ ...c, note: g.note })
  else dropped.push({ claim: c.claim, sources: c.sources, status: (g && g.status) || 'no-source', note: (g && g.note) || 'verifier failed' })
}
log(`verify: ${grounded.length} grounded, ${dropped.length} dropped (${dropped.filter(d => d.status === 'source-dead').length} dead, ${dropped.filter(d => d.status === 'source-mismatch').length} mismatch)`)
if (grounded.length === 0) return { error: 'no claims survived source verification', dropped, report: null }

phase('Debate') // barrier justified: the debater must see ALL grounded claims to catch contradictions between researchers
const debate = await agent(
  `You are the skeptic in a research debate. Your job is to REFUTE, not to agree — ` +
  `default to 'weak' unless the reasoning stands up.\n\n` +
  `Research question:\n${q}\n\n` +
  `These claims already passed source-verification (a real source supports each), so DON'T ` +
  `re-litigate whether the source exists — attack the REASONING: contradictions between claims, ` +
  `outdated info, overgeneralization, cherry-picking, whether it actually answers the question.\n\n` +
  `Grounded claims:\n${JSON.stringify(grounded, null, 2)}\n\n` +
  `Verdicts: confirmed (survived a real challenge) / weak (plausible, reasoning thin) / refuted (unsound despite a real source).`,
  { label: 'debater', phase: 'Debate', schema: VERDICTS, model: MODEL }
)
const verdicts = (debate && debate.verdicts) || []
const refuted = verdicts.filter(v => v.verdict === 'refuted')
log(`debate: ${verdicts.filter(v => v.verdict === 'confirmed').length} confirmed, ${verdicts.filter(v => v.verdict === 'weak').length} weak, ${refuted.length} refuted`)

phase('Synthesize')
const summary = await agent(
  `Write the final research report to ${REPORT} using the Write tool, then return ONLY ` +
  `the file path plus a summary of at most 5 lines. Do NOT print the report inline ` +
  `(long inline output overflows the turn).\n\n` +
  `Research question:\n${q}\n\n` +
  `Claims with debate verdicts:\n${JSON.stringify(verdicts, null, 2)}\n\n` +
  `Grounded claims (for sources + what each source says):\n${JSON.stringify(grounded, null, 2)}\n\n` +
  `Dropped in verification (unverifiable — DO NOT use as evidence):\n${JSON.stringify(dropped, null, 2)}\n\n` +
  `Report structure: executive summary → conclusions built ONLY on confirmed claims ` +
  `(cite sources) → 'weak' claims as unverified leads → appendix: refuted claims (reason) + ` +
  `claims dropped in verification (status: dead/mismatch/no-source). Markdown, concise.`,
  { label: 'synthesize', phase: 'Synthesize', model: MODEL }
)

return {
  report: REPORT,
  summary,
  stats: {
    researchers: research.length,
    claims: claims.length,
    grounded: grounded.length,
    dropped: dropped.length,
    refuted: refuted.length,
  },
}
