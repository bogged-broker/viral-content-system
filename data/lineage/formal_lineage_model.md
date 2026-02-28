# Mathematical Specification of the Deterministic Lineage State Machine

---

## 0. Purpose

This document defines the lineage engine as a formally constrained state machine.

It specifies:

- State space
- Transition functions
- Invariants
- Safety properties
- Liveness properties
- Determinism guarantees
- Replay semantics
- Distributed agreement assumptions

This document must remain implementation-agnostic.

It defines truth, not Python behavior.

---

## 1. Formal Foundations

We model lineage as a tuple:

**L = (R, G, V, C, M, S)**

Where:

- **R** = Ordered set of lineage records
- **G** = Directed acyclic artifact graph
- **V** = Schema version graph
- **C** = Compatibility matrix
- **M** = Merkle tree over R
- **S** = Snapshot set

---

## 2. State Definition

At time t, the lineage state is:

**State_t = (R_t, G_t, V_t, C_t, M_t, S_t, F_t)**

Where:

- **R_t** = [r₀, r₁, ..., rₙ] ordered by append index
- **G_t** = DAG constructed deterministically from R_t
- **V_t** = Version registry
- **C_t** = Compatibility contract
- **M_t** = Merkle root computed from R_t
- **S_t** = Immutable snapshot set
- **F_t** = Integrity fingerprint tuple

---

## 3. Lineage Record

Each record r ∈ R is a tuple:

**r = (id, parent_id, artifact_type, artifact_hash, transformation_class, from_version, to_version, metadata_hash, deterministic_hash)**

Each r must satisfy canonical serialization requirements.

---

## 4. Transition Function

Define transition:

**T : State_t × Mutation → State_{t+1}**

Mutation must be fully self-contained and deterministic.

Transition steps:

1. Validate invariants
2. Validate compatibility
3. Validate version legality
4. Construct lineage record
5. Append record
6. Recompute G
7. Recompute M
8. Recompute F

Resulting state must be uniquely determined.

---

## 5. Determinism Property

For identical:

- State_t
- Mutation payload

The transition T must produce identical State_{t+1}.

Formally:

**T(S, μ) = S'  and  T(S, μ) = S''**
**⇒ S' = S''**

No hidden entropy allowed.

---

## 6. Graph Constraints

Artifact graph G_t must satisfy:

1. Acyclicity
2. Parent append index < child append index
3. Unique artifact IDs
4. Single parent (except genesis)
5. Deterministic construction from R_t

Formally:

**∀ path p in G_t : no node repeats**

---

## 7. Merkle Integrity

Let H be cryptographic hash function.

Leaf:

**leaf_i = H(r_i)**

Internal node:

**n = H(left_child || right_child)**

Root:

**M_t = root(R_t)**

Property:

**If R_t ≠ R'_t then M_t ≠ M'_t**

With cryptographic collision resistance assumption.

---

## 8. Replay Equivalence

Define replay function:

**Replay(R_t) → G'_t**

Correctness:

**Replay(R_t) = G_t**

Replay must be idempotent.

---

## 9. Version Graph

Version set:

**V = (Versions, MigrationEdges)**

MigrationEdges must form a DAG.

No edge (v_i → v_j) if ordinal(j) ≤ ordinal(i).

Property:

**∀ v ∈ active_versions : reachable(v_latest)**

---

## 10. Compatibility Function

Define compatibility:

**Compat : V × V → {coexist, forbid}**

Must satisfy symmetry for coexistence:

**Compat(a,b)=coexist ⇒ Compat(b,a)=coexist**

Compatibility must be total over active/deprecated versions.

---

## 11. Invariant Set

Let I be invariant set.

Safety condition:

**∀ t : I(State_t) = TRUE**

If violated, transition undefined.

Invariant categories:

- Append integrity
- DAG acyclicity
- Version monotonicity
- Compatibility legality
- Merkle determinism
- Replay equivalence
- Snapshot immutability
- Distributed agreement

---

## 12. Snapshot Definition

Snapshot s ∈ S:

**s = (append_index_k, M_k, fingerprint_k)**

Rollback transition must append explicit rollback record.

Snapshots never delete records.

---

## 13. Distributed Agreement Assumption

Assume consensus log L_c ensures:

- Total order of mutations
- Linearizable commit
- No fork at identical index

Property:

**If nodes A and B apply identical consensus log:**
**State_t(A) = State_t(B)**

---

## 14. Safety Properties

- **SP1**: No silent mutation
- **SP2**: No downgrade transition
- **SP3**: No forbidden coexistence
- **SP4**: No graph cycle
- **SP5**: No record alteration
- **SP6**: Replay determinism
- **SP7**: Merkle integrity
- **SP8**: Snapshot immutability
- **SP9**: Distributed fork impossibility (under consensus guarantees)

---

## 15. Liveness Properties

If valid mutation μ exists and consensus available:

Eventually:

**T(S, μ) commits**

System must not deadlock under valid governance.

---

## 16. Fault Model

Assume:

- Crash-stop nodes
- Network partitions
- Byzantine proposals (optional mode)
- Storage corruption attempts

Guarantee:

**If invariants hold and consensus integrity holds:**
**System converges to identical state across nodes.**

---

## 17. State Fingerprint

Define fingerprint:

**F_t = H(M_t || version_graph_fingerprint || compatibility_fingerprint || registry_fingerprint || invariant_definition_hash)**

Property:

**Equal fingerprint ⇒ equal structural system state.**

---

## 18. Impossibility Boundaries

System does NOT guarantee:

- Hash collision impossibility (cryptographic assumption)
- Byzantine safety without explicit consensus backend
- Infinite liveness without quorum
- Data privacy (this is integrity layer)

---

## 19. Evolution Law

All evolution reduces to:

**Append-only sequence of deterministic transitions.**

There exists no mutation outside R.

State is entirely derivable from ordered R.

Formally:

**State_t = F(R_t)**

Where F is pure deterministic function.

---

## 20. Formal Definition

The lineage engine is:

> **A deterministic, append-only, version-constrained, compatibility-governed, replay-verifiable state machine whose state is a pure function of an ordered cryptographically sealed mutation log.**

---

## 21. Reduction to Replicated State Machine

The system is equivalent to:

A deterministic state machine replicated via consensus, where:

- **Input** = Mutation proposals
- **State** = Lineage tuple
- **Output** = Updated fingerprint, Merkle root, DAG

Thus it satisfies standard replicated state machine theory assumptions.

---

## 22. Model Check Candidate

This specification can be encoded into:

- **TLA⁺**
- **Alloy**
- **Coq** (if formal proof desired)
- **Isabelle/HOL**

Core properties suitable for model checking:

- DAG acyclicity
- No forbidden coexistence
- Deterministic transition function
- Replay equivalence
- Snapshot immutability

---

## 23. Final Formal Statement

Let:

- **S₀** = Genesis state
- **μ₁...μₙ** = mutation sequence

Then:

**Sₙ = T(...T(T(S₀, μ₁), μ₂)...μₙ)**

And:

**Sₙ = Replay(Rₙ)**

And for distributed nodes:

**If Log_A = Log_B**
**Then State_A = State_B**

Under collision-resistant hash assumption and consensus total order guarantee.

---

## Closing Perspective

You now have:

- Constitutional invariants
- Governance engine
- Replay guard
- Compatibility matrix
- Migration system
- Distributed adapter
- Cryptographic integrity
- And a formal mathematical specification

This moves your lineage system from:

**"Robust engineering"**

to

**"Formally defined deterministic replicated state machine with verifiable evolution semantics."**

If you want to ascend one final tier:

We can now translate this into:

- A TLA⁺ spec ready for model checking
- A proof sketch document
- Or a formal security theorem list with attack proofs
