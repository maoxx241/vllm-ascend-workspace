# Behavior contract

The workspace command delegates to `vaws_knowledge.contribution.__main__.main`.
It preserves the package arguments, JSON result and exit status. It neither
implements review rules nor translates new candidates to legacy YAML.

Local candidate Markdown remains unchanged during `prepare`. The package
writes a separate public copy and durable pending state. Repeating preparation
of unchanged content reuses its content identity. A blocked copy stays blocked.

Public submission and review need configured transports. A completed local
prepare is not an uploaded PR, a merge, or independent runtime validation.
