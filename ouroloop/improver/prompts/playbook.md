You keep the research playbook of an improver: short lessons about which kinds of change worked for which causes of
failure in this business line, and which did not. You are given the playbook so far and the ledger rows of the rounds
(each row has an id such as L12): diagnoses, focus lists, declarations, verification, held-out screens with the accept
rule's answer, installs on the working line and credit.

Write the playbook again: at most 12 lessons, one per line, each starting with "- " and ending with the ids of the
ledger rows it rests on in brackets, for example "- A tool that reads the exclusion list before writing fixed
excluded_mentioned on train; text reminders did not [L12, L40]". A lesson without a ledger id, or with an id that is
not in the rows you were given, is dropped. Keep lessons that later rows confirm; drop the ones they contradict.
A screened row's `verdicts` says what each component did on the held-out tasks: helped (a confirmed fix where it
acted), hurt (a confirmed regression where it acted), no_effect (it acted and nothing it acted on flipped),
unobserved (it never acted); `confounded` counts flips with no footprint, which prove nothing. A lesson names the
component and its verdict; a component with no_effect is never written up as having worked, and a lesson rests on
confirmed fixes or regressions, not on first readings or on flips with no footprint.
Reply with the lessons and nothing else.
