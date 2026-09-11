# Behavior

Map an accessible diff to the minimum evidence needed for a reviewable validation conclusion.

Use --diff-file for an already captured diff. The report classifies affected components, derives supported coverage from actual evidence and exact code identities, and lists missing checks. Agents do not enter coverage labels or lifecycle records.

Read the changed behavior and affected callers before choosing tests. Build, numerical, graph, distributed and performance evidence cover different failure modes. Existing evidence is reusable when its observed code states and scope match the diff.

Execute missing checks with the owning validation, benchmark, profiling or debug skill; this report does not run an NPU experiment.

Progress is written to stderr; stdout contains the structured result. Remote
device execution uses coordinator ownership. Local report construction does not
allocate devices or alter an execution. Reports describe the supplied evidence;
missing evidence is not a passing result.
