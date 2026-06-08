# cv-tailor — Project Retrospective

**Project duration:** ~6 weeks (Week 3 of the AI learning track)  
**Lines of code:** ~4,000 Python + ~2,000 TypeScript/React  
**Test coverage:** 191 tests, zero API calls in suite  
**Final cost per full run:** ~$0.79 (F-28)

---

## The framing that matters before anything else

This project did not start with AI and look for a problem.

It started with a problem — the manual multi-model CV tailoring workflow was
producing better results than any single-model pass, but taking 45–90 minutes
per application with no audit trail and no repeatability.

The sequence was:

```
Problem
  ↓
What kind of system would solve it?
  ↓
Which parts of that system benefit from AI?
  ↓
Which AI patterns apply?
```

Not the reverse. That ordering is what made the architecture defensible.
Every model routing decision, every HITL placement, every convergence signal
has a reason grounded in the actual workflow it was automating — not in
demonstrating AI capability for its own sake.

This is worth stating explicitly because most AI portfolio projects go the
other direction: pick a model or a framework, then retrofit a use case. The
difference shows in whether you can answer "why GPT for critique rather than
Claude?" with something better than "to demonstrate multi-provider."

---

## 1. What I thought I was building

A CV tailoring application that could improve job applications.

Concretely: give it a job description and some CV versions, get back a better
CV. An automation of a repetitive manual task. Something like a smart
find-and-replace that understood job requirements.

The early spec reflected this. Phase 0 extracts keywords. Phase 1 picks the
best CV. Phase 2 drafts against the keywords. Phase 3 critiques and revises.
Clean, linear, mechanical.

The implicit assumption was that the value lived in the model — that a
sufficiently capable LLM would produce a good CV if given the right prompt.
The architecture was a delivery mechanism for the model's capability.

---

## 2. What I actually built

A multi-agent decision-support system combining retrieval, evaluation,
grounding, orchestration, and human review.

The shift from "CV tailoring application" to that description is not
rebranding. It describes a different thing:

**Retrieval** — ChromaDB with section-granular ingestion, metadata-filtered
semantic search, and keyword coverage for cross-variant selection. The system
doesn't know what's in the corpus until it retrieves it. The retrieval
architecture determines what the models can see.

**Evaluation** — a dynamic scoring rubric that starts from the JD and can
evolve during the loop. Not just "does the CV match the JD" but "are we
measuring the right things?" The rubric is a first-class versioned object
because evaluation criteria are a design decision, not a given.

**Grounding** — Phase 5 produces a Grounded Coverage score and an Unsupported
Claims count. The system flags when the CV makes claims that can't be traced to
the JD or rubric. This distinction — between "comprehensive" (keyword coverage)
and "honest" (grounded) — emerged from the build, not the spec. The original
design had no grounding concept.

**Orchestration** — a dual-writer loop where two models with different priors
draft independently, an orchestrator adjudicates, and both writers can push
back. The orchestrator doesn't just pick the better draft — it can synthesise
the best elements of both. The synthesis rate dropped from 87% in iteration 1
to 50% in iteration 2 (F-28), showing the loop was actually converging rather
than reflexively merging.

**Human review** — three HITL checkpoints, each designed around a different
interaction pattern. The fit assessment checkpoint is conversational because
the human has nuanced knowledge the model doesn't (Microsoft is a PM role, not
presales). The formatting checkpoint is binary because there's nothing to
discuss — the diff either looks right or it doesn't.

The combination is what makes it a decision-support system rather than an
automation. The models don't make the final call. They produce structured
analysis, scored recommendations, and explicit reasoning that supports a human
decision. The human can override anything. The audit trail shows why every
automated decision was made.

---

## 3. Top 10 learning shifts

Each one is a before and after. The "before" is what I believed going in.
The "after" is what the build proved.

---

**1.**
> We believed: the model is the product — choose a good one and the system will be good.
>
> We learned: the model is a component. The architecture is the product.

The value in cv-tailor doesn't come from any particular model. It comes from
what the models can see (retrieval), how their outputs are evaluated (rubric +
scoring), what happens when they disagree (orchestration), and where humans
remain in the loop. Swapping GPT-4o-mini for any comparable model produces
similar results. Removing the grounding check or the HITL fit assessment does not.

In any serious AI system: the model is a component. The architecture is the product.

---

**2.**
> We believed: hybrid retrieval (semantic + BM25) would outperform pure semantic retrieval.
>
> We learned: corpus characteristics matter more than retrieval ideology.

The RFI project's 36-configuration eval showed semantic outperforming hybrid
on a small, paraphrase-rich corpus. cv-tailor confirmed it. More specifically:
keyword coverage — not semantic similarity — is the right signal for selecting
between CV variants of the same section, because semantic scores barely differ
across versions of the same person's profile.

The retrieval methodology should match the actual discrimination problem.
Measure your corpus before committing to an architecture.

---

**3.**
> We believed: more autonomy produces more value — HITL is friction to minimise.
>
> We learned: human review is the strongest safety mechanism, and its placement is an architectural decision.

Phase 4's HITL surfaced that the Microsoft section was PM work, not presales,
and that Xandr was adtech, not fintech. The models flagged both as unresolved.
But only a human could decide whether to address them, how to frame them, or
whether to apply at all. That's not a model failure. It's a correct boundary.

The question is not how much autonomy to give the system, but which decisions
benefit from human judgment. That question has a different answer at each
checkpoint.

---

**4.**
> We believed: richer narrative context produces more authentic CVs.
>
> We learned: better narratives increase fabrication pressure — authenticity and grounding are in tension.

Adding the CVCM (Candidate Value Creation Model) to shift drafting toward
value articulation produced more compelling CVs. It also produced more
unsupported claims: statements about leadership philosophy and value creation
patterns the JD didn't ask for and the grounding check couldn't validate.

Optimising for authenticity without measuring grounding produces a CV that
feels more genuine but may be less honest. Both constraints must be tracked.
The grounding check is what makes the CVCM safe to use.

---

**5.**
> We believed: keyword coverage + critique quality score was a sufficient convergence signal.
>
> We learned: both signals are optimisable without the CV becoming more honest. Grounding is the missing third axis.

A CV can score well on keywords by restating JD requirements without grounding
them in actual experience. It can score well on critique quality through
structurally competent writing that makes unsupportable claims. The grounding
check is orthogonal to both: not "does it mention the right things" or "does it
read well" but "can the claims be traced to evidence." That's the signal that
most closely approximates what a competent recruiter is actually checking.

---

**6.**
> We believed: enterprise AI frameworks should be learned first to avoid reinventing the wheel.
>
> We learned: frameworks hide problems. Understanding the problem first is what makes the framework choice defensible.

cv-tailor was built without LangChain or LangGraph by design. The LoopMemory
pattern, the dual-signal convergence table, the section freeze mechanics, the
rubric JD-validation guard — none of these would have been explicit design
decisions in a framework-wrapped implementation. They would have been emergent
behaviours, difficult to reason about and impossible to explain.

Build manually the first time you encounter a pattern. Use a framework once
you understand what it's abstracting.

---

**7.**
> We believed: the value of AI systems comes from model quality.
>
> We learned: the value comes from workflow transformation — the audit trail matters as much as the output.

The manual workflow this project automated took 45–90 minutes and produced no
audit trail. The automated workflow takes 2–4 minutes, costs under $1, and
produces a traceable record of every decision. The quality difference between
GPT-4o-mini and a hypothetically better writer model is marginal compared to
the difference between having an audit trail and not having one.

The question is not "which model is best" but "what does this system make
possible that wasn't possible before?"

---

**8.**
> We believed: evaluation criteria are a given — they come from the requirements.
>
> We learned: evaluation criteria are a design decision, and making them explicit, versioned, and auditable is an architectural choice.

The scoring rubric in this system is a versioned, dynamic object. A static
rubric measures the CV against the JD as written. A dynamic rubric measures it
against what the JD means. Any system that produces a score embeds a theory of
what good looks like. Making that theory explicit is what makes the system
auditable. Leaving it implicit — inside a model's training priors — is also a
choice, but not a defensible one in production.

In the Sonnet validation run, the orchestrator proposed 30+ rubric additions.
The JD-validation guard rejected every one. Without the guard the score would
have been meaningless. The guard was load-bearing precisely because the
evaluation criteria were explicit enough to be enforced.

---

**9.**
> We believed: the model could be instructed to preserve structural facts like job titles and dates.
>
> We learned: deterministic where the content is a fact; model judgment only where it adds value.

The Phase 2 drafter dropped role and date lines from experience sections
inconsistently — Microsoft lost its job title; AppNexus/Xandr rendered as two
identical blocks. The fix was structural: split the role line out before
drafting, store it in the manifest, re-attach verbatim at assembly. The model
never sees it and cannot drop it.

The general rule: if the correct output is determinable before the model call,
don't ask the model to produce it. Job titles and dates are facts. The model is
a judgment engine, not a transcription service.

---

**10.**
> We believed: the goal was to understand AI models well enough to use them effectively.
>
> We learned: the goal is to understand systems well enough to know where AI adds value — and where it doesn't.

Week 1 asked: how do embeddings work, what is ChromaDB, how does Mistral handle
structured output? The questions were about components. Week 3 asked: how do
retrieval quality, evaluation design, convergence signals, grounding constraints,
and human review gates interact to determine whether the output is trustworthy?
The questions were about systems.

A model has capabilities. A system has properties — correctness, auditability,
cost predictability, failure modes, blast radius when a component misbehaves.
None of those system properties can be read off from any individual component's
benchmark.

The right question when building with AI is not "which model should I use?" It
is "what properties does this system need, and what is the model's role in
achieving them?" Model choice is downstream of system design. It should be the
last decision, not the first.

That's the shift that transfers across every future project: from asking "what
can the model do?" to asking "what does the system need to do, and what is the
model's role in that?"

---

**11. The audit trail is the product, as much as the CV.**

Every decision in the system is logged: which CV section was selected and why,
which writer's draft was chosen, which critique items were accepted and
rejected, what the orchestrator's direction was, what the convergence signals
were at each iteration.

A recruiter doesn't read the audit trail. But the candidate does. Knowing why
a section was drafted a certain way, which fit gaps were identified, which
rubric items weren't covered — that is information that improves the next
application, not just the current one.

The system produces two outputs: a tailored CV (immediately useful) and a
structured record of the tailoring decisions (cumulatively valuable). The
second output was not in the original spec.

---

## 4. What I'd do differently

This is the section almost nobody writes, because it requires admitting that
the completed thing is not the optimal design — only the design that emerged
from building it with the knowledge available at the time.

---

**Build grounding before adding more writers.**

The dual-writer loop was added mid-build because it better reflected the
actual workflow. It was the right decision. But the grounding check was added
late, after the CV quality improvements were already visible. The temporal
sequence was: add authenticity lever (CVCM) → observe fabrication pressure →
add grounding check.

The better sequence would have been: define what "honest" means before
optimising for "compelling." Grounding is a constraint on the optimisation
problem, not an afterthought. Starting with grounding would have shaped the
CVCM integration and possibly the convergence signals from the beginning.

---

**Introduce run management earlier.**

The run archive, visibility controls, and retention policy (D-40) were built
late in the UI phase because they felt like polish. They turned out to be load-
bearing for the demo story: a recruiter visiting the portfolio site should see
curated public runs, not all runs, and certainly not the full metadata.

The distinction between `mode` (how the run was computed) and `public_demo`
(who can see it) and `keep` (cleanup protection) is a genuine three-dimensional
design space. Treating them as the same thing early would have created
migration debt. Building the three-axis model early, even if the controls
weren't wired to the UI, would have been cheaper.

---

**Separate public and private visibility from model mode earlier.**

Related to the above. Early in the design, demo mode meant "Haiku orchestrator,
one iteration, cheap" and full mode meant "Sonnet orchestrator, three
iterations, expensive." Both were private.

The portfolio deployment required a third concern: some demo runs should be
publicly visible, some full runs should stay private, and the owner needs to
curate which is which independently of how the run was computed.

This three-way separation (compute mode × visibility × retention) was not in
the original spec. It was discovered when deployment forced the question.
Designing for it earlier would have produced a cleaner data model.

---

**Measure more and speculate less.**

The dual-writer loop's cost estimate was $2–4. The actual cost was $0.79. The
rubric validation guard was expected to reject occasional over-eager additions.
It rejected 30+ additions from Sonnet in a single run. The synthesis rate was
expected to stay high; it dropped from 87% to 50% across two iterations,
showing genuine convergence.

In each case, the real number was more interesting and more useful than the
estimate. The estimates came from reasoning about the design; the measurements
came from running it on real data.

The lesson is not "don't estimate." The lesson is: run on real data as early
as possible, and when the measurement contradicts the estimate, update both the
number and the reasoning that produced the estimate. F-28 (the Sonnet
validation run) changed the cost model, the convergence understanding, and the
rubric guard assessment in a single run. That run should have happened earlier.

---

**Test against real workflows sooner.**

The system was tested against the Airwallex JD throughout development. That
was good. The fit tension it exposed — Microsoft is PM work, not presales;
Xandr is adtech, not fintech — was exactly the kind of real signal that
synthetic test cases don't produce.

What wasn't tested early enough was the application workflow end-to-end: not
just "does the pipeline produce a CV" but "does the produced CV actually
improve the application, and how would I know?" The grounding check is the
beginning of an answer to that question. A user study against real applications
and real outcomes would be the next step — and the one most likely to reveal
what the system is actually optimising for versus what it should be.

---

## Coda

The most useful frame for this project, in retrospect, is not "Week 3 of an AI
learning track." It's a proof of concept for a production pattern that appears
in any serious AI deployment:

- A structured workflow with explicit human decision points
- Multiple models assigned to roles based on observed behaviour, not capability marketing
- Evaluation criteria that are explicit, versioned, and auditable
- An audit trail that is separate from the model's context but inspectable after the fact
- A grounding mechanism that distinguishes between comprehensive and honest

Each of those is a general principle. cv-tailor is one instantiation of them
for one workflow. The next instantiation will be different, but the principles
will transfer.

That transfer is the point of building it this way.

The shift from understanding models to understanding systems is what makes that transfer possible.
