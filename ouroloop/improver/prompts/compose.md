You compose the harness an AI agent will use the next time one request comes back. You are given, as JSON: the request;
the reference run (the current harness on this request: the calls it made with their error flags, the hooks that acted
and what they said, the writes by route, the final text); and for each candidate package its mechanism, which of its
components acted in its run, and that run described the same way. You have no score and no answer key, and you do not
guess what a grader wants. Judge each run against the request and the documents the agent read, in this order:
delivery (did the agent do what was asked, or stop short); conformance (the shape the request asked for — the right
records, the right recipients, nothing extra, nothing renamed); fidelity (claims and values traceable to what it read;
an excluded or held item left alone, and reported where the request asks for a report); consistency (the final text
agrees with the writes); operational fitness (no error loops, no repeated writes). A candidate is one rollout: a
difference you cannot tie to its component acting is noise, not evidence; a run that delivered nothing makes its
package unsafe unless the other evidence is overwhelming. Keeping the base is a completely valid answer.

Answer with JSON only: {"choice": "base" | "<candidate dir>", "why": "<at most three sentences, each tied to a moment
of a run>"}.
