# Trust Network Architecture & Trade-offs

## 1. Trust Propagation
Transitive trust decays multiplicatively along simple directed paths with a 0.5 damping factor per hop after the first, hard-capped at 2 hops (`HARD_MAX_HOPS = 2`) to prevent cycles and signal dilution. When multiple paths reach a project, the strongest single path wins (`max`), preventing trust inflation via extra intermediaries.

## 2. Adversarial Resilience
Denouncements are weighted by the querying project's local trust in the denouncing project. A contributor is blocked only if a single highly trusted denouncer reaches `block_trust` (0.7) OR at least 2 credible denouncers (each >= 0.2 trust) corroborate with a combined noisy-OR weight >= 0.5. An isolated low-trust denouncement only raises a `WARNING` for human review, and a project's own local vouch overrides remote denouncements.

## 3. Sybil Resistance
Trust is computed locally from the querying project's seed set (Personalized PageRank style). Outbound trust only flows from seeds; inbound edges from untrusted repos are ignored. A self-vouching ring of 50 fake repositories unreachable within 2 hops evaluates strictly to 0.0 trust and is listed in `ignored_projects`.

## 4. Newcomer Inclusion
Unvouched contributors (`Status.UNKNOWN`) with zero denouncements and no cross-project spam campaign match are routed to `NEWCOMER_TRIAGE` instead of being auto-closed, allowing genuine first-time contributors and students to be reviewed and vouched.

## 5. Expiry & Appeals
Minor denouncements can expire via git commit timestamps (TTL), and any maintainer can directly override a remote denouncement by adding a local vouch in their repository's `.github/VOUCHED.td`.

## 6. Privacy-Preserving Campaign Detection
Repositories share only normalized structural fingerprints (64-slot MinHash signatures over 3-character text shingles + blake2b-hashed changed file paths), salted author hashes, and timestamps—never raw PR text, IP addresses, or personal emails.
