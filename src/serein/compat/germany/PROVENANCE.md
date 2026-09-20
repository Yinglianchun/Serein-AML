# Compatibility implementation provenance
These compat algorithms derive from the author's earlier Ombre-Brain implementation,
upstream revision 9c99c591d0ff810b4d9f8691c829dbb50d45c0be, adapted by Serein.
Public derivation starts at Serein ea49ac918bd717206004e2c92c044cd3fa327b7e.
The package name records the compatibility boundary; no deployment is discovered.
Public changes replace private defaults, examples and identity values while preserving
evidence, lifecycle, mixed selection and post-selection cooldown behavior.
Automatic mixed recall has no universal cosine floor. Explicit lookup keeps its caller cutoff.
