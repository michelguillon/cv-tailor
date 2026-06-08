# cv-tailor — Top 15 Learnings
## For interview preparation and portfolio conversations

These are the fifteen most transferable insights from building cv-tailor
(and the three prior projects it draws on). Each one is stated as a claim
you can defend with a specific example from the build.

---

### 1. I started trying to understand models. I ended up understanding systems.

The right question when building with AI is not "which model should I use?"
It's "what properties does this system need, and what is the model's role in
achieving them?" Model choice is downstream of system design.

**The example:** the most important decisions in cv-tailor weren't which model
to use for which phase — they were where to put the HITL gates, how to define
the convergence signals, how to separate the audit trail from the model's
context, and how to make grounding a first-class constraint. None of those are
model decisions. They're system design decisions. The models execute them.

---

### 2. Models are tools, not the product.

The value in the system comes from the architecture: what the models can see
(retrieval), how their outputs are evaluated (rubric + scoring), what happens
when they disagree (orchestration), and where humans remain in the loop. Swap
any individual model for a comparable alternative and the system largely works.
Remove the grounding check or the HITL fit assessment and it doesn't.

**The interview framing:** "The orchestrator calls `critique_cv()` the same way
it calls `get_spending_summary()` in my finance agent. The provider is an
implementation detail of the tool, not a concern of the orchestrator. That
abstraction is what makes the system composable — and what makes the model
choice the least interesting part of the design."

---

### 3. Deterministic phases; agency only where judgment is genuinely required.

Most production agentic systems are not pure agent loops. They're deterministic
scaffolds containing one or more bounded agentic regions. Wrapping a controlled
loop in deterministic scaffolding makes cost, latency, and HITL placement
predictable. The question is always: which parts of this workflow have a known
correct procedure, and which require the model to actually decide?

**The example:** Phases 0–2 and 4–6 always run in the same order. Only Phase 3
(refinement) is agentic — because only there is the right answer genuinely
unknown in advance: how many iterations, which draft to select, when to stop.

---

### 4. Retrieval quality matters more than retrieval ideology.

"Hybrid retrieval beats semantic" is an ideology, not a fact. The RFI project's
36-configuration eval showed semantic outperforming hybrid on a small,
paraphrase-rich corpus. cv-tailor confirmed it. The retrieval methodology should
match the actual discrimination problem: in this corpus, keyword coverage (not
semantic similarity) is the right signal for selecting between CV variants of
the same section, because semantic scores barely differ across versions of the
same person's profile.

**The generalisation:** measure your corpus before committing to a retrieval
architecture. The right answer is empirical, not theoretical.

---

### 5. Evaluation criteria are a design decision, not a given.

The scoring rubric in cv-tailor is a versioned, dynamic object. It starts from
the JD and can evolve during the loop when the critique surfaces requirements
not in the original JD. This is deliberate: a static rubric measures the CV
against the JD as written; a dynamic rubric measures it against what the JD
means. The score is only meaningful if the evaluation criteria are right.

**The guard that makes it safe:** maximum 2 additions per iteration, each
validated against the JD. In the Sonnet validation run (F-28), the orchestrator
proposed 30+ rubric additions; the guard rejected every one. Without the guard,
the rubric would have inflated until keyword coverage stalled — a false signal.

---

### 6. Grounded optimisation beats raw optimisation.

A CV can achieve high keyword coverage and high critique quality while making
claims the candidate can't support. The grounding check — Grounded Coverage %
and Unsupported Claims count — is a third, orthogonal signal: not "does this CV
mention the right things" but "can the claims in this CV be traced to evidence."

**The harder lesson:** better narrative framing (via the CVCM) increases
fabrication pressure. Giving a model richer context and asking it to frame
experience through value creation patterns creates genuine pressure to invent.
Optimising for authenticity without measuring grounding produces a CV that
feels more genuine but may be less honest. Both constraints must be tracked.

---

### 7. Human review is the strongest safety mechanism — and it belongs in the design, not as an afterthought.

The Phase 4 HITL display surfaced that the Microsoft experience section was PM
work, not presales, and that Xandr was adtech, not fintech. The models correctly
flagged these as unresolved. But only a human could decide whether to address
them, how to frame them, or whether to apply for the role at all.

**The design principle:** HITL placement is an architectural decision with cost,
latency, and quality implications. The fit assessment checkpoint is
conversational because the human has nuanced knowledge the model doesn't. The
formatting checkpoint is binary because there's nothing to discuss. Different
decisions require different interaction patterns.

---

### 8. LLM-as-judge over-scores without explicit anchors. Actionable signal lives in the failure modes.

The RFI project found faithfulness = 5.00 and relevance = 5.00 across all 36
configurations — the judge was consistently too generous to discriminate. In
cv-tailor, the same problem would have made `quality_delta` useless as a
convergence signal if the score anchors weren't explicit: "9–10 = publication-
ready; 7–8 = one gap remains; 5–6 = multiple structural issues; 3–4 = weak."

**The generalisation:** when using a model to evaluate model output, the
evaluation rubric must be defined with explicit anchors, or the scores reflect
training priors rather than actual quality. The actionable signal is often in
the rate of failure modes (retrieval gaps, unsupported claims), not the score.

---

### 9. Deterministic where the content is a fact; model judgment only where it adds value.

The Phase 2 drafter dropped role/date lines from experience sections
inconsistently — Microsoft lost its job title; AppNexus/Xandr rendered as two
identical blocks. The fix was structural: split the role line out before
drafting, store it in the manifest, re-attach verbatim at assembly. The model
never sees it and cannot drop it.

**The general rule:** if the correct output is determinable before the model
call, don't ask the model to produce it. Job titles and dates are facts. Section
ordering is a position integer. Static sections are constants. The model is a
judgment engine, not a transcription service.

---

### 10. The audit trail is a first-class output, not a debugging tool.

Every decision in the system is logged: which section was selected and why,
which writer's draft was chosen, which critique items were accepted and rejected,
what the convergence signals were at each iteration. This is not observability
for debugging — it's information that improves future applications, not just the
current one.

**The architectural principle:** the audit trail is separate from the model's
context (it's never injected back into the messages array). This keeps context
clean and cost down, while keeping the audit complete. Context is what the
model sees. The audit trail is what the human reads afterwards. Conflating them
is a common design mistake.

---

### 11. Frameworks hide problems; understand the problem before reaching for a framework.

cv-tailor was built without LangChain or LangGraph by design. The LoopMemory
pattern, the dual-signal convergence table, the section freeze mechanics, the
rubric JD-validation guard — none of these would have been explicit design
decisions in a framework-wrapped implementation. They would have been emergent
behaviours, difficult to reason about and impossible to explain.

**The practical implication:** build manually the first time you encounter a
pattern. Use a framework once you understand what it's abstracting and are
confident the abstraction fits your actual problem. "I learned LangGraph" is a
weaker portfolio statement than "I understand why you'd use LangGraph and what
it's hiding."

---

### 12. Measure early and often; let real data correct your estimates.

The dual-writer loop cost estimate was $2–4. Actual: $0.79. The rubric guard
was expected to reject occasional over-eager additions. In practice: Sonnet
proposed 30+ per run; the guard rejected every one. The synthesis rate was
expected to stay high; it dropped from 87% to 50% across two iterations.

In each case the real number was more interesting and more useful than the
estimate. The estimates came from reasoning about the design; the measurements
came from running on real data. The lesson is not "don't estimate" — it's: run
on real data as early as possible, and when the measurement contradicts the
estimate, update both the number and the reasoning that produced it.

---

### 13. Business value comes from workflow transformation, not model choice.

The manual workflow this project automated took 45–90 minutes, produced no
audit trail, and couldn't be repeated consistently. The automated workflow
takes 2–4 minutes, produces a traceable decision record, costs under $1, and
can be run at application scale. The quality difference between GPT-4o-mini
and a hypothetical better model in the writer role is marginal compared to
the difference between having an audit trail and not having one.

**The evaluation question for any AI system:** not "which model is best" but
"what does this system make possible that wasn't possible before?" That question
grounds the architecture in outcomes, not in capability marketing.

---

### 14. One capability, two powers — security design should follow the threat model, not the convenience model.

The security gates in cv-tailor are not full authentication. They're a spend
guard: stop anyone but the owner from running expensive operations or mutating
corpus state on a public portfolio deployment. A passphrase-unlock issuing a
signed HttpOnly capability cookie achieves that without a login system.

**The design principle:** match the security model to the actual threat. "Public
demo, private full" is a different problem from "multi-tenant access control."
Over-engineering the former (building accounts, roles, sessions) adds complexity
without solving the actual risk. The gate fails closed: no `FULL_MODE_KEY`
environment variable means demo-only, view-only corpus, no configuration error
that opens the expensive path.

---

### 15. The right sequence is: problem → system → AI. Not the reverse.

Every model routing decision, every HITL placement, every convergence signal
in cv-tailor has a reason grounded in the actual workflow it was automating.
GPT-4o-mini is the challenger writer because of direct observation that it
produces harsher, less flattering drafts — not because demonstrating
multi-provider adds portfolio value. The fit assessment is conversational
because the human has knowledge the model genuinely lacks. The grounding check
exists because better narratives increase fabrication pressure.

**The interview version:** "I started with a workflow I was already doing
manually. I asked what a system that automated that workflow would need to do.
Then I asked which parts of that system benefit from AI judgment versus
deterministic computation versus human review. The model choices were the last
decisions, not the first."

That ordering is what makes the architecture defensible. Starting with the AI
and looking for a problem produces systems that demonstrate capability.
Starting with the problem and reaching for AI where it adds value produces
systems that demonstrate judgment.

---

## Quick reference — decision codes

| Learning | Source decisions/findings |
|---|---|
| 1. Models → systems | Retrospective §3 #10 |
| 2. Models are tools | D-02 |
| 3. Deterministic phases | D-01 |
| 4. Retrieval quality | R-07, F-10, F-11 |
| 5. Evaluation criteria | D-04, F-28 |
| 6. Grounded optimisation | D-33, F-39, grounding concept |
| 7. Human review | D-18, F-25 |
| 8. LLM-as-judge | R-08, F-14 |
| 9. Deterministic facts | D-32, D-13, F-29 |
| 10. Audit trail | D-06 |
| 11. Frameworks | LS-02 |
| 12. Measure early | F-28, D-28 |
| 13. Workflow transformation | Retrospective §3 #7 |
| 14. Security model | D-38, D-39 |
| 15. Problem → system → AI | Retrospective coda |
