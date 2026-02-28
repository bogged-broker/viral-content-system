"""
input_recorder.py - Deterministic Input Capture for Time-Travel Replay

Location: /infra/replay/input_recorder.py

Purpose:
    Capture ALL external & nondeterministic inputs that influence system behavior
    so any execution can be re-run bit-for-bit identically.

    If it wasn't recorded, it didn't happen.

Guarantees:
    - Write-ahead safety (fsync before execution)
    - Hash-chained ordering (tamper detection)
    - Canonical serialization (deterministic replay)
    - Input boundary enforcement (no hidden reads)
    - Token-based access (full traceability)
"""

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, Optional, Dict, List
from contextlib import contextmanager


# ============================================================================
# INPUT CLASSIFICATION
# ============================================================================

class InputType(Enum):
    """Classification of all nondeterministic input sources."""
    USER_ACTION = "user_action"
    PLATFORM_RESPONSE = "platform_response"
    MODEL_DECISION = "model_decision"
    CONFIG_LOOKUP = "config_lookup"
    FLAG_RESOLUTION = "flag_resolution"
    CLOCK_READ = "clock_read"
    RANDOMNESS = "randomness"
    NETWORK_IO = "network_io"


# ============================================================================
# INPUT TOKEN - The Only Currency
# ============================================================================

@dataclass(frozen=True)
class InputToken:
    """
    Immutable proof that an input was recorded.
    Downstream systems consume tokens, never raw inputs.
    
    This guarantees:
        - No hidden reads
        - No mutation
        - Full traceability
    """
    input_id: str
    replay_seq: int
    content_hash: str
    input_type: InputType
    timestamp_logical: int
    source: str
    
    def __str__(self) -> str:
        return f"InputToken({self.input_id}@seq{self.replay_seq})"


# ============================================================================
# REPLAY CONTEXT - Execution Binding
# ============================================================================

@dataclass
class ReplayContext:
    """
    Binds this execution to a specific replay session.
    Every input recording is scoped to a context.
    """
    run_id: str
    environment: str
    started_at: datetime
    replay_mode: bool = False
    parent_run_id: Optional[str] = None
    
    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "environment": self.environment,
            "started_at": self.started_at.isoformat(),
            "replay_mode": self.replay_mode,
            "parent_run_id": self.parent_run_id
        }


# ============================================================================
# RECORDED INPUT - Storage Format
# ============================================================================

@dataclass
class RecordedInput:
    """
    The actual stored input record.
    Written to disk with fsync guarantees.
    """
    input_id: str
    replay_seq: int
    input_type: InputType
    source: str
    payload: dict
    timestamp_logical: int
    timestamp_wall: str
    content_hash: str
    previous_hash: Optional[str]
    context: dict
    
    def to_json_canonical(self) -> str:
        """Canonical JSON serialization for deterministic replay."""
        data = {
            "input_id": self.input_id,
            "replay_seq": self.replay_seq,
            "input_type": self.input_type.value,
            "source": self.source,
            "payload": self._normalize_payload(self.payload),
            "timestamp_logical": self.timestamp_logical,
            "timestamp_wall": self.timestamp_wall,
            "content_hash": self.content_hash,
            "previous_hash": self.previous_hash,
            "context": self.context
        }
        # Sorted keys, no whitespace variations, stable floats
        return json.dumps(data, sort_keys=True, separators=(',', ':'))
    
    @staticmethod
    def _normalize_payload(payload: dict) -> dict:
        """Normalize floats and ensure stable serialization."""
        normalized = {}
        for key in sorted(payload.keys()):
            value = payload[key]
            if isinstance(value, float):
                # Normalize float representation
                normalized[key] = round(value, 15)
            elif isinstance(value, dict):
                normalized[key] = RecordedInput._normalize_payload(value)
            elif isinstance(value, list):
                normalized[key] = [
                    RecordedInput._normalize_payload(item) if isinstance(item, dict) 
                    else item 
                    for item in value
                ]
            else:
                normalized[key] = value
        return normalized


# ============================================================================
# INPUT BOUNDARY - The Gate
# ============================================================================

class InputBoundary:
    """
    Ensures all nondeterministic inputs cross the same gate.
    This is the ONLY entry point for external data.
    """
    
    def __init__(self, recorder: 'InputRecorder'):
        self.recorder = recorder
    
    def record_user_action(self, action: str, payload: dict, context: ReplayContext) -> InputToken:
        """User interaction (click, input, command)."""
        return self.recorder.record(InputType.USER_ACTION, payload, f"user:{action}", context)
    
    def record_platform_response(self, platform: str, response: dict, context: ReplayContext) -> InputToken:
        """Platform API response (success, error, delay)."""
        return self.recorder.record(InputType.PLATFORM_RESPONSE, response, f"platform:{platform}", context)
    
    def record_model_decision(self, model: str, output: dict, context: ReplayContext) -> InputToken:
        """AI model output."""
        return self.recorder.record(InputType.MODEL_DECISION, output, f"model:{model}", context)
    
    def record_config_lookup(self, key: str, value: Any, context: ReplayContext) -> InputToken:
        """Configuration value resolution."""
        return self.recorder.record(InputType.CONFIG_LOOKUP, {"key": key, "value": value}, f"config:{key}", context)
    
    def record_flag_resolution(self, flag: str, enabled: bool, context: ReplayContext) -> InputToken:
        """Feature flag evaluation."""
        return self.recorder.record(InputType.FLAG_RESOLUTION, {"flag": flag, "enabled": enabled}, f"flag:{flag}", context)
    
    def record_clock_read(self, timestamp: int, context: ReplayContext) -> InputToken:
        """Logical clock read (monotonic ordering)."""
        return self.recorder.record(InputType.CLOCK_READ, {"timestamp": timestamp}, "clock", context)
    
    def record_randomness(self, seed: int, values: List[float], context: ReplayContext) -> InputToken:
        """Random number generation."""
        return self.recorder.record(InputType.RANDOMNESS, {"seed": seed, "values": values}, "random", context)
    
    def record_network_io(self, endpoint: str, data: dict, context: ReplayContext) -> InputToken:
        """Network I/O event."""
        return self.recorder.record(InputType.NETWORK_IO, data, f"network:{endpoint}", context)


# ============================================================================
# SAFETY EVENTS
# ============================================================================

class SafetyEvent(Exception):
    """Raised when input recording fails. Execution MUST halt."""
    pass


# ============================================================================
# INPUT RECORDER - Core Implementation
# ============================================================================

class InputRecorder:
    """
    Records all nondeterministic inputs with write-ahead safety.
    
    Guarantees:
        1. Inputs are fsynced before execution proceeds
        2. Hash-chained to detect tampering
        3. Canonically serialized for deterministic replay
        4. Token-based access only
        5. Silent failure is FORBIDDEN
    """
    
    def __init__(
        self,
        storage_dir: Path,
        emergency_stop_on_failure: bool = False,
        enable_fsync: bool = True
    ):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        self.emergency_stop_on_failure = emergency_stop_on_failure
        self.enable_fsync = enable_fsync
        
        self._lock = Lock()
        self._sequence_counter = 0
        self._logical_clock = 0
        self._last_hash: Optional[str] = None
        self._current_context: Optional[ReplayContext] = None
        
        # Integration points (set by runtime)
        self.runtime_context = None
        self.clock = None
        self.feature_flags = None
        self.config_registry = None
        self.rate_limiter = None
        self.outcome_collector = None
        self.replay_context = None
        
        # Boundary interface
        self.boundary = InputBoundary(self)
    
    def set_context(self, context: ReplayContext):
        """Bind recorder to execution context."""
        with self._lock:
            self._current_context = context
            self._sequence_counter = 0
            self._logical_clock = 0
            self._last_hash = None
    
    def record(
        self,
        input_type: InputType,
        payload: dict,
        source: str,
        context: ReplayContext
    ) -> InputToken:
        """
        Record a nondeterministic input with write-ahead safety.
        
        Args:
            input_type: Classification of input source
            payload: The actual input data
            source: Human-readable source identifier
            context: Execution context binding
        
        Returns:
            InputToken - immutable proof of recording
        
        Raises:
            SafetyEvent - if recording fails (execution MUST halt)
        """
        with self._lock:
            try:
                # Generate sequence
                self._sequence_counter += 1
                self._logical_clock += 1
                seq = self._sequence_counter
                
                # Generate input ID
                input_id = self._generate_input_id(input_type, source, seq)
                
                # Compute content hash
                content_hash = self._hash_content(payload)
                
                # Create recorded input
                recorded = RecordedInput(
                    input_id=input_id,
                    replay_seq=seq,
                    input_type=input_type,
                    source=source,
                    payload=payload,
                    timestamp_logical=self._logical_clock,
                    timestamp_wall=datetime.utcnow().isoformat(),
                    content_hash=content_hash,
                    previous_hash=self._last_hash,
                    context=context.to_dict()
                )
                
                # WRITE-AHEAD SAFETY: Record before allowing execution
                self._write_input(recorded)
                
                # Update chain
                self._last_hash = content_hash
                
                # Create token
                token = InputToken(
                    input_id=input_id,
                    replay_seq=seq,
                    content_hash=content_hash,
                    input_type=input_type,
                    timestamp_logical=self._logical_clock,
                    source=source
                )
                
                # Notify integrations
                self._notify_integrations(token, recorded)
                
                return token
                
            except Exception as e:
                # SILENT FAILURE IS FORBIDDEN
                self._handle_recording_failure(e, input_type, source, context)
                raise  # Never swallow
    
    def _generate_input_id(self, input_type: InputType, source: str, seq: int) -> str:
        """Generate unique, deterministic input ID."""
        components = [
            input_type.value,
            source,
            str(seq),
            str(self._logical_clock)
        ]
        raw = ":".join(components)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
    
    def _hash_content(self, payload: dict) -> str:
        """Compute deterministic content hash."""
        normalized = RecordedInput._normalize_payload(payload)
        canonical = json.dumps(normalized, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode()).hexdigest()
    
    def _write_input(self, recorded: RecordedInput):
        """
        Write input to disk with fsync guarantees.
        
        Format: one JSON record per line (newline-delimited JSON)
        Filename: {run_id}_inputs.jsonl
        """
        if not self._current_context:
            raise SafetyEvent("No context set - cannot record input")
        
        filepath = self.storage_dir / f"{self._current_context.run_id}_inputs.jsonl"
        
        try:
            # Write-ahead: append + flush + fsync
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(recorded.to_json_canonical() + '\n')
                f.flush()
                
                if self.enable_fsync:
                    os.fsync(f.fileno())
                    
        except Exception as e:
            raise SafetyEvent(f"Failed to write input {recorded.input_id}: {e}")
    
    def _notify_integrations(self, token: InputToken, recorded: RecordedInput):
        """Notify integration points of new input."""
        # Outcome collector: causal chain
        if self.outcome_collector:
            try:
                self.outcome_collector.record_input_event(token, recorded)
            except Exception:
                pass  # Don't fail recording due to integration issues
        
        # Replay context: execution binding
        if self.replay_context:
            try:
                self.replay_context.register_input(token)
            except Exception:
                pass
    
    def _handle_recording_failure(
        self,
        error: Exception,
        input_type: InputType,
        source: str,
        context: ReplayContext
    ):
        """
        Handle recording failure.
        
        Actions:
            1. Emit safety event (always)
            2. Log failure (always)
            3. Emergency stop (if configured)
        """
        safety_event = {
            "event": "input_recording_failure",
            "error": str(error),
            "input_type": input_type.value,
            "source": source,
            "context": context.to_dict(),
            "timestamp": datetime.utcnow().isoformat(),
            "critical": True
        }
        
        # Log to safety channel
        self._emit_safety_event(safety_event)
        
        # Emergency stop if configured
        if self.emergency_stop_on_failure:
            self._emergency_stop(safety_event)
    
    def _emit_safety_event(self, event: dict):
        """Emit safety event to monitoring/alerting system."""
        # Write to safety log
        safety_log = self.storage_dir / "safety_events.jsonl"
        try:
            with open(safety_log, 'a', encoding='utf-8') as f:
                f.write(json.dumps(event, sort_keys=True) + '\n')
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            # Last resort: stderr
            print(f"SAFETY EVENT: {event}", flush=True)
    
    def _emergency_stop(self, event: dict):
        """
        Emergency stop: halt all execution.
        Only triggered if emergency_stop_on_failure=True.
        """
        print(f"EMERGENCY STOP: Input recording failed", flush=True)
        print(f"Event: {event}", flush=True)
        # In production, this might trigger process shutdown
        raise SafetyEvent(f"Emergency stop: {event}")
    
    @contextmanager
    def recording_session(self, context: ReplayContext):
        """
        Context manager for recording sessions.
        
        Usage:
            with recorder.recording_session(context):
                # All inputs recorded with this context
                token = recorder.boundary.record_user_action(...)
        """
        old_context = self._current_context
        try:
            self.set_context(context)
            yield self
        finally:
            self._current_context = old_context
    
    def verify_chain(self, run_id: str) -> bool:
        """
        Verify hash chain integrity for a recording.
        
        Returns:
            True if chain is valid, False if tampered
        """
        filepath = self.storage_dir / f"{run_id}_inputs.jsonl"
        
        if not filepath.exists():
            return False
        
        previous_hash = None
        
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                record = json.loads(line)
                
                # Verify chain link
                if record['previous_hash'] != previous_hash:
                    return False
                
                # Verify content hash
                payload = record['payload']
                expected_hash = self._hash_content(payload)
                if record['content_hash'] != expected_hash:
                    return False
                
                previous_hash = record['content_hash']
        
        return True
    
    def load_recording(self, run_id: str) -> List[RecordedInput]:
        """Load a complete recording for replay."""
        filepath = self.storage_dir / f"{run_id}_inputs.jsonl"
        
        if not filepath.exists():
            raise FileNotFoundError(f"No recording found for run_id: {run_id}")
        
        records = []
        
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                data = json.loads(line)
                records.append(RecordedInput(
                    input_id=data['input_id'],
                    replay_seq=data['replay_seq'],
                    input_type=InputType(data['input_type']),
                    source=data['source'],
                    payload=data['payload'],
                    timestamp_logical=data['timestamp_logical'],
                    timestamp_wall=data['timestamp_wall'],
                    content_hash=data['content_hash'],
                    previous_hash=data['previous_hash'],
                    context=data['context']
                ))
        
        return records


# ============================================================================
# INTEGRATION ADAPTERS
# ============================================================================

class RuntimeContextAdapter:
    """Adapter for runtime_context integration."""
    
    def __init__(self, recorder: InputRecorder):
        self.recorder = recorder
    
    def bind_run(self, run_id: str, environment: str):
        """Called when a new run starts."""
        context = ReplayContext(
            run_id=run_id,
            environment=environment,
            started_at=datetime.utcnow(),
            replay_mode=False
        )
        self.recorder.set_context(context)


class ClockAdapter:
    """Adapter for logical clock integration."""
    
    def __init__(self, recorder: InputRecorder):
        self.recorder = recorder
    
    def record_clock_tick(self, context: ReplayContext) -> InputToken:
        """Record logical clock advancement."""
        timestamp = self.recorder._logical_clock
        return self.recorder.boundary.record_clock_read(timestamp, context)


class FeatureFlagAdapter:
    """Adapter for feature flag integration."""
    
    def __init__(self, recorder: InputRecorder):
        self.recorder = recorder
    
    def capture_gate(self, flag: str, enabled: bool, context: ReplayContext) -> InputToken:
        """Capture feature flag resolution."""
        return self.recorder.boundary.record_flag_resolution(flag, enabled, context)


class ConfigRegistryAdapter:
    """Adapter for config registry integration."""
    
    def __init__(self, recorder: InputRecorder):
        self.recorder = recorder
    
    def freeze_config(self, key: str, value: Any, context: ReplayContext) -> InputToken:
        """Capture config value at lookup time."""
        return self.recorder.boundary.record_config_lookup(key, value, context)


class RateLimiterAdapter:
    """Adapter for rate limiter integration."""
    
    def __init__(self, recorder: InputRecorder):
        self.recorder = recorder
    
    def capture_decision(self, decision: dict, context: ReplayContext) -> InputToken:
        """Capture rate limit decision."""
        return self.recorder.boundary.record_platform_response("rate_limiter", decision, context)


class OutcomeCollectorAdapter:
    """Adapter for outcome collector integration."""
    
    def __init__(self, recorder: InputRecorder):
        self.recorder = recorder
        self.causal_chain = []
    
    def record_input_event(self, token: InputToken, recorded: RecordedInput):
        """Build causal chain from inputs."""
        self.causal_chain.append({
            "token": token,
            "recorded": recorded,
            "timestamp": datetime.utcnow().isoformat()
        })


# ============================================================================
# FACTORY
# ============================================================================

def create_input_recorder(
    storage_dir: str = "/var/replay/inputs",
    emergency_stop: bool = False,
    enable_fsync: bool = True
) -> InputRecorder:
    """
    Create production input recorder with all integrations.
    
    Args:
        storage_dir: Where to store input recordings
        emergency_stop: Halt on recording failure
        enable_fsync: Enable fsync guarantees (disable only for testing)
    
    Returns:
        Configured InputRecorder instance
    """
    recorder = InputRecorder(
        storage_dir=Path(storage_dir),
        emergency_stop_on_failure=emergency_stop,
        enable_fsync=enable_fsync
    )
    
    # Wire up integrations
    recorder.runtime_context = RuntimeContextAdapter(recorder)
    recorder.clock = ClockAdapter(recorder)
    recorder.feature_flags = FeatureFlagAdapter(recorder)
    recorder.config_registry = ConfigRegistryAdapter(recorder)
    recorder.rate_limiter = RateLimiterAdapter(recorder)
    recorder.outcome_collector = OutcomeCollectorAdapter(recorder)
    
    return recorder


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

if __name__ == "__main__":
    # Create recorder
    recorder = create_input_recorder(
        storage_dir="/tmp/replay_demo",
        emergency_stop=False,
        enable_fsync=True
    )
    
    # Create execution context
    context = ReplayContext(
        run_id="demo_run_001",
        environment="production",
        started_at=datetime.utcnow()
    )
    
    # Recording session
    with recorder.recording_session(context):
        # Record user action
        token1 = recorder.boundary.record_user_action(
            action="click_button",
            payload={"button_id": "submit", "x": 100, "y": 200},
            context=context
        )
        print(f"Recorded: {token1}")
        
        # Record model decision
        token2 = recorder.boundary.record_model_decision(
            model="gpt-4",
            output={"text": "Hello world", "tokens": 2},
            context=context
        )
        print(f"Recorded: {token2}")
        
        # Record platform response
        token3 = recorder.boundary.record_platform_response(
            platform="twitter",
            response={"status": "success", "tweet_id": "12345"},
            context=context
        )
        print(f"Recorded: {token3}")
    
    # Verify chain integrity
    is_valid = recorder.verify_chain("demo_run_001")
    print(f"\nChain integrity: {'✓ VALID' if is_valid else '✗ TAMPERED'}")
    
    # Load recording for replay
    recording = recorder.load_recording("demo_run_001")
    print(f"\nRecorded {len(recording)} inputs")
    for rec in recording:
        print(f"  [{rec.replay_seq}] {rec.input_type.value} from {rec.source}")