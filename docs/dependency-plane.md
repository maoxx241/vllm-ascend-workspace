# Dependency plane

Status: current

The four external repositories (remote-dev, vaws-coordinator, vaws-top,
vaws-knowledge) are consumed through one pin schema, one locator, and one
bootstrap. Check workspace capability with
`python3 .agents/scripts/vaws_deps.py status|bootstrap|doctor` before assuming
remote endpoints, the task pool, fleet observation, or shared knowledge are
available. A `partial` outcome names what is missing and the bootstrap
command.

Pins live under `.agents/deps/`. This page is the consumer-facing contract
for that plane; the command itself is landed by the sibling dependency-plane
package.
