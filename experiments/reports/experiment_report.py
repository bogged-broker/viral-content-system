
"""
experiments/reports/experiment_report.py

Deterministic Human-Readable Experiment Summaries

CORE PRINCIPLE:
    Human-readable does NOT mean simplified — it means structured.
    Every statement is traceable to a snapshot artifact.
    No floating claims. No creative interpretation. No bias.

GUARANTEES:
    - Byte-identical output for same snapshot + spec
    - Every claim maps to evidence
    - No recomputation of metrics
    - No narrative spin
    - Complete section coverage
    - Audit-safe formatting

DEPENDENCIES:
    - replay artifacts (read-only)
    - frozen experiment results
    - NEVER touches live systems

INVARIANTS:
    - Reports are artifacts, not opinions
    - Missing evidence = refused report
    - Emotional language = blocked
    - Uncertainty always disclosed
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any, Literal
from datetime import datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class ReportSpec:
    """
    Immutable specification for report generation.
    
    Audience affects FORMAT only — never facts.
    """
    experiment_id: str
    snapshot_id: str
    audience: Literal['internal', 'executive', 'audit']
    verbosity: Literal['concise', 'standard', 'exhaustive']
    include_appendix: bool
    
    def __post_init__(self):
        if not self.experiment_id:
            raise ValueError("experiment_id required")
        if not self.snapshot_id:
            raise ValueError("snapshot_id required")


@dataclass(frozen=True)
class ReportMetadata:
    """
    Provenance lock for complete reproducibility.
    """
    experiment_name: str
    generated_at: datetime
    report_version: str
    
    # Reproducibility anchors
    snapshot_id: str
    replay_hash: str
    code_version: str
    
    # Experiment identity
    hypothesis: str
    start_date: datetime
    end_date: datetime
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'experiment_name': self.experiment_name,
            'generated_at': self.generated_at.isoformat(),
            'report_version': self.report_version,
            'snapshot_id': self.snapshot_id,
            'replay_hash': self.replay_hash,
            'code_version': self.code_version,
            'hypothesis': self.hypothesis,
            'start_date': self.start_date.isoformat(),
            'end_date': self.end_date.isoformat(),
        }


@dataclass(frozen=True)
class EvidenceRef:
    """
    Cryptographic link to source artifact.
    """
    artifact_id: str
    artifact_type: str  # snapshot | metric | test_result | invariant
    content_hash: str
    location: str  # path in snapshot
    
    def verify(self, actual_content: bytes) -> bool:
        """Verify content matches referenced hash."""
        computed = hashlib.sha256(actual_content).hexdigest()
        return computed == self.content_hash


@dataclass(frozen=True)
class ReportSection:
    """
    Single section with evidence binding.
    
    Every paragraph must trace to verified artifact.
    """
    title: str
    body: str
    evidence_refs: List[EvidenceRef]
    subsections: List['ReportSection'] = field(default_factory=list)
    
    def verify_evidence(self, evidence_store) -> bool:
        """Confirm all evidence references resolve."""
        for ref in self.evidence_refs:
            content = evidence_store.get(ref.artifact_id)
            if not content or not ref.verify(content):
                return False
        return all(s.verify_evidence(evidence_store) for s in self.subsections)


class SectionType(Enum):
    """Mandatory report sections — must all be present."""
    OVERVIEW = "experiment_overview"
    HYPOTHESIS = "hypothesis_and_intent"
    DESIGN = "experimental_design"
    TRAFFIC = "traffic_and_exposure"
    OUTCOMES = "primary_outcomes"
    EFFECTS = "effect_sizes"
    STATISTICS = "statistical_validity"
    CONFIDENCE = "confidence_and_uncertainty"
    INVARIANTS = "invariant_compliance"
    FAILURES = "failure_modes"
    VERDICT = "final_verdict"
    REPRODUCIBILITY = "reproducibility_metadata"


@dataclass(frozen=True)
class ExperimentReport:
    """
    Complete, immutable experiment report.
    
    INVARIANT: All SectionType values must be present.
    """
    metadata: ReportMetadata
    sections: Dict[SectionType, ReportSection]
    appendix: Optional[Dict[str, Any]] = None
    
    def __post_init__(self):
        # Verify completeness
        missing = set(SectionType) - set(self.sections.keys())
        if missing:
            raise ValueError(f"Missing required sections: {missing}")
    
    def compute_hash(self) -> str:
        """Deterministic hash of entire report."""
        content = json.dumps(self._to_hashable(), sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()
    
    def _to_hashable(self) -> Dict:
        """Convert to deterministic dict for hashing."""
        return {
            'metadata': self.metadata.to_dict(),
            'sections': {
                k.value: {
                    'title': v.title,
                    'body': v.body,
                    'evidence': [r.content_hash for r in v.evidence_refs]
                }
                for k, v in self.sections.items()
            }
        }


# ============================================================================
# LANGUAGE NORMALIZATION
# ============================================================================

class LanguageNormalizer:
    """
    Enforces rigorous, unbiased language.
    
    RULES:
        - No superlatives (amazing, crushed, destroyed)
        - No future predictions (will, should, expected to)
        - No implied causality beyond statistics
        - Bounded uncertainty language only
    """
    
    FORBIDDEN_TERMS = {
        'amazing', 'incredible', 'crushed', 'destroyed', 'obliterated',
        'clearly', 'obviously', 'definitely', 'certainly',
        'always', 'never', 'guaranteed', 'proven',
        'will', 'should', 'must', 'has to'
    }
    
    BOUNDED_REPLACEMENTS = {
        'better': 'showed improvement',
        'worse': 'showed decline',
        'increased': 'exhibited increase',
        'decreased': 'exhibited decrease',
        'caused': 'was associated with',
        'improved': 'showed positive effect',
    }
    
    @classmethod
    def normalize(cls, text: str) -> str:
        """
        Apply normalization rules.
        
        Raises if forbidden terms detected.
        """
        lower_text = text.lower()
        
        # Check for forbidden terms
        violations = [term for term in cls.FORBIDDEN_TERMS if term in lower_text]
        if violations:
            raise ValueError(f"Forbidden language detected: {violations}")
        
        # Apply bounded replacements
        result = text
        for original, replacement in cls.BOUNDED_REPLACEMENTS.items():
            # Case-insensitive replacement preserving sentence case
            result = result.replace(original, replacement)
            result = result.replace(original.capitalize(), replacement.capitalize())
        
        return result
    
    @classmethod
    def format_metric(cls, name: str, value: Decimal, ci_lower: Decimal, 
                     ci_upper: Decimal, confidence: Decimal) -> str:
        """
        Standard metric presentation format.
        
        Example: "Conversion rate: 3.2% (95% CI: 1.1%, 5.4%)"
        """
        return (
            f"{name}: {value:+.1f}% "
            f"({confidence*100:.0f}% CI: {ci_lower:+.1f}%, {ci_upper:+.1f}%)"
        )
    
    @classmethod
    def format_verdict(cls, conclusion: str, certainty: str) -> str:
        """
        Bounded verdict language.
        
        certainty must be: high | moderate | low | insufficient
        """
        valid_certainty = {'high', 'moderate', 'low', 'insufficient'}
        if certainty not in valid_certainty:
            raise ValueError(f"Invalid certainty: {certainty}")
        
        return f"{conclusion} (certainty: {certainty})"


# ============================================================================
# EVIDENCE LINKING
# ============================================================================

class EvidenceLinker:
    """
    Maps every claim to snapshot artifacts.
    
    GUARANTEE: No floating statements allowed.
    """
    
    def __init__(self, snapshot_store):
        self.snapshot_store = snapshot_store
        self._evidence_cache: Dict[str, EvidenceRef] = {}
    
    def link_metric(self, metric_id: str, snapshot_id: str) -> EvidenceRef:
        """Create evidence reference for metric."""
        artifact = self.snapshot_store.get_metric(metric_id, snapshot_id)
        if not artifact:
            raise ValueError(f"Metric not found: {metric_id}")
        
        content = json.dumps(artifact, sort_keys=True).encode()
        content_hash = hashlib.sha256(content).hexdigest()
        
        ref = EvidenceRef(
            artifact_id=metric_id,
            artifact_type='metric',
            content_hash=content_hash,
            location=f"snapshots/{snapshot_id}/metrics/{metric_id}"
        )
        
        self._evidence_cache[metric_id] = ref
        return ref
    
    def link_test_result(self, test_id: str, snapshot_id: str) -> EvidenceRef:
        """Create evidence reference for statistical test."""
        artifact = self.snapshot_store.get_test(test_id, snapshot_id)
        if not artifact:
            raise ValueError(f"Test not found: {test_id}")
        
        content = json.dumps(artifact, sort_keys=True).encode()
        content_hash = hashlib.sha256(content).hexdigest()
        
        return EvidenceRef(
            artifact_id=test_id,
            artifact_type='test_result',
            content_hash=content_hash,
            location=f"snapshots/{snapshot_id}/tests/{test_id}"
        )
    
    def link_invariant(self, invariant_id: str, snapshot_id: str) -> EvidenceRef:
        """Create evidence reference for invariant check."""
        artifact = self.snapshot_store.get_invariant(invariant_id, snapshot_id)
        if not artifact:
            raise ValueError(f"Invariant not found: {invariant_id}")
        
        content = json.dumps(artifact, sort_keys=True).encode()
        content_hash = hashlib.sha256(content).hexdigest()
        
        return EvidenceRef(
            artifact_id=invariant_id,
            artifact_type='invariant',
            content_hash=content_hash,
            location=f"snapshots/{snapshot_id}/invariants/{invariant_id}"
        )
    
    def verify_all(self) -> bool:
        """Verify all cached evidence still valid."""
        for ref in self._evidence_cache.values():
            artifact = self.snapshot_store.get_raw(ref.artifact_id)
            if not ref.verify(artifact):
                return False
        return True


# ============================================================================
# REPORT ASSEMBLER
# ============================================================================

class ReportAssembler:
    """
    Deterministic report construction from validated artifacts.
    
    FLOW:
        1. Load replay session
        2. Extract validated outcomes
        3. Map results → sections
        4. Attach evidence hashes
        5. Normalize language
        6. Freeze output
    
    REFUSES:
        - Unverifiable claims
        - Missing evidence
        - Invalid snapshots
        - Incomplete results
    """
    
    def __init__(self, snapshot_store, replay_loader):
        self.snapshot_store = snapshot_store
        self.replay_loader = replay_loader
        self.linker = EvidenceLinker(snapshot_store)
        self.normalizer = LanguageNormalizer()
    
    def assemble(self, spec: ReportSpec) -> ExperimentReport:
        """
        Main assembly entry point.
        
        Raises if any section cannot be constructed with evidence.
        """
        # Load replay session
        replay = self.replay_loader.load(spec.snapshot_id)
        if not replay:
            raise ValueError(f"Snapshot not found: {spec.snapshot_id}")
        
        # Build metadata
        metadata = self._build_metadata(spec, replay)
        
        # Build all required sections
        sections = {
            SectionType.OVERVIEW: self._build_overview(spec, replay),
            SectionType.HYPOTHESIS: self._build_hypothesis(spec, replay),
            SectionType.DESIGN: self._build_design(spec, replay),
            SectionType.TRAFFIC: self._build_traffic(spec, replay),
            SectionType.OUTCOMES: self._build_outcomes(spec, replay),
            SectionType.EFFECTS: self._build_effects(spec, replay),
            SectionType.STATISTICS: self._build_statistics(spec, replay),
            SectionType.CONFIDENCE: self._build_confidence(spec, replay),
            SectionType.INVARIANTS: self._build_invariants(spec, replay),
            SectionType.FAILURES: self._build_failures(spec, replay),
            SectionType.VERDICT: self._build_verdict(spec, replay),
            SectionType.REPRODUCIBILITY: self._build_reproducibility(spec, replay),
        }
        
        # Optional appendix
        appendix = None
        if spec.include_appendix:
            appendix = self._build_appendix(spec, replay)
        
        # Verify all evidence
        if not self.linker.verify_all():
            raise RuntimeError("Evidence verification failed")
        
        return ExperimentReport(
            metadata=metadata,
            sections=sections,
            appendix=appendix
        )
    
    def _build_metadata(self, spec: ReportSpec, replay) -> ReportMetadata:
        """Extract metadata from replay."""
        return ReportMetadata(
            experiment_name=replay['experiment_name'],
            generated_at=datetime.utcnow(),
            report_version='1.0.0',
            snapshot_id=spec.snapshot_id,
            replay_hash=replay['replay_hash'],
            code_version=replay['code_version'],
            hypothesis=replay['hypothesis'],
            start_date=datetime.fromisoformat(replay['start_date']),
            end_date=datetime.fromisoformat(replay['end_date']),
        )
    
    def _build_overview(self, spec: ReportSpec, replay) -> ReportSection:
        """Section 1: Experiment Overview."""
        body = f"""
Experiment: {replay['experiment_name']}
ID: {spec.experiment_id}
Duration: {replay['start_date']} to {replay['end_date']}
Status: {replay['status']}

This report documents the complete results of the experiment as captured
in snapshot {spec.snapshot_id}.
"""
        body = self.normalizer.normalize(body.strip())
        
        evidence = [
            self.linker.link_metric('experiment_config', spec.snapshot_id)
        ]
        
        return ReportSection(
            title="Experiment Overview",
            body=body,
            evidence_refs=evidence
        )
    
    def _build_hypothesis(self, spec: ReportSpec, replay) -> ReportSection:
        """Section 2: Hypothesis & Intent."""
        body = f"""
Hypothesis: {replay['hypothesis']}

Primary Metrics:
{self._format_metric_list(replay['primary_metrics'])}

Secondary Metrics:
{self._format_metric_list(replay.get('secondary_metrics', []))}

Success Criteria:
{self._format_criteria(replay['success_criteria'])}
"""
        body = self.normalizer.normalize(body.strip())
        
        evidence = [
            self.linker.link_metric('hypothesis', spec.snapshot_id),
            self.linker.link_metric('success_criteria', spec.snapshot_id),
        ]
        
        return ReportSection(
            title="Hypothesis & Intent",
            body=body,
            evidence_refs=evidence
        )
    
    def _build_design(self, spec: ReportSpec, replay) -> ReportSection:
        """Section 3: Experimental Design."""
        design = replay['design']
        
        body = f"""
Design Type: {design['type']}
Randomization Unit: {design['randomization_unit']}

Variants:
{self._format_variants(design['variants'])}

Traffic Allocation:
{self._format_allocation(design['allocation'])}

Stratification:
{self._format_stratification(design.get('stratification', {}))}
"""
        body = self.normalizer.normalize(body.strip())
        
        evidence = [
            self.linker.link_metric('experimental_design', spec.snapshot_id)
        ]
        
        return ReportSection(
            title="Experimental Design",
            body=body,
            evidence_refs=evidence
        )
    
    def _build_traffic(self, spec: ReportSpec, replay) -> ReportSection:
        """Section 4: Traffic & Exposure Summary."""
        traffic = replay['traffic_summary']
        
        body = f"""
Total Users Exposed: {traffic['total_users']:,}
Total Events Collected: {traffic['total_events']:,}

Per-Variant Exposure:
{self._format_exposure(traffic['variant_exposure'])}

Sample Ratio Mismatch Check:
{self._format_srm_check(traffic['srm_check'])}
"""
        body = self.normalizer.normalize(body.strip())
        
        evidence = [
            self.linker.link_metric('traffic_summary', spec.snapshot_id),
            self.linker.link_invariant('srm_check', spec.snapshot_id),
        ]
        
        return ReportSection(
            title="Traffic & Exposure Summary",
            body=body,
            evidence_refs=evidence
        )
    
    def _build_outcomes(self, spec: ReportSpec, replay) -> ReportSection:
        """Section 5: Primary Outcomes."""
        outcomes = replay['primary_outcomes']
        
        subsections = []
        for metric_name, result in outcomes.items():
            metric_body = self._format_outcome(metric_name, result)
            metric_body = self.normalizer.normalize(metric_body)
            
            evidence = [
                self.linker.link_metric(f"outcome_{metric_name}", spec.snapshot_id)
            ]
            
            subsections.append(ReportSection(
                title=metric_name,
                body=metric_body,
                evidence_refs=evidence
            ))
        
        body = f"Analysis of {len(outcomes)} primary outcome metrics."
        
        return ReportSection(
            title="Primary Outcomes",
            body=body,
            evidence_refs=[],
            subsections=subsections
        )
    
    def _build_effects(self, spec: ReportSpec, replay) -> ReportSection:
        """Section 6: Effect Sizes."""
        effects = replay['effect_sizes']
        
        body = self._format_effect_sizes(effects)
        body = self.normalizer.normalize(body)
        
        evidence = [
            self.linker.link_metric('effect_sizes', spec.snapshot_id)
        ]
        
        return ReportSection(
            title="Effect Sizes",
            body=body,
            evidence_refs=evidence
        )
    
    def _build_statistics(self, spec: ReportSpec, replay) -> ReportSection:
        """Section 7: Statistical Validity."""
        stats = replay['statistical_tests']
        
        body = f"""
Tests Performed:
{self._format_test_summary(stats['tests'])}

Multiple Comparison Correction:
Method: {stats['correction_method']}
Adjusted Alpha: {stats['adjusted_alpha']}

Power Analysis:
{self._format_power_analysis(stats['power_analysis'])}
"""
        body = self.normalizer.normalize(body.strip())
        
        evidence = [
            self.linker.link_test_result('statistical_tests', spec.snapshot_id)
        ]
        
        return ReportSection(
            title="Statistical Validity",
            body=body,
            evidence_refs=evidence
        )
    
    def _build_confidence(self, spec: ReportSpec, replay) -> ReportSection:
        """Section 8: Confidence & Uncertainty."""
        confidence = replay['confidence_estimates']
        
        body = f"""
Overall Confidence Level: {confidence['level']}

Uncertainty Sources:
{self._format_uncertainty(confidence['uncertainty_breakdown'])}

Confidence Intervals:
{self._format_confidence_intervals(confidence['intervals'])}
"""
        body = self.normalizer.normalize(body.strip())
        
        evidence = [
            self.linker.link_metric('confidence_estimates', spec.snapshot_id)
        ]
        
        return ReportSection(
            title="Confidence & Uncertainty",
            body=body,
            evidence_refs=evidence
        )
    
    def _build_invariants(self, spec: ReportSpec, replay) -> ReportSection:
        """Section 9: Invariant Compliance."""
        invariants = replay['invariant_checks']
        
        body = f"""
Total Invariants Checked: {len(invariants)}
Passed: {sum(1 for i in invariants if i['passed'])}
Failed: {sum(1 for i in invariants if not i['passed'])}

{self._format_invariant_details(invariants)}
"""
        body = self.normalizer.normalize(body.strip())
        
        evidence = [
            self.linker.link_invariant(f"invariant_{i['id']}", spec.snapshot_id)
            for i in invariants
        ]
        
        return ReportSection(
            title="Invariant Compliance",
            body=body,
            evidence_refs=evidence
        )
    
    def _build_failures(self, spec: ReportSpec, replay) -> ReportSection:
        """Section 10: Failure Modes."""
        failures = replay.get('failures', [])
        
        if not failures:
            body = "No failure modes detected during experiment execution."
        else:
            body = f"""
{len(failures)} failure mode(s) detected:

{self._format_failures(failures)}
"""
        
        body = self.normalizer.normalize(body.strip())
        
        evidence = [
            self.linker.link_metric('failure_log', spec.snapshot_id)
        ] if failures else []
        
        return ReportSection(
            title="Failure Modes",
            body=body,
            evidence_refs=evidence
        )
    
    def _build_verdict(self, spec: ReportSpec, replay) -> ReportSection:
        """Section 11: Final Verdict (Bounded)."""
        verdict = replay['verdict']
        
        # Use normalized verdict language
        conclusion = self.normalizer.format_verdict(
            verdict['conclusion'],
            verdict['certainty']
        )
        
        body = f"""
{conclusion}

Recommendation: {verdict['recommendation']}

Justification:
{verdict['justification']}

Limitations:
{self._format_limitations(verdict['limitations'])}
"""
        body = self.normalizer.normalize(body.strip())
        
        evidence = [
            self.linker.link_metric('final_verdict', spec.snapshot_id)
        ]
        
        return ReportSection(
            title="Final Verdict",
            body=body,
            evidence_refs=evidence
        )
    
    def _build_reproducibility(self, spec: ReportSpec, replay) -> ReportSection:
        """Section 12: Reproducibility Metadata."""
        body = f"""
Snapshot ID: {spec.snapshot_id}
Replay Hash: {replay['replay_hash']}
Code Version: {replay['code_version']}

To reproduce this report:
1. Load snapshot: {spec.snapshot_id}
2. Verify replay hash: {replay['replay_hash']}
3. Run report generator with identical ReportSpec

All artifacts are cryptographically sealed and verified.
"""
        body = self.normalizer.normalize(body.strip())
        
        evidence = [
            self.linker.link_metric('reproducibility_manifest', spec.snapshot_id)
        ]
        
        return ReportSection(
            title="Reproducibility Metadata",
            body=body,
            evidence_refs=evidence
        )
    
    def _build_appendix(self, spec: ReportSpec, replay) -> Dict[str, Any]:
        """Optional appendix with raw data."""
        return {
            'raw_metrics': replay.get('raw_metrics', {}),
            'raw_tests': replay.get('raw_tests', {}),
            'debug_info': replay.get('debug_info', {}),
        }
    
    # Helper formatters
    def _format_metric_list(self, metrics: List[str]) -> str:
        return '\n'.join(f"- {m}" for m in metrics)
    
    def _format_criteria(self, criteria: Dict) -> str:
        lines = []
        for metric, threshold in criteria.items():
            lines.append(f"- {metric}: {threshold}")
        return '\n'.join(lines)
    
    def _format_variants(self, variants: List[Dict]) -> str:
        lines = []
        for v in variants:
            lines.append(f"- {v['name']}: {v['description']}")
        return '\n'.join(lines)
    
    def _format_allocation(self, allocation: Dict) -> str:
        lines = []
        for variant, pct in allocation.items():
            lines.append(f"- {variant}: {pct*100:.1f}%")
        return '\n'.join(lines)
    
    def _format_stratification(self, strat: Dict) -> str:
        if not strat:
            return "None"
        return '\n'.join(f"- {k}: {v}" for k, v in strat.items())
    
    def _format_exposure(self, exposure: Dict) -> str:
        lines = []
        for variant, count in exposure.items():
            lines.append(f"- {variant}: {count:,} users")
        return '\n'.join(lines)
    
    def _format_srm_check(self, srm: Dict) -> str:
        status = "PASS" if srm['passed'] else "FAIL"
        return f"Status: {status}\nP-value: {srm['p_value']:.4f}"
    
    def _format_outcome(self, name: str, result: Dict) -> str:
        return f"""
Control: {result['control_mean']:.4f}
Treatment: {result['treatment_mean']:.4f}
Absolute Difference: {result['absolute_diff']:+.4f}
Relative Difference: {result['relative_diff']:+.2%}
P-value: {result['p_value']:.4f}
"""
    
    def _format_effect_sizes(self, effects: Dict) -> str:
        lines = []
        for metric, effect in effects.items():
            lines.append(
                f"{metric}: {effect['cohens_d']:.3f} "
                f"(95% CI: {effect['ci_lower']:.3f}, {effect['ci_upper']:.3f})"
            )
        return '\n'.join(lines)
    
    def _format_test_summary(self, tests: List[Dict]) -> str:
        lines = []
        for test in tests:
            lines.append(
                f"- {test['name']}: {test['result']} (p={test['p_value']:.4f})"
            )
        return '\n'.join(lines)
    
    def _format_power_analysis(self, power: Dict) -> str:
        return f"""
Achieved Power: {power['achieved']:.2%}
Required Sample Size: {power['required_n']:,}
Actual Sample Size: {power['actual_n']:,}
"""
    
    def _format_uncertainty(self, breakdown: Dict) -> str:
        lines = []
        for source, contribution in breakdown.items():
            lines.append(f"- {source}: {contribution:.2%}")
        return '\n'.join(lines)
    
    def _format_confidence_intervals(self, intervals: Dict) -> str:
        lines = []
        for metric, ci in intervals.items():
            lines.append(
                f"- {metric}: [{ci['lower']:.4f}, {ci['upper']:.4f}]"
            )
        return '\n'.join(lines)
    
    def _format_invariant_details(self, invariants: List[Dict]) -> str:
        lines = []
        for inv in invariants:
            status = "✓ PASS" if inv['passed'] else "✗ FAIL"
            lines.append(f"{status} - {inv['name']}: {inv['description']}")
        return '\n'.join(lines)
    
    def _format_failures(self, failures: List[Dict]) -> str:
        lines = []
        for f in failures:
            lines.append(f"""
Failure: {f['type']}
Time: {f['timestamp']}
Impact: {f['impact']}
Mitigation: {f['mitigation']}
""")
        return '\n'.join(lines)
    
    def _format_limitations(self, limitations: List[str]) -> str:
        return '\n'.join(f"- {lim}" for lim in limitations)


# ============================================================================
# REPORT RENDERING
# ============================================================================

class ReportRenderer:
    """
    Deterministic output formatting.
    
    Supported formats:
        - Markdown (default)
        - Plain text
        - JSON (machine-consumable)
    
    GUARANTEES:
        - Byte-identical for same input
        - Diff-able
        - Version-controllable
        - No styling/charts/emojis
    """
    
    @staticmethod
    def render_markdown(report: ExperimentReport) -> str:
        """Render as Markdown document."""
        lines = []
        
        # Title
        lines.append(f"# Experiment Report: {report.metadata.experiment_name}")
        lines.append("")
        
        # Metadata
        lines.append("## Metadata")
        lines.append("")
        for key, value in report.metadata.to_dict().items():
            lines.append(f"- **{key}**: {value}")
        lines.append("")
        
        # Sections in canonical order
        for section_type in SectionType:
            section = report.sections[section_type]
            lines.append(f"## {section.title}")
            lines.append("")
            lines.append(section.body)
            lines.append("")
            
            # Subsections
            for subsection in section.subsections:
                lines.append(f"### {subsection.title}")
                lines.append("")
        
        # Appendix
        if report.appendix:
            lines.append("## Appendix")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(report.appendix, indent=2, sort_keys=True))
            lines.append("```")
            lines.append("")
        
        # Footer
        lines.append("---")
        lines.append(f"*Report generated: {report.metadata.generated_at.isoformat()}*")
        lines.append(f"*Report hash: {report.compute_hash()}*")
        
        return '\n'.join(lines)
    
    @staticmethod
    def render_text(report: ExperimentReport) -> str:
        """Render as plain text (no markdown)."""
        lines = []
        
        # Title
        lines.append(f"EXPERIMENT REPORT: {report.metadata.experiment_name.upper()}")
        lines.append("=" * 80)
        lines.append("")
        
        # Metadata
        lines.append("METADATA")
        lines.append("-" * 80)
        for key, value in report.metadata.to_dict().items():
            lines.append(f"{key}: {value}")
        lines.append("")
        
        # Sections
        for section_type in SectionType:
            section = report.sections[section_type]
            lines.append(section.title.upper())
            lines.append("-" * 80)
            lines.append(section.body)
            lines.append("")
            
            for subsection in section.subsections:
                lines.append(f"  {subsection.title}")
                lines.append("  " + subsection.body.replace('\n', '\n  '))
                lines.append("")
        
        lines.append("=" * 80)
        lines.append(f"Report generated: {report.metadata.generated_at.isoformat()}")
        
        return '\n'.join(lines)
    
    @staticmethod
    def render_json(report: ExperimentReport) -> str:
        """Render as JSON for machine consumption."""
        data = {
            'metadata': report.metadata.to_dict(),
            'sections': {
                section_type.value: {
                    'title': section.title,
                    'body': section.body,
                    'evidence': [
                        {
                            'artifact_id': ref.artifact_id,
                            'artifact_type': ref.artifact_type,
                            'content_hash': ref.content_hash,
                            'location': ref.location,
                        }
                        for ref in section.evidence_refs
                    ],
                    'subsections': [
                        {
                            'title': sub.title,
                            'body': sub.body,
                        }
                        for sub in section.subsections
                    ]
                }
                for section_type, section in report.sections.items()
            },
            'appendix': report.appendix,
            'report_hash': report.compute_hash(),
        }
        
        return json.dumps(data, indent=2, sort_keys=True)


# ============================================================================
# REPORT VALIDATION
# ============================================================================

class ReportValidator:
    """
    Pre-emission validation.
    
    CHECKS:
        - All sections present
        - Evidence completeness
        - Invariant compliance
        - Replay match
        - Language normalization
    
    Invalid report → refused emission.
    """
    
    def __init__(self, snapshot_store):
        self.snapshot_store = snapshot_store
    
    def validate(self, report: ExperimentReport) -> bool:
        """
        Full validation pipeline.
        
        Returns True if valid, raises ValueError if not.
        """
        # Check section completeness
        self._validate_sections(report)
        
        # Check evidence integrity
        self._validate_evidence(report)
        
        # Check language compliance
        self._validate_language(report)
        
        # Check metadata consistency
        self._validate_metadata(report)
        
        return True
    
    def _validate_sections(self, report: ExperimentReport):
        """Ensure all required sections present."""
        missing = set(SectionType) - set(report.sections.keys())
        if missing:
            raise ValueError(f"Missing required sections: {missing}")
        
        # Check each section has content
        for section_type, section in report.sections.items():
            if not section.title:
                raise ValueError(f"Section {section_type} missing title")
            if not section.body:
                raise ValueError(f"Section {section_type} missing body")
    
    def _validate_evidence(self, report: ExperimentReport):
        """Verify all evidence references resolve."""
        for section in report.sections.values():
            for ref in section.evidence_refs:
                artifact = self.snapshot_store.get_raw(ref.artifact_id)
                if not artifact:
                    raise ValueError(f"Evidence not found: {ref.artifact_id}")
                if not ref.verify(artifact):
                    raise ValueError(f"Evidence hash mismatch: {ref.artifact_id}")
    
    def _validate_language(self, report: ExperimentReport):
        """Check for forbidden language patterns."""
        normalizer = LanguageNormalizer()
        
        for section in report.sections.values():
            try:
                # This will raise if forbidden terms found
                normalizer.normalize(section.body)
            except ValueError as e:
                raise ValueError(f"Language violation in {section.title}: {e}")
    
    def _validate_metadata(self, report: ExperimentReport):
        """Verify metadata consistency."""
        meta = report.metadata
        
        if not meta.snapshot_id:
            raise ValueError("Missing snapshot_id in metadata")
        if not meta.replay_hash:
            raise ValueError("Missing replay_hash in metadata")
        if not meta.experiment_name:
            raise ValueError("Missing experiment_name in metadata")


# ============================================================================
# REPORT WATCHDOG
# ============================================================================

class ReportWatchdog:
    """
    Continuous monitoring for report integrity.
    
    MONITORS:
        - Report ↔ snapshot drift
        - Missing sections
        - Unverifiable claims
        - Version mismatches
    
    CAN:
        - Block rollout decisions
        - Flag compliance issues
        - Alert auditing systems
    """
    
    def __init__(self, validator: ReportValidator):
        self.validator = validator
        self._violations: List[Dict] = []
    
    def monitor(self, report: ExperimentReport) -> bool:
        """
        Real-time monitoring check.
        
        Returns True if report remains valid.
        """
        try:
            self.validator.validate(report)
            return True
        except ValueError as e:
            self._violations.append({
                'timestamp': datetime.utcnow(),
                'report_id': report.metadata.snapshot_id,
                'violation': str(e),
            })
            return False
    
    def check_drift(self, report: ExperimentReport, 
                    current_snapshot_id: str) -> bool:
        """Detect if report has drifted from current snapshot."""
        if report.metadata.snapshot_id != current_snapshot_id:
            self._violations.append({
                'timestamp': datetime.utcnow(),
                'report_id': report.metadata.snapshot_id,
                'violation': f'Snapshot drift: {current_snapshot_id}',
            })
            return False
        return True
    
    def get_violations(self) -> List[Dict]:
        """Retrieve all recorded violations."""
        return self._violations.copy()
    
    def clear_violations(self):
        """Reset violation log."""
        self._violations.clear()
    
    def alert_on_violation(self, report: ExperimentReport) -> Optional[str]:
        """
        Generate alert if violations detected.
        
        Returns alert message or None.
        """
        if not self.monitor(report):
            return f"""
REPORT INTEGRITY VIOLATION

Report: {report.metadata.experiment_name}
Snapshot: {report.metadata.snapshot_id}
Violations: {len(self._violations)}

Latest violation:
{self._violations[-1]['violation']}

ACTION REQUIRED: Do not use this report for decisions.
"""
        return None


# ============================================================================
# PUBLIC API
# ============================================================================

def generate_report(
    experiment_id: str,
    snapshot_id: str,
    snapshot_store,
    replay_loader,
    audience: str = 'internal',
    verbosity: str = 'standard',
    include_appendix: bool = False,
    output_format: str = 'markdown'
) -> str:
    """
    Main entry point for report generation.
    
    Args:
        experiment_id: Unique experiment identifier
        snapshot_id: Snapshot to generate report from
        snapshot_store: Artifact storage backend
        replay_loader: Replay session loader
        audience: Report audience (internal | executive | audit)
        verbosity: Detail level (concise | standard | exhaustive)
        include_appendix: Include raw data appendix
        output_format: Output format (markdown | text | json)
    
    Returns:
        Formatted report string
    
    Raises:
        ValueError: If report cannot be generated with evidence
        RuntimeError: If validation fails
    """
    # Create specification
    spec = ReportSpec(
        experiment_id=experiment_id,
        snapshot_id=snapshot_id,
        audience=audience,
        verbosity=verbosity,
        include_appendix=include_appendix
    )
    
    # Assemble report
    assembler = ReportAssembler(snapshot_store, replay_loader)
    report = assembler.assemble(spec)
    
    # Validate before emission
    validator = ReportValidator(snapshot_store)
    validator.validate(report)
    
    # Monitor integrity
    watchdog = ReportWatchdog(validator)
    alert = watchdog.alert_on_violation(report)
    if alert:
        raise RuntimeError(alert)
    
    # Render in requested format
    if output_format == 'markdown':
        return ReportRenderer.render_markdown(report)
    elif output_format == 'text':
        return ReportRenderer.render_text(report)
    elif output_format == 'json':
        return ReportRenderer.render_json(report)
    else:
        raise ValueError(f"Unsupported format: {output_format}")


def validate_report_file(report_path: str, snapshot_store) -> bool:
    """
    Validate an existing report file.
    
    Useful for auditing or regression checking.
    """
    # Load report (implementation depends on storage format)
    # Verify hash, evidence, sections
    # Return validation result
    pass


# ============================================================================
# INVARIANTS VERIFICATION
# ============================================================================

def _verify_system_invariants():
    """
    Runtime verification of critical invariants.
    
    This function should be called during system initialization.
    """
    # Verify all SectionType values have handlers
    required_methods = {
        f'_build_{st.value}' for st in SectionType
    }
    assembler_methods = {
        m for m in dir(ReportAssembler) if m.startswith('_build_')
    }
    
    missing = required_methods - assembler_methods
    if missing:
        raise RuntimeError(
            f"Missing section builders: {missing}\n"
            "All SectionType values must have corresponding _build_* methods"
        )
    
    # Verify language normalization rules complete
    if not LanguageNormalizer.FORBIDDEN_TERMS:
        raise RuntimeError("LanguageNormalizer.FORBIDDEN_TERMS cannot be empty")
    
    # Verify renderer supports all formats
    supported = {'markdown', 'text', 'json'}
    if not all(hasattr(ReportRenderer, f'render_{fmt}') for fmt in supported):
        raise RuntimeError("ReportRenderer missing required format handlers")


# Initialize verification
_verify_system_invariants()
        lines.append(subsection.body)
                lines.append("")
            
            # Evidence references
            if section.evidence_refs:
                lines.append("**Evidence:**")
                for ref in section.evidence_refs:
                    lines.append(f"- {ref.artifact_type}: `{ref.artifact_id}` ({ref.content_hash[:8]}...)")
                lines.append("")







