---
name: trello-card-update-skill
description: "Use this skill whenever the user asks for a Trello card update, daily update, status update, sprint update, progress note, or short engineering update. The goal is to produce a short, human, technically grounded update that can be pasted directly into Trello."
---
 
## Inherited Hard Rules

Outward-text rules come from the dhiraj-writing-style skill and apply here: no em dashes, no AI-sounding filler, plain human language.

Preferred raw material: the closing summary from a pipeline skill (What changed / Validation / Residual risk from bidgenie-feature-implementer, Findings / Root cause from bidgenie-beast-readonly-debug). When the session has one, build the update from it instead of asking the user to restate the work. Suite handoffs live in `../_shared/suite_map.md`.

## Default Style
 
Write in a direct, first-person, engineering-status style.
 
The update should be:
 
* Short
* Specific
* Reviewer-friendly
* Technically grounded
* Free of corporate fluff
* Not overly polished
* Not a PR description
* Not a long discovery report
Avoid emojis, hype, vague progress language, and long architecture explanations.
 
## Default Structure
 
Use this format unless the user asks otherwise:
 
```md
Today’s Update
 
- I completed [work done] around [module / issue / area].
- The main finding is [specific technical finding], especially around [failure mode / bottleneck / behavior].
- I’m now moving toward [next implementation step], so we can [expected benefit].
- Blocker / open question: [only include if real].
```
 
## What to Include
 
Always try to include:
 
1. What was done
   Examples: discovery, implementation, debugging, testing, PR cleanup, report writing, migration work.
2. What was found
   Include concrete findings, not generic progress.
3. What is next
   One clear next step is usually enough.
4. Blockers or risks
   Include only if useful. Keep it to one line.
5. Specific technical references
   Mention function names, files, scripts, prompts, or modules when relevant.
Good examples of useful technical references:
 
* `allocate_budget_within_work_topics`
* `scripts/llm_prompt_validation/`
* `rfp_data`
* `json_mode`
* `function_calling`
* `plaintext fallback`
* `consolidated_budget_projection_prompt`
## What to Avoid
 
Do not write:
 
* Long explanations
* Overly formal status reports
* “Made significant progress”
* “Worked on multiple aspects”
* “Leveraged robust architecture”
* PR-style implementation details unless asked
* Huge bullet lists
* Vague statements without findings
## Preferred Tone
 
Use language that sounds like the user wrote it.
 
Good tone:
 
```md
I completed a discovery pass on the budget prompt fallback behavior and narrowed the issue down to output parsing reliability. The main finding is that `json_mode` is effectively not useful here, while `function_calling` works only with retries and still adds a lot of wallclock time.
```
 
Bad tone:
 
```md
I made significant progress on enhancing the robustness of the budget allocation system by evaluating multiple structured output pathways and identifying opportunities for optimization.
```
 
## Alternate Format: Current Direction
 
Use this when the user is trying to explain an implementation direction or decision:
 
```md
Current Direction
 
I think the clean direction is to [decision]. The main reason is that [reason], and this keeps the implementation [simple / reusable / consistent].
 
What can be reused:
- [Reusable component 1]
- [Reusable component 2]
- [Reusable component 3]
 
What should not be reused directly:
- [Boundary / reason]
 
Next step:
- [Concrete implementation step]
```
 
## Alternate Format: Discovery Update
 
Use this when the user has been investigating a technical issue:
 
```md
Today’s Update
 
- I did a discovery pass on [problem/module] and confirmed that [main finding].
- The main issue is [specific technical issue], especially around [failure mode / bottleneck / unclear behavior].
- This is impacting [latency / reliability / output quality / debugging clarity].
- Next, I’m going to [concrete next step].
```
 
## Alternate Format: Implementation Update
 
Use this when the user has already started coding:
 
```md
Today’s Update
 
- I started implementing [feature/fix] in [area/file/module].
- The change currently focuses on [specific behavior being added or changed].
- I’m keeping the implementation [simple / config-driven / isolated / reusable] so that [reason].
- Next, I’ll test this against [specific case / script / flow].
```
 
## Final Quality Check
 
Before responding, check:
 
* Can this be pasted directly into Trello?
* Is it short enough?
* Does it mention the actual technical thing?
* Does it explain the finding or impact?
* Is there a clear next step?
* Did I remove fluff?